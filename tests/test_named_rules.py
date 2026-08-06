"""Named-rule tests.

The stub answers COUNT queries by inspecting which graph is on which side of
the ``FILTER NOT EXISTS``, so the three write modes can be told apart by the
updates they issue and the counts they report — without implementing a
triplestore.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from holonbridge.config import Settings
from holonbridge.fuseki import FusekiTimeout
from holonbridge.server import create_app

TOKEN = "test-token"
NS = "https://w3id.org/holonbridge/"
RULE_TYPE = f"{NS}NamedRule"

SUBCLASS_BODY = (
    "CONSTRUCT { ?s a ?super }\n"
    "WHERE {\n"
    "  GRAPH <urn:ds:holons>   { ?s a ?c }\n"
    "  GRAPH <urn:ds:ontology> { ?c <http://www.w3.org/2000/01/rdf-schema#subClassOf>+ ?super }\n"
    "}"
)
ROSTER_BODY = (
    "CONSTRUCT { ?p <urn:squadOf> {{team}} }\n"
    "WHERE { GRAPH <urn:ds:holons> { ?p <urn:onTeam> {{team}} } }"
)
FOCUS_BODY = "CONSTRUCT { $this <urn:derived> ?o } WHERE { GRAPH <urn:ds:holons> { $this <urn:src> ?o } }"


def _row(**cells: str) -> dict:
    return {key: {"value": value} for key, value in cells.items()}


def rule_rows() -> list[dict]:
    rows: list[dict] = []

    def rule(iri: str, **props: str) -> None:
        for key, value in props.items():
            rows.append(_row(r=iri, type=RULE_TYPE, p=f"{NS}{key}", o=value))

    rule(
        "urn:ds:named-rule:append-derived",
        id="append-derived",
        construct=SUBCLASS_BODY,
        targetGraph="urn:ds:derived",
        writeMode="Append",
        ruleStatus="Active",
        order="5",
    )
    rule(
        "urn:ds:named-rule:rdfs-subclass-inference",
        id="rdfs-subclass-inference",
        construct=SUBCLASS_BODY,
        targetGraph="urn:ds:inferred",
        writeMode="Sync",
        ruleStatus="Active",
        order="10",
    )
    rule(
        "urn:ds:named-rule:replace-snapshot",
        id="replace-snapshot",
        construct=SUBCLASS_BODY,
        targetGraph="urn:ds:snapshot",
        writeMode="Replace",
        ruleStatus="Active",
        order="20",
    )
    rule(
        "urn:ds:named-rule:squad",
        id="squad",
        construct=ROSTER_BODY,
        targetGraph="urn:ds:squads",
        writeMode="Append",
        ruleStatus="Active",
        order="30",
    )
    rule(
        "urn:ds:named-rule:focus",
        id="focus",
        construct=FOCUS_BODY,
        targetGraph="urn:ds:focus",
        ruleStatus="Active",
        order="40",
    )
    rule(
        "urn:ds:named-rule:old",
        id="old",
        construct=SUBCLASS_BODY,
        targetGraph="urn:ds:legacy",
        ruleStatus="Suspended",
        order="50",
    )
    rule(
        "urn:ds:named-rule:gone",
        id="gone",
        construct=SUBCLASS_BODY,
        targetGraph="urn:ds:legacy",
        ruleStatus="Deprecated",
        order="60",
    )
    rule(
        "urn:ds:named-rule:not-a-construct",
        id="not-a-construct",
        construct="SELECT * WHERE { ?s ?p ?o }",
        targetGraph="urn:ds:whatever",
        ruleStatus="Active",
        order="70",
    )
    # no target graph — must be reported and skipped, not fail at run time
    rule(
        "urn:ds:named-rule:homeless",
        id="homeless",
        construct=SUBCLASS_BODY,
        ruleStatus="Active",
    )
    # unparseable order
    rule(
        "urn:ds:named-rule:odd-order",
        id="odd-order",
        construct=SUBCLASS_BODY,
        targetGraph="urn:ds:odd",
        ruleStatus="Active",
        order="soon",
    )
    return rows


def rule_param_rows() -> list[dict]:
    return [
        _row(r="urn:ds:named-rule:squad", param="_:t", p=f"{NS}name", o="team"),
        _row(r="urn:ds:named-rule:squad", param="_:t", p=f"{NS}datatype", o="xsd:anyURI"),
        _row(r="urn:ds:named-rule:squad", param="_:t", p=f"{NS}required", o="true"),
    ]


class RuleStub:
    """Serves the rule registry and answers COUNT queries by shape."""

    def __init__(self) -> None:
        self.rules = rule_rows()
        self.params = rule_param_rows()
        self.updates: list[str] = []
        self.constructed: list[str] = []
        self.pushed: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        self.counts = {"constructed": 7, "added": 3, "removed": 2, "target_size": 5}
        self.construct_error: Exception | None = None
        self.turtle = "<urn:a> <urn:b> <urn:c> .\n"

    async def select(self, conn, query, *, default_graph=None):
        if "COUNT(*)" in query:
            return {"results": {"bindings": [{"n": {"value": str(self._count(query))}}]}}
        if "?link" in query:
            return {"results": {"bindings": self.params}}
        if "NamedRule" in query:
            return {"results": {"bindings": self.rules}}
        return {"results": {"bindings": []}}

    def _count(self, query: str) -> int:
        negated = "NOT EXISTS" in query
        head = query.split("FILTER NOT EXISTS")[0]
        from_scratch = "rule-scratch" in head
        if from_scratch:
            return self.counts["added"] if negated else self.counts["constructed"]
        return self.counts["removed"] if negated else self.counts["target_size"]

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        if self.construct_error is not None:
            raise self.construct_error
        self.constructed.append(query)
        return self.turtle

    async def update(self, conn, update):
        self.updates.append(" ".join(update.split()))

    async def get_graph(self, conn, graph_iri):
        return ""

    async def post_graph(self, conn, graph_iri, turtle):
        self.pushed.append((graph_iri, turtle))

    async def put_graph(self, conn, graph_iri, turtle):
        return None

    async def drop_graph(self, conn, graph_iri):
        self.dropped.append(graph_iri)

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        return ""

    async def ping(self, conn):
        return True

    async def aclose(self):
        return None

    # helpers
    def updates_matching(self, keyword: str) -> list[str]:
        return [u for u in self.updates if u.upper().startswith(keyword.upper())]


@pytest.fixture
def stub() -> RuleStub:
    return RuleStub()


@pytest.fixture
def client(stub: RuleStub):
    app = create_app(Settings(bearer_token=TOKEN, named_query_ttl=0.0))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        yield test_client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def run(client, rule_id: str, **body):
    return client.post(f"/named-rule/{rule_id}/run", json=body, headers=auth())


# --- loading ------------------------------------------------------------------


def test_rules_load_in_declared_order(client):
    body = client.get("/named-rules", headers=auth()).json()
    ids = [r["id"] for r in body["rules"]]
    assert ids[:3] == ["append-derived", "rdfs-subclass-inference", "replace-snapshot"]


def test_rules_with_no_usable_order_run_last(client):
    body = client.get("/named-rules", headers=auth()).json()
    ids = [r["id"] for r in body["rules"]]
    # a rule whose order will not parse must not preempt ones that declare one
    assert ids.index("odd-order") > ids.index("not-a-construct")
    assert next(r for r in body["rules"] if r["id"] == "odd-order")["order"] is None


def test_rule_without_a_target_graph_is_skipped_with_a_warning(client):
    body = client.get("/named-rules", headers=auth()).json()
    assert "homeless" not in [r["id"] for r in body["rules"]]
    assert any("no target graph" in w for w in body["warnings"])


def test_unparseable_order_warns(client):
    body = client.get("/named-rules", headers=auth()).json()
    assert any("not an integer" in w for w in body["warnings"])


def test_status_filter(client):
    body = client.get("/named-rules", params={"rule_status": "Suspended"}, headers=auth()).json()
    assert [r["id"] for r in body["rules"]] == ["old"]


def test_unknown_rule_lists_what_is_available(client):
    response = client.get("/named-rule/nope", headers=auth())
    assert response.status_code == 404
    assert "squad" in response.json()["detail"]["available"]


# --- status gating ------------------------------------------------------------


def test_suspended_rule_is_refused(client, stub):
    response = run(client, "old")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "rule_not_active"
    assert stub.constructed == []


def test_deprecated_rule_is_refused(client):
    assert run(client, "gone").status_code == 409


def test_non_construct_body_is_refused(client, stub):
    response = run(client, "not-a-construct")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "rule_error"
    assert stub.updates == []


# --- write modes --------------------------------------------------------------


def test_append_adds_without_removing(client, stub):
    body = run(client, "append-derived").json()
    assert body["writeMode"] == "Append"
    assert body["triplesWritten"] == 7
    assert body["triplesAdded"] == 3
    assert body["triplesRemoved"] == 0
    assert stub.updates_matching("ADD SILENT")
    assert not stub.updates_matching("DELETE")
    assert not stub.updates_matching("COPY")


def test_replace_copies_over_the_target(client, stub):
    body = run(client, "replace-snapshot").json()
    assert body["writeMode"] == "Replace"
    assert body["triplesRemoved"] == 5  # whatever the target held
    copies = stub.updates_matching("COPY SILENT")
    assert copies and copies[0].endswith("TO <urn:ds:snapshot>")


def test_sync_deletes_stale_then_inserts_new(client, stub):
    body = run(client, "rdfs-subclass-inference").json()
    assert body["writeMode"] == "Sync"
    assert body["triplesAdded"] == 3
    assert body["triplesRemoved"] == 2

    deletes = stub.updates_matching("DELETE")
    inserts = stub.updates_matching("INSERT")
    assert deletes and inserts
    # stale triples go before new ones, so a re-derived triple is not churned
    assert stub.updates.index(deletes[0]) < stub.updates.index(inserts[0])
    assert "urn:ds:inferred" in deletes[0]


def test_write_mode_can_be_overridden_per_run(client, stub):
    body = run(client, "append-derived", write_mode="Replace").json()
    assert body["writeMode"] == "Replace"
    assert stub.updates_matching("COPY SILENT")


def test_construct_output_never_goes_through_a_local_parser(client, stub):
    stub.turtle = "<< :a :b :c >> :certainty 0.9 .\n"  # RDF 1.2, rdflib cannot read it
    body = run(client, "append-derived").json()
    assert body["executed"] is True
    assert stub.pushed[0][1] == stub.turtle


def test_scratch_graph_is_always_dropped(client, stub):
    run(client, "append-derived")
    assert any("rule-scratch" in g for g in stub.dropped)


def test_scratch_graph_is_dropped_even_when_the_construct_fails(client, stub):
    stub.construct_error = FusekiTimeout(60.0)
    response = run(client, "append-derived")
    assert response.status_code == 504
    assert any("rule-scratch" in g for g in stub.dropped)


# --- parameters ---------------------------------------------------------------


def test_placeholders_are_substituted_with_the_declared_datatype(client, stub):
    run(client, "squad", params={"team": "https://example.org/team/northbridge"})
    assert "{{team}}" not in stub.constructed[0]
    assert "<https://example.org/team/northbridge>" in stub.constructed[0]


def test_unresolved_placeholder_is_a_client_error(client, stub):
    response = run(client, "squad", params={})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "parameter_error"
    assert stub.constructed == []


def test_focus_node_binds_by_values_not_by_pasting(client, stub):
    run(client, "focus", params={"$this": "https://example.org/holon/earth"})
    sparql = stub.constructed[0]
    assert sparql.strip().endswith("VALUES $this { <https://example.org/holon/earth> }")
    # the body's own $this tokens are untouched
    assert "CONSTRUCT { $this <urn:derived> ?o }" in sparql


def test_rule_without_a_focus_binding_runs_unbound(client, stub):
    run(client, "focus")
    assert "VALUES" not in stub.constructed[0]


def test_focus_must_be_an_iri(client):
    response = run(client, "focus", params={"$this": "not-an-iri"})
    assert response.status_code == 400


def test_dry_run_binds_without_touching_the_backend(client, stub):
    body = run(
        client, "squad", params={"team": "https://example.org/t"}, dry_run=True
    ).json()
    assert body["executed"] is False
    assert "<https://example.org/t>" in body["sparql"]
    assert stub.constructed == [] and stub.updates == []


# --- run all ------------------------------------------------------------------


def test_run_all_fires_active_rules_and_skips_the_rest(client, stub):
    body = client.post(
        "/named-rules/run", json={"params": {"team": "https://example.org/t"}}, headers=auth()
    ).json()
    assert body["pass"] == "single"
    ran = [r["ruleId"] for r in body["results"]]
    assert "old" not in ran and "gone" not in ran
    assert ran.index("append-derived") < ran.index("rdfs-subclass-inference")


def test_run_all_stops_on_the_first_error_by_default(client):
    body = client.post("/named-rules/run", json={}, headers=auth()).json()
    # 'squad' needs a team parameter, so it fails and halts the pass
    assert body["errors"]
    assert body["errors"][0]["ruleId"] == "squad"


def test_run_all_can_continue_past_errors(client):
    body = client.post(
        "/named-rules/run", json={"stop_on_error": False}, headers=auth()
    ).json()
    assert len(body["errors"]) >= 2  # squad and not-a-construct


# --- graph-op -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("clear", "CLEAR SILENT GRAPH <urn:ds:x>"),
        ("drop", "DROP SILENT GRAPH <urn:ds:x>"),
        ("create", "CREATE SILENT GRAPH <urn:ds:x>"),
    ],
)
def test_single_graph_operations(client, stub, operation, expected):
    body = client.post(
        "/graph-op", json={"operation": operation, "target": "urn:ds:x"}, headers=auth()
    ).json()
    assert body["update"] == expected


@pytest.mark.parametrize("operation", ["copy", "move", "add"])
def test_two_graph_operations(client, operation):
    body = client.post(
        "/graph-op",
        json={"operation": operation, "source": "urn:ds:a", "target": "urn:ds:b"},
        headers=auth(),
    ).json()
    assert body["update"] == f"{operation.upper()} SILENT <urn:ds:a> TO <urn:ds:b>"


def test_two_graph_operations_require_a_source(client):
    response = client.post(
        "/graph-op", json={"operation": "copy", "target": "urn:ds:b"}, headers=auth()
    )
    assert response.status_code == 400


def test_silent_can_be_switched_off(client):
    body = client.post(
        "/graph-op",
        json={"operation": "drop", "target": "urn:ds:x", "silent": False},
        headers=auth(),
    ).json()
    assert body["update"] == "DROP GRAPH <urn:ds:x>"


def test_unknown_operation_is_rejected(client):
    response = client.post(
        "/graph-op", json={"operation": "obliterate", "target": "urn:ds:x"}, headers=auth()
    )
    assert response.status_code == 422
