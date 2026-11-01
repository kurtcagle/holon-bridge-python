"""Pipeline, ingest, and message tests.

Runs use ``wait=true`` so outcomes are deterministic; the background path is
tested for its immediate contract rather than by racing a task to completion.

Route-level tests authenticate with ``auth()`` from ``tests/_identity.py`` (the
bearer token plus the animus header ``require_animus`` demands), and
``PipelineStub`` inherits identity, ACL, Toolset and audit-config answers from
``IdentityStub``. The direct ``run_pipeline`` / ``MessageStore`` tests below
bypass the FastAPI layer entirely and need none of that.

Two stub behaviours worth knowing about:

- The audit middleware writes an ``hb:AuditEntry`` to ``urn:ds:audit`` after
  every response. Those land in ``audit_pushed``, so ``pushed`` holds only what
  the route under test wrote.
- ``validate_delta`` asks ``ASK { GRAPH <target> {...} }`` before validating a
  baseline, and skips the baseline if the target is empty. The stub answers
  "empty" for every graph unless a test lists it in ``nonempty_graphs``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.fetch import SourceFetchError
from holonbridge.messages import Message, MessageStore, StageRecord
from holonbridge.pipeline import (
    Manifest,
    PipelineError,
    PipelineNode,
    run_pipeline,
    topological_order,
)
from holonbridge.routes import pipeline as pipeline_routes
from holonbridge.server import create_app

from ._identity import TOKEN, IdentityStub, auth
from ._identity import row as _row

HB = "https://w3id.org/holonbridge/"
BUILD = "https://w3id.org/databook/ns#"
RULE_TYPE = f"{HB}NamedRule"

CONSTRUCT = "CONSTRUCT { ?s a ?c } WHERE { GRAPH <urn:ds:holons> { ?s a ?c } }"

DATABOOK_TEXT = """---
id: https://example.org/databooks/sensor-grid-v1
graph:
  named_graph: urn:ds:sensor-grid
---

```turtle
<urn:a> <urn:b> <urn:c> .
```
"""


def manifest_rows() -> list[dict]:
    """A diamond: source -> two stages -> target."""
    rows: list[dict] = []

    def node(iri: str, kind: str, **props: str) -> None:
        rows.append(_row(n=iri, type=f"{BUILD}{kind}", p=f"{BUILD}kindMarker", o=kind))
        for key, value in props.items():
            rows.append(_row(n=iri, type=f"{BUILD}{kind}", p=f"{BUILD}{key}", o=value))

    node("db:notes", "Source", id="notes", outputType="turtle")
    node(
        "db:shapes",
        "Stage",
        id="shapes",
        transformer="llm",
        inputType="turtle",
        outputType="shacl",
        order="1",
        dependsOn="db:notes",
    )
    node(
        "db:inferred",
        "Stage",
        id="inferred",
        transformer="sparql",
        rule="materialise",
        inputType="turtle",
        outputType="turtle",
        order="2",
        dependsOn="db:notes",
    )
    rows.append(_row(n="db:target", type=f"{BUILD}Target", p=f"{BUILD}id", o="compiled"))
    rows.append(_row(n="db:target", type=f"{BUILD}Target", p=f"{BUILD}dependsOn", o="db:shapes"))
    rows.append(_row(n="db:target", type=f"{BUILD}Target", p=f"{BUILD}dependsOn", o="db:inferred"))
    rows.append(_row(n="db:target", type=f"{BUILD}Target", p=f"{BUILD}transformer", o="sparql"))
    rows.append(_row(n="db:target", type=f"{BUILD}Target", p=f"{BUILD}rule", o="materialise"))
    rows.append(
        _row(n="db:target", type=f"{BUILD}Target", p=f"{BUILD}targetGraph", o="urn:ds:compiled")
    )
    return rows


def cyclic_rows() -> list[dict]:
    rows = []
    for a, b in (("db:x", "db:y"), ("db:y", "db:x")):
        rows.append(_row(n=a, type=f"{BUILD}Stage", p=f"{BUILD}id", o=a.split(":")[1]))
        rows.append(_row(n=a, type=f"{BUILD}Stage", p=f"{BUILD}dependsOn", o=b))
    return rows


def rule_rows() -> list[dict]:
    iri = "urn:ds:named-rule:materialise"
    return [
        _row(r=iri, type=RULE_TYPE, p=f"{HB}id", o="materialise"),
        _row(r=iri, type=RULE_TYPE, p=f"{HB}construct", o=CONSTRUCT),
        _row(r=iri, type=RULE_TYPE, p=f"{HB}targetGraph", o="urn:ds:derived"),
        _row(r=iri, type=RULE_TYPE, p=f"{HB}writeMode", o="Sync"),
        _row(r=iri, type=RULE_TYPE, p=f"{HB}ruleStatus", o="Active"),
    ]


class PipelineStub(IdentityStub):
    def __init__(self) -> None:
        super().__init__()
        self.manifests = {"build": manifest_rows(), "loop": cyclic_rows()}
        self.rules = rule_rows()
        self.updates: list[str] = []
        self.pushed: list[tuple[str, str]] = []
        self.audit_pushed: list[tuple[str, str]] = []
        self.replaced: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        self.graphs: dict[str, str] = {}
        self.nonempty_graphs: set[str] = set()
        self.shacl_reports: list[str] = []
        self.message_rows: list[dict] = []
        self.counts = {"constructed": 4, "added": 4, "removed": 0, "target_size": 0}

    async def select(self, conn, query, *, default_graph=None):
        infra = self.infra_select(query)
        if infra is not None:
            return infra
        if query.lstrip().upper().startswith("ASK"):
            return {"boolean": any(f"<{graph}>" in query for graph in self.nonempty_graphs)}
        if "COUNT(*)" in query:
            return {"results": {"bindings": [{"n": {"value": str(self._count(query))}}]}}
        if "?sp ?so" in query or "messageId" in query:
            return {"results": {"bindings": self.message_rows}}
        if "Manifest" in query and "pipelineId" in query:
            return {
                "results": {
                    "bindings": [
                        _row(
                            id=name,
                            graph=f"urn:ds:pipeline:{name}",
                            label=name,
                            registeredAt="2026-07-27T00:00:00+00:00",
                        )
                        for name in sorted(self.manifests)
                    ]
                }
            }
        if 'STRENDS(STR(?type), "Target")' in query:
            for name, rows in self.manifests.items():
                if f"urn:ds:pipeline:{name}" in query:
                    return {"results": {"bindings": rows}}
            return {"results": {"bindings": []}}
        if "?link" in query:
            return {"results": {"bindings": []}}
        if "NamedRule" in query:
            return {"results": {"bindings": self.rules}}
        return {"results": {"bindings": []}}

    def _count(self, query: str) -> int:
        negated = "NOT EXISTS" in query
        head = query.split("FILTER NOT EXISTS")[0]
        if "rule-scratch" in head:
            return self.counts["added"] if negated else self.counts["constructed"]
        return self.counts["removed"] if negated else self.counts["target_size"]

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        return "<urn:a> <urn:b> <urn:c> .\n"

    async def update(self, conn, update):
        self.updates.append(update)

    async def get_graph(self, conn, graph_iri):
        return self.graphs.get(graph_iri, "")

    async def post_graph(self, conn, graph_iri, turtle):
        if graph_iri.startswith("urn:ds:audit"):
            self.audit_pushed.append((graph_iri, turtle))
            return
        self.pushed.append((graph_iri, turtle))

    async def put_graph(self, conn, graph_iri, turtle):
        self.replaced.append((graph_iri, turtle))

    async def drop_graph(self, conn, graph_iri):
        self.dropped.append(graph_iri)

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        return self.shacl_reports.pop(0) if self.shacl_reports else CONFORMS

    async def ping(self, conn):
        return True

    async def aclose(self):
        return None


CONFORMS = "@prefix sh: <http://www.w3.org/ns/shacl#> .\n[] a sh:ValidationReport ; sh:conforms true ."
VIOLATES = """@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .
[] a sh:ValidationReport ; sh:conforms false ;
   sh:result [ a sh:ValidationResult ; sh:focusNode ex:a ; sh:resultMessage "bad" ] ."""


@pytest.fixture
def stub() -> PipelineStub:
    return PipelineStub()


@pytest.fixture
def client(stub: PipelineStub):
    app = create_app(Settings(bearer_token=TOKEN, named_query_ttl=0.0))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        yield test_client


def conn() -> Conn:
    return Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")


# --- ordering -----------------------------------------------------------------


def test_dependencies_run_before_dependents(client):
    body = client.get("/pipeline/build", headers=auth()).json()
    order = body["order"]
    assert order.index("notes") < order.index("shapes")
    assert order.index("notes") < order.index("inferred")
    assert order.index("compiled") == len(order) - 1


def test_declared_order_breaks_ties(client):
    order = client.get("/pipeline/build", headers=auth()).json()["order"]
    assert order.index("shapes") < order.index("inferred")  # order 1 before order 2


def test_cycle_is_reported_not_silently_dropped(client):
    body = client.get("/pipeline/loop", headers=auth()).json()
    assert body["runnable"] is False
    assert "cycle" in body["error"]
    assert "x" in body["error"] and "y" in body["error"]


def test_cyclic_manifest_cannot_be_run(client):
    response = client.post("/pipeline-run", json={"pipeline": "loop"}, headers=auth())
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "unrunnable_manifest"


def test_topological_order_is_stable_without_declared_orders():
    manifest = Manifest(id="m", graph="g")
    manifest.nodes = {
        "b": PipelineNode(iri="b", id="b", kind="Stage", depends_on=["a"]),
        "a": PipelineNode(iri="a", id="a", kind="Source"),
        "c": PipelineNode(iri="c", id="c", kind="Target", depends_on=["b"]),
    }
    assert [n.id for n in topological_order(manifest)] == ["a", "b", "c"]


def test_dangling_dependency_warns_but_still_orders():
    manifest = Manifest(id="m", graph="g")
    manifest.nodes = {
        "a": PipelineNode(iri="a", id="a", kind="Stage", depends_on=["missing"]),
    }
    order = topological_order(manifest)
    assert [n.id for n in order] == ["a"]
    assert any("does not define" in w for w in manifest.warnings)


# test_type_mismatch_is_a_warning was retired: its fixture has no type
# mismatch (its own comment said so) and it asserted only that ``warnings`` is
# a list, so it could not fail. If input/output type checking needs a test, it
# needs a manifest that actually mismatches and an assertion on the warning.


def test_unknown_pipeline_is_404(client):
    assert client.get("/pipeline/nope", headers=auth()).status_code == 404


# --- registration -------------------------------------------------------------


def test_register_writes_the_manifest_graph_and_indexes_it(client, stub):
    stub.manifests["fresh"] = manifest_rows()
    body = client.post(
        "/pipeline",
        json={"id": "fresh", "manifest": "<db:notes> a <https://w3id.org/databook/ns#Source> ."},
        headers=auth(),
    ).json()

    assert body["ok"] is True
    assert body["graph"] == "urn:ds:pipeline:fresh"
    assert len(stub.replaced) == 1
    graph, turtle = stub.replaced[0]
    assert graph == "urn:ds:pipeline:fresh"
    assert "databook/ns#Source" in turtle
    assert any("pipelineId" in u for u in stub.updates)


def test_register_rejects_an_unsafe_id(client):
    response = client.post(
        "/pipeline", json={"id": "../evil", "manifest": "x"}, headers=auth()
    )
    assert response.status_code == 422


def test_register_reports_an_unrunnable_manifest_immediately(client, stub):
    stub.manifests["broken"] = cyclic_rows()
    body = client.post(
        "/pipeline", json={"id": "broken", "manifest": "x"}, headers=auth()
    ).json()
    assert body["runnable"] is False
    assert "cycle" in body["error"]


def test_pipelines_are_listed(client):
    body = client.get("/pipelines", headers=auth()).json()
    assert {p["id"] for p in body["pipelines"]} == {"build", "loop"}


def test_drop_removes_graph_and_index_entry(client, stub):
    body = client.delete("/pipeline/build", headers=auth()).json()
    assert body["ok"] is True
    assert "urn:ds:pipeline:build" in stub.dropped


def test_register_is_refused_without_any_grant(client, stub):
    """Withdraw both grants so the outcome does not depend on which of write
    or replace the route checks. Registration replaces the manifest graph
    (``put_graph``), so it is probably gated on replace -- with only the write
    grant withdrawn it returned 200 -- but that is an inference, and this test
    deliberately does not pin it. If this returns 200, registration is
    ungated, which is a real gap rather than a stale test."""
    stub.write_grant = False
    stub.replace_grant = False
    response = client.post(
        "/pipeline",
        json={"id": "fresh", "manifest": "<db:notes> a <https://w3id.org/databook/ns#Source> ."},
        headers=auth(),
    )
    assert response.status_code == 403
    assert stub.replaced == []


def test_drop_needs_a_replace_grant(client, stub):
    stub.replace_grant = False
    response = client.delete("/pipeline/build", headers=auth())
    assert response.status_code == 403
    assert stub.dropped == []


# --- running ------------------------------------------------------------------


def test_run_executes_sparql_stages_and_defers_llm_ones(client):
    body = client.post(
        "/pipeline-run", json={"pipeline": "build", "wait": True}, headers=auth()
    ).json()

    stages = {s["name"]: s for s in body["stages"]}
    assert stages["notes"]["status"] == "Skipped"
    assert stages["shapes"]["status"] == "Deferred"
    assert "not executed by the bridge" in stages["shapes"]["detail"]
    assert stages["inferred"]["status"] == "Completed"
    assert body["status"] == "Completed"


def test_completed_stage_reports_what_the_rule_wrote(client):
    body = client.post(
        "/pipeline-run", json={"pipeline": "build", "wait": True}, headers=auth()
    ).json()
    inferred = next(s for s in body["stages"] if s["name"] == "inferred")
    assert inferred["triplesWritten"] == 4
    assert "Sync into" in inferred["detail"]


def test_manifest_target_graph_overrides_the_rules_own(client):
    body = client.post(
        "/pipeline-run", json={"pipeline": "build", "wait": True}, headers=auth()
    ).json()
    compiled = next(s for s in body["stages"] if s["name"] == "compiled")
    assert "urn:ds:compiled" in compiled["detail"]


def test_unknown_rule_fails_the_stage_and_stops_the_run(client, stub):
    stub.rules = []
    body = client.post(
        "/pipeline-run", json={"pipeline": "build", "wait": True}, headers=auth()
    ).json()
    assert body["status"] == "Failed"
    inferred = next(s for s in body["stages"] if s["name"] == "inferred")
    assert inferred["status"] == "Failed"
    assert "unknown rule" in inferred["detail"]
    # the run halted, so the target stage never ran
    assert not any(s["name"] == "compiled" for s in body["stages"])


def test_run_can_continue_past_a_failing_stage(client, stub):
    stub.rules = []
    body = client.post(
        "/pipeline-run",
        json={"pipeline": "build", "wait": True, "stop_on_error": False},
        headers=auth(),
    ).json()
    assert body["status"] == "Failed"
    assert any(s["name"] == "compiled" for s in body["stages"])


def test_background_run_returns_a_message_id_immediately(client):
    body = client.post("/pipeline-run", json={"pipeline": "build"}, headers=auth()).json()
    assert body["pipeline"] == "build"
    assert len(body["messageId"]) == 32
    assert body["status"] == "Received"


# --- prompt-based ingestion: context-only stages + llm rendering ---------------
#
# Exercised by calling run_pipeline() directly against a hand-built Manifest
# and a PipelineStub, the same way test_topological_order_is_stable_without_
# declared_orders() above bypasses the FastAPI/AnimusDep layer entirely --
# nothing here needs an identity or an ACL decision, only pipeline.py's own
# execution logic.


def test_context_only_stage_is_read_only_and_feeds_llm_context(stub):
    manifest = Manifest(id="cnl", graph="urn:ds:pipeline:cnl")
    manifest.nodes = {
        "db2:source": PipelineNode(iri="db2:source", id="source", kind="Source"),
        "db2:context": PipelineNode(
            iri="db2:context",
            id="context",
            kind="Stage",
            transformer="sparql",
            rule="context-rule",
            context_only=True,
            order=1,
            depends_on=["db2:source"],
        ),
        "db2:map": PipelineNode(
            iri="db2:map",
            id="map",
            kind="Stage",
            transformer="llm",
            prompt_text="Context:\n{{context}}\n\nInput:\n{{input}}",
            order=2,
            depends_on=["db2:context"],
        ),
    }

    # context-rule declares its own targetGraph, deliberately, so the test
    # actually proves a context-only stage never writes there -- it isn't
    # read-only merely because no target was configured anywhere.
    stub.rules = stub.rules + [
        _row(r="urn:ds:named-rule:context-rule", type=RULE_TYPE, p=f"{HB}id", o="context-rule"),
        _row(r="urn:ds:named-rule:context-rule", type=RULE_TYPE, p=f"{HB}construct", o=CONSTRUCT),
        _row(
            r="urn:ds:named-rule:context-rule",
            type=RULE_TYPE,
            p=f"{HB}targetGraph",
            o="urn:ds:should-never-be-written",
        ),
        _row(r="urn:ds:named-rule:context-rule", type=RULE_TYPE, p=f"{HB}writeMode", o="Append"),
        _row(r="urn:ds:named-rule:context-rule", type=RULE_TYPE, p=f"{HB}ruleStatus", o="Active"),
    ]

    message = Message(id="m1", pipeline="cnl")

    import asyncio

    asyncio.run(
        run_pipeline(conn(), stub, manifest, message, params={"input": "raw source text"})
    )

    stages = {s.name: s for s in message.stages}
    assert stages["source"].status == "Skipped"
    assert stages["context"].status == "Completed"
    assert "not written to any graph" in stages["context"].detail
    assert not any(g == "urn:ds:should-never-be-written" for g, _ in stub.pushed)

    map_stage = stages["map"]
    assert map_stage.status == "Deferred"
    assert "raw source text" in map_stage.result
    assert "<urn:a> <urn:b> <urn:c> ." in map_stage.result  # the stub's construct() output
    assert message.error == ""


def test_llm_stage_without_prompt_text_keeps_the_original_placeholder_detail(stub):
    """No regression for existing manifests: an ``llm`` stage that declares
    no ``promptText`` (like ``build``'s own ``shapes`` stage) keeps the
    exact original detail text and an empty ``result``."""
    manifest = Manifest(id="cnl2", graph="urn:ds:pipeline:cnl2")
    manifest.nodes = {
        "db3:shapes": PipelineNode(
            iri="db3:shapes", id="shapes", kind="Stage", transformer="llm"
        ),
    }
    message = Message(id="m2", pipeline="cnl2")

    import asyncio

    asyncio.run(run_pipeline(conn(), stub, manifest, message))

    stage = message.stages[0]
    assert stage.status == "Deferred"
    assert stage.detail == "'llm' stages are not executed by the bridge"
    assert stage.result == ""


def test_llm_stage_exposes_extra_params_as_named_slots(stub):
    """A caller-supplied param beyond ``input`` -- e.g. ``submitter`` -- is
    available as its own ``{{name}}`` placeholder, so a prompt template can
    ask for run-specific metadata the bridge already knows (who is running
    this invocation) without a bespoke field for every such use. See
    pipeline.py's "Extra param slots" and ``_extra_param_slots``."""
    manifest = Manifest(id="cnl5", graph="urn:ds:pipeline:cnl5")
    manifest.nodes = {
        "db5:map": PipelineNode(
            iri="db5:map",
            id="map",
            kind="Stage",
            transformer="llm",
            prompt_text="Submitter: {{submitter}}\nInput: {{input}}",
        ),
    }
    message = Message(id="m5", pipeline="cnl5")

    import asyncio

    asyncio.run(
        run_pipeline(
            conn(),
            stub,
            manifest,
            message,
            params={
                "input": "Example Corp uses HolonBridge.",
                "submitter": "urn:example:person:jane",
            },
        )
    )

    stage = message.stages[0]
    assert stage.status == "Deferred"
    assert "Submitter: urn:example:person:jane" in stage.result
    assert "Input: Example Corp uses HolonBridge." in stage.result
    # "input" is consumed only by {{input}} -- never also exposed under its
    # own name, so no leftover, unsubstituted placeholder should remain.
    assert "{{input}}" not in stage.result
    assert "{{submitter}}" not in stage.result


def test_llm_stage_extra_param_slot_is_unused_without_a_matching_placeholder(stub):
    """A param the template never names is simply unused -- no error, and
    (unlike render_template's own "nothing matched" fallback) it must not
    get silently appended just because *some* other slot (``{{input}}``)
    did match."""
    manifest = Manifest(id="cnl6", graph="urn:ds:pipeline:cnl6")
    manifest.nodes = {
        "db6:map": PipelineNode(
            iri="db6:map", id="map", kind="Stage", transformer="llm", prompt_text="Input: {{input}}"
        ),
    }
    message = Message(id="m6", pipeline="cnl6")

    import asyncio

    asyncio.run(
        run_pipeline(
            conn(),
            stub,
            manifest,
            message,
            params={"input": "text", "unused_metadata": "should not appear"},
        )
    )

    stage = message.stages[0]
    assert stage.result == "Input: text"


def test_context_only_and_target_graph_together_is_flagged_at_load_time(client, stub):
    """A stage that both reads for context and declares a targetGraph is a
    modeling error the schema should catch -- see pipeline.py's load_manifest."""
    stub.manifests["confused"] = [
        _row(
            n="db4:both",
            type=f"{BUILD}Stage",
            p=f"{BUILD}id",
            o="both",
        ),
        _row(n="db4:both", type=f"{BUILD}Stage", p=f"{BUILD}contextOnly", o="true"),
        _row(n="db4:both", type=f"{BUILD}Stage", p=f"{BUILD}targetGraph", o="urn:ds:somewhere"),
    ]
    body = client.get("/pipeline/confused", headers=auth()).json()
    assert any("contextOnly" in w and "targetGraph" in w for w in body["warnings"])


# --- ingest -------------------------------------------------------------------


def test_ingest_lands_an_inline_payload(client, stub):
    body = client.post(
        "/ingest",
        json={"turtle": "<urn:a> <urn:b> <urn:c> .", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    ).json()
    assert body["status"] == "Completed"
    assert stub.pushed[-1][0] == "urn:ds:holons"
    assert body["stages"][0]["status"] == "Completed"


def test_ingest_needs_exactly_one_source(client):
    both = client.post(
        "/ingest",
        json={"turtle": "x", "source_graph": "urn:ds:a", "graph_iri": "urn:ds:b"},
        headers=auth(),
    )
    neither = client.post("/ingest", json={"graph_iri": "urn:ds:b"}, headers=auth())
    assert both.status_code == 400 and neither.status_code == 400


def test_ingest_from_a_graph_already_in_the_store(client, stub):
    stub.graphs["urn:ds:staging"] = "<urn:a> <urn:b> <urn:c> .\n"
    body = client.post(
        "/ingest",
        json={"source_graph": "urn:ds:staging", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    ).json()
    assert body["status"] == "Completed"
    assert stub.pushed[-1][1] == "<urn:a> <urn:b> <urn:c> .\n"


def test_ingest_from_an_empty_graph_fails_clearly(client):
    response = client.post(
        "/ingest",
        json={"source_graph": "urn:ds:nothing", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    )
    assert response.status_code == 400
    assert "empty or absent" in response.json()["detail"]["message"]


def test_ingest_cannot_bypass_the_shacl_gate(client, stub):
    stub.graphs["urn:ds:shacl"] = "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    # The target has content, so delta validation runs a baseline first and
    # consumes the first report; the merged state then reads the second.
    stub.nonempty_graphs = {"urn:ds:holons"}
    stub.shacl_reports = [CONFORMS, VIOLATES]  # baseline clean, merged dirty
    response = client.post(
        "/ingest",
        json={
            "turtle": "<urn:a> <urn:b> <urn:c> .",
            "graph_iri": "urn:ds:holons",
            "shapes_graph": "urn:ds:shacl",
        },
        headers=auth(),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "shacl_violation"
    assert len(detail["messageId"]) == 32
    # the scratch graph used for delta validation is written; the target is not
    assert not any(graph == "urn:ds:holons" for graph, _ in stub.pushed)


def test_ingest_needs_a_write_grant(client, stub):
    stub.write_grant = False
    response = client.post(
        "/ingest",
        json={"turtle": "<urn:a> <urn:b> <urn:c> .", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    )
    assert response.status_code == 403
    assert not any(graph == "urn:ds:holons" for graph, _ in stub.pushed)


def test_ingest_then_pipeline(client):
    body = client.post(
        "/ingest",
        json={
            "turtle": "<urn:a> <urn:b> <urn:c> .",
            "graph_iri": "urn:ds:holons",
            "pipeline": "build",
            "wait": True,
        },
        headers=auth(),
    ).json()
    assert body["status"] == "Completed"
    names = [s["name"] for s in body["stages"]]
    assert names[0] == "ingest"
    assert "inferred" in names


def test_ingest_with_an_unknown_pipeline_records_the_failure(client):
    response = client.post(
        "/ingest",
        json={"turtle": "<urn:a> <urn:b> <urn:c> .", "graph_iri": "urn:ds:holons", "pipeline": "nope"},
        headers=auth(),
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_pipeline"


# --- ingest: databook and source_url vectors -----------------------------------


def test_ingest_from_an_inline_databook(client, stub):
    body = client.post("/ingest", json={"databook": DATABOOK_TEXT}, headers=auth()).json()
    assert body["status"] == "Completed"
    assert body["targetGraph"] == "urn:ds:sensor-grid"
    assert stub.pushed[-1] == ("urn:ds:sensor-grid", "<urn:a> <urn:b> <urn:c> .")


def test_ingest_databook_explicit_graph_iri_wins(client, stub):
    client.post(
        "/ingest",
        json={"databook": DATABOOK_TEXT, "graph_iri": "urn:ds:override"},
        headers=auth(),
    )
    assert stub.pushed[-1][0] == "urn:ds:override"


def test_ingest_databook_without_named_graph_needs_graph_iri(client):
    text = "---\nid: x\n---\n\n```turtle\n<urn:a> <urn:b> <urn:c> .\n```\n"
    response = client.post("/ingest", json={"databook": text}, headers=auth())
    assert response.status_code == 400
    assert "graph_iri is required" in response.json()["detail"]["message"]


def test_ingest_databook_with_no_turtle_block_fails_clearly(client):
    text = "---\nid: x\n---\nJust prose, no data.\n"
    response = client.post(
        "/ingest", json={"databook": text, "graph_iri": "urn:ds:holons"}, headers=auth()
    )
    assert response.status_code == 400
    assert "no turtle" in response.json()["detail"]["message"]


def test_ingest_from_a_source_url(client, stub, monkeypatch):
    async def fake_fetch(url, *, timeout=30.0):
        assert url == "https://example.org/data.ttl"
        return "<urn:a> <urn:b> <urn:c> .", "text/turtle"

    monkeypatch.setattr(pipeline_routes, "fetch_source", fake_fetch)

    body = client.post(
        "/ingest",
        json={"source_url": "https://example.org/data.ttl", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    ).json()

    assert body["status"] == "Completed"
    stages = {s["name"]: s for s in body["stages"]}
    assert stages["fetch"]["status"] == "Completed"
    assert stages["ingest"]["status"] == "Completed"
    assert stub.pushed[-1] == ("urn:ds:holons", "<urn:a> <urn:b> <urn:c> .")


def test_ingest_from_a_source_url_that_is_a_databook(client, stub, monkeypatch):
    async def fake_fetch(url, *, timeout=30.0):
        return DATABOOK_TEXT, "text/markdown"

    monkeypatch.setattr(pipeline_routes, "fetch_source", fake_fetch)

    body = client.post(
        "/ingest",
        json={"source_url": "https://example.org/sensor.databook.md"},
        headers=auth(),
    ).json()

    assert body["status"] == "Completed"
    assert stub.pushed[-1][0] == "urn:ds:sensor-grid"


def test_ingest_source_url_fetch_failure_is_recorded(client, monkeypatch):
    async def fake_fetch(url, *, timeout=30.0):
        raise SourceFetchError(url, "HTTP 404")

    monkeypatch.setattr(pipeline_routes, "fetch_source", fake_fetch)

    response = client.post(
        "/ingest",
        json={"source_url": "https://example.org/gone.ttl", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    )
    assert response.status_code == 400
    assert "HTTP 404" in response.json()["detail"]["message"]


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1:3030/ds/sparql",
        "http://example.org/plain.ttl",
    ],
)
def test_ingest_source_url_cannot_reach_internal_or_plain_http_targets(client, stub, url):
    # The real fetch runs here (no monkeypatch): the policy is what is under test.
    response = client.post(
        "/ingest", json={"source_url": url, "graph_iri": "urn:ds:holons"}, headers=auth()
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "ingest_failed" and "refused" in detail["message"]
    assert stub.pushed == []  # nothing was written


def test_ingest_still_needs_exactly_one_source_with_new_shapes(client):
    too_many = client.post(
        "/ingest",
        json={"turtle": "x", "databook": DATABOOK_TEXT, "graph_iri": "urn:ds:b"},
        headers=auth(),
    )
    assert too_many.status_code == 400


# --- messages -----------------------------------------------------------------


def test_unknown_message_is_404(client):
    assert client.get("/message/deadbeef", headers=auth()).status_code == 404


@pytest.mark.asyncio
async def test_message_writes_escape_their_literals(stub):
    store = MessageStore(stub)
    message = Message(id="abc", pipeline="build")
    message.stages.append(
        StageRecord(name="s", status="Failed", detail='he said "no"\nthen quit')
    )
    await store.save(stub_conn := conn(), message)

    insert = next(u for u in stub.updates if u.startswith("INSERT DATA"))
    assert '\\"no\\"' in insert and "\\n" in insert
    assert "\n" not in insert.split('"he said')[1].split('"')[0]
    assert f"urn:{stub_conn.dataset}:message:abc" in insert


@pytest.mark.asyncio
async def test_message_save_deletes_before_inserting(stub):
    await MessageStore(stub).save(conn(), Message(id="abc"))
    assert stub.updates[0].startswith("DELETE")
    assert stub.updates[1].startswith("INSERT DATA")


@pytest.mark.asyncio
async def test_message_reads_back_its_stages(stub):
    stub.message_rows = [
        _row(p=f"{HB}messageId", o="abc", stage="s0", sp=f"{HB}stageName", so="ingest"),
        _row(p=f"{HB}status", o="Completed", stage="s0", sp=f"{HB}stageStatus", so="Completed"),
        _row(p=f"{HB}status", o="Completed", stage="s0", sp=f"{HB}triplesWritten", so="12"),
    ]
    message = await MessageStore(stub).get(conn(), "abc")
    assert message is not None
    assert message.status == "Completed"
    assert message.stages[0].name == "ingest"
    assert message.stages[0].triples_written == 12


def test_message_iri_is_unscoped_by_default():
    """No-op guarantee: a dataset that has not opted into bank scoping gets
    byte-identical IRIs to before this fix."""
    plain = conn()
    assert Message(id="abc").iri(plain) == "urn:ds:message:abc"


def test_message_iri_follows_bank_scoping_when_opted_in():
    scoped = Conn(
        base_url="http://x",
        dataset="ds",
        overridden=False,
        bank_name="secondary",
        bank_scoped_datasets=frozenset({"ds"}),
    )
    assert Message(id="abc").iri(scoped) == "urn:secondary:ds:message:abc"


@pytest.mark.asyncio
async def test_message_get_queries_the_same_iri_it_would_be_saved_under():
    """Regression test for the bug this fixes: save() and get() must agree
    on the message IRI, or a bank-scoped message becomes permanently
    unfindable through its own store the moment it is written."""

    captured: dict[str, str] = {}

    class QueryCapture:
        async def select(self, conn, query, *, default_graph=None):
            captured["query"] = query
            return {"results": {"bindings": []}}

    scoped = Conn(
        base_url="http://x",
        dataset="ds",
        overridden=False,
        bank_name="secondary",
        bank_scoped_datasets=frozenset({"ds"}),
    )
    await MessageStore(QueryCapture()).get(scoped, "abc")
    assert "urn:secondary:ds:message:abc" in captured["query"]
    assert "urn:ds:message:abc" not in captured["query"]
