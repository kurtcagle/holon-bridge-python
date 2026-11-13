"""Registering, re-statusing and deleting named triggers, and the access
checks that make that safe.

A trigger acts later, as the bridge, with nobody calling it, so everything the
caller must be allowed to do is checked when it is registered. The stub below
puts the identity/ACL answers (``IdentityStub``) in front of the trigger,
query and rule rows ``test_triggers.TriggerStub`` already serves, and adds one
knob the shared stub lacks: refusing write grants for particular graphs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from rdflib import Graph, Literal, URIRef

from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.server import create_app
from holonbridge.trigger_registry import (
    InvalidTrigger,
    render_trigger_turtle,
    validate_watched_predicates,
)
from holonbridge.triggers import TRIGGER_STATE, evaluate_triggers, load_named_triggers

from ._identity import IdentityStub, auth, row
from .test_triggers import HB, HEV, TriggerStub

TOKEN = "test-token"
REGISTRY = "urn:ds:named-triggers"
CANDIDATES = "urn:ds:candidates"
LOG_TARGET = "urn:ds:log-target"
WIPE_TARGET = "urn:ds:wipe-target"


class RegStub(IdentityStub):
    def __init__(self) -> None:
        super().__init__()
        self.domain = TriggerStub()
        self.updates: list[str] = []
        self.denied_write_graphs: set[str] = set()
        self.denied_replace_graphs: set[str] = set()
        self._extend_registries()

    def _extend_registries(self) -> None:
        def add_query(name: str, sparql: str) -> None:
            iri = f"urn:ds:named-query:{name}"
            self.domain.query_rows += [
                row(q=iri, type=f"{HB}NamedQuery", p=f"{HB}id", o=name),
                row(q=iri, type=f"{HB}NamedQuery", p=f"{HB}sparql", o=sparql),
            ]

        add_query("not-a-select", "CONSTRUCT { ?focus a <urn:t:X> } WHERE { ?focus a <urn:t:Y> }")
        add_query("reads-any-graph", "SELECT ?focus WHERE { GRAPH ?g { ?focus a <urn:t:Y> } }")
        add_query("limited", "SELECT ?focus WHERE { ?focus a <urn:t:Y> } LIMIT 5")

        def add_rule(name: str, construct: str, target: str, mode: str) -> None:
            iri = f"urn:ds:named-rule:{name}"
            for key, value in {
                "id": name, "construct": construct, "targetGraph": target,
                "writeMode": mode, "ruleStatus": "Active",
            }.items():
                self.domain.rule_rows.append(
                    row(r=iri, type=f"{HB}NamedRule", p=f"{HB}{key}", o=value)
                )

        add_rule("wipe-and-replace", "CONSTRUCT { $this a <urn:t:Z> } WHERE { }", WIPE_TARGET, "Replace")
        add_rule("not-a-construct", "SELECT ?x WHERE { ?x ?p ?o }", LOG_TARGET, "Append")

    async def select(self, conn, query, *, default_graph=None):
        for graph in self.denied_write_graphs:
            if "grantsWrite" in query and graph in query and not self.admin:
                return {"boolean": False}
        for graph in self.denied_replace_graphs:
            if "grantsReplace" in query and graph in query and not self.admin:
                return {"boolean": False}
        infra = self.infra_select(query)
        if infra is not None:
            return infra
        return await self.domain.select(conn, query, default_graph=default_graph)

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        return await self.domain.construct(conn, query, default_graph=default_graph)

    async def update(self, conn, update):
        self.updates.append(" ".join(update.split()))
        await self.domain.update(conn, update)

    async def post_graph(self, conn, graph_iri, turtle):
        return await self.domain.post_graph(conn, graph_iri, turtle)

    async def put_graph(self, conn, graph_iri, turtle):
        return None

    async def get_graph(self, conn, graph_iri):
        return await self.domain.get_graph(conn, graph_iri)

    async def drop_graph(self, conn, graph_iri):
        return None

    async def aclose(self):
        return None


@pytest.fixture
def stub() -> RegStub:
    return RegStub()


@pytest.fixture
def client(stub):
    app = create_app(Settings(bearer_token=TOKEN))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        yield test_client


def hdrs() -> dict[str, str]:
    return auth(TOKEN)


REVIEWED = {
    "id": "fresh",
    "trigger_kind": "StateTrigger",
    "condition": "select-underage-varsity",
    "on_fire": "flag-violation",
}
AUTO = {
    "id": "fresh-auto",
    "trigger_kind": "StateTrigger",
    "condition": "select-something",
    "on_fire": "log-rule",
    "review_required": False,
}


def register(client, body, headers=None):
    return client.post("/named-trigger", json=body, headers=headers or hdrs())


# --- the happy paths ------------------------------------------------------------


def test_a_reviewed_trigger_can_be_registered(client, stub):
    response = register(client, REVIEWED)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and body["iri"] == "urn:ds:named-trigger:fresh"
    assert body["reviewRequired"] is True
    (update,) = stub.updates
    assert update.index("DELETE WHERE") < update.index("INSERT DATA")
    for needle in (REGISTRY, "NamedTrigger", '"select-underage-varsity"', '"flag-violation"', '"StateTrigger"'):
        assert needle in update


def test_an_auto_executing_trigger_can_be_registered_by_a_writer(client, stub):
    response = register(client, AUTO)
    assert response.status_code == 200, response.text
    assert response.json()["reviewRequired"] is False
    assert '"false"^^<http://www.w3.org/2001/XMLSchema#boolean>' in stub.updates[0]


def test_watched_predicates_are_stored(client, stub):
    body = {**REVIEWED, "watched_predicates": ["https://example.org/p", "https://example.org/p"]}
    assert register(client, body).status_code == 200
    assert stub.updates[0].count("<https://example.org/p>") == 1


# --- identity and the registry grant ----------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/named-trigger", REVIEWED),
        ("post", "/named-trigger/auto-log/status", {"status": "Suspended"}),
        ("delete", "/named-trigger/auto-log", None),
        ("post", "/named-trigger/auto-log/evaluate", {}),
    ],
)
def test_these_routes_need_an_identity(client, method, path, body):
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(path, headers={"Authorization": f"Bearer {TOKEN}"}, **kwargs)
    assert response.status_code == 401


def test_registering_needs_write_on_the_registry(client, stub):
    stub.denied_write_graphs = {REGISTRY}
    response = register(client, REVIEWED)
    assert response.status_code == 403
    assert REGISTRY in response.json()["detail"]
    assert stub.updates == []


# --- what the condition and rule must be ------------------------------------------


def test_an_unknown_condition_is_reported_with_the_known_ones(client, stub):
    response = register(client, {**REVIEWED, "condition": "nope"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_condition"
    assert "select-underage-varsity" in detail["available"]
    assert stub.updates == []


def test_an_unknown_rule_is_reported(client, stub):
    response = register(client, {**REVIEWED, "on_fire": "nope"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "unknown_rule"


def test_a_condition_outside_the_callers_toolset_looks_unknown(client, stub):
    hidden = "urn:ds:named-query:select-underage-varsity"
    stub.restricted_tools = {hidden}
    response = register(client, REVIEWED)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_condition"
    assert "select-underage-varsity" not in detail["available"]


def test_a_condition_must_be_a_select(client):
    response = register(client, {**REVIEWED, "condition": "not-a-select"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "condition_not_select"


def test_a_rule_must_construct(client):
    response = register(client, {**REVIEWED, "on_fire": "not-a-construct"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "rule_not_construct"


def test_a_limit_on_the_condition_is_warned_about(client):
    response = register(client, {**REVIEWED, "condition": "limited"})
    assert response.status_code == 200
    assert any("LIMIT" in w for w in response.json()["warnings"])


def test_invalid_kind_and_predicates_are_refused(client, stub):
    assert register(client, {**REVIEWED, "trigger_kind": "Nope"}).status_code == 400
    bad = register(client, {**REVIEWED, "watched_predicates": ["not an iri>"]})
    assert bad.status_code == 400
    assert stub.updates == []


# --- definer's rights: the caller must be able to do what the trigger will ----------


def test_a_condition_that_reads_graphs_it_does_not_name_is_refused_to_non_admins(client, stub):
    body = {**REVIEWED, "condition": "reads-any-graph"}
    denied = register(client, body)
    assert denied.status_code == 403
    assert denied.json()["detail"]["error"] == "condition_not_readable"
    assert stub.updates == []

    stub.admin = True
    assert register(client, body).status_code == 200


def test_a_reviewed_trigger_needs_write_on_the_candidates_graph(client, stub):
    stub.denied_write_graphs = {CANDIDATES}
    assert register(client, REVIEWED).status_code == 403
    assert stub.updates == []


def test_an_auto_executing_trigger_needs_write_on_the_rules_target(client, stub):
    stub.denied_write_graphs = {LOG_TARGET}
    denied = register(client, AUTO)
    assert denied.status_code == 403
    assert LOG_TARGET in denied.json()["detail"]
    assert stub.updates == []


def test_the_same_rule_is_fine_when_a_human_reviews_what_it_proposes(client, stub):
    stub.denied_write_graphs = {LOG_TARGET}
    reviewed = {**AUTO, "review_required": True}
    assert register(client, reviewed).status_code == 200


def test_a_wholesale_rule_unreviewed_needs_replace_as_well(client, stub):
    body = {**AUTO, "on_fire": "wipe-and-replace"}
    stub.denied_replace_graphs = {WIPE_TARGET}
    denied = register(client, body)
    assert denied.status_code == 403
    assert stub.updates == []

    stub.denied_replace_graphs = set()
    assert register(client, body).status_code == 200


def test_a_wholesale_rule_is_fine_reviewed_because_approval_only_merges(client, stub):
    stub.denied_replace_graphs = {WIPE_TARGET}
    assert register(client, {**REVIEWED, "on_fire": "wipe-and-replace"}).status_code == 200


# --- status ---------------------------------------------------------------------


def test_suspending_needs_only_the_registry_grant(client, stub):
    stub.denied_write_graphs = {LOG_TARGET}  # would block activating "auto-log"
    response = client.post("/named-trigger/auto-log/status", json={"status": "Suspended"}, headers=hdrs())
    assert response.status_code == 200, response.text
    assert "Suspended" in stub.updates[-1]
    assert "DELETE" in stub.updates[-1] and "triggerStatus" in stub.updates[-1]


def test_activating_rechecks_what_the_trigger_will_do(client, stub):
    stub.denied_write_graphs = {LOG_TARGET}
    denied = client.post("/named-trigger/auto-log/status", json={"status": "Active"}, headers=hdrs())
    assert denied.status_code == 403
    assert stub.updates == []

    stub.denied_write_graphs = set()
    assert client.post("/named-trigger/auto-log/status", json={"status": "Active"}, headers=hdrs()).status_code == 200


def test_status_of_an_unknown_trigger_is_404(client):
    assert client.post("/named-trigger/nope/status", json={"status": "Suspended"}, headers=hdrs()).status_code == 404


def test_status_needs_the_registry_grant(client, stub):
    stub.denied_write_graphs = {REGISTRY}
    assert client.post("/named-trigger/auto-log/status", json={"status": "Suspended"}, headers=hdrs()).status_code == 403


# --- delete ---------------------------------------------------------------------


def test_deleting_needs_replace_on_the_registry(client, stub):
    stub.denied_replace_graphs = {REGISTRY}
    assert client.delete("/named-trigger/auto-log", headers=hdrs()).status_code == 403
    assert stub.updates == []

    stub.denied_replace_graphs = set()
    response = client.delete("/named-trigger/auto-log", headers=hdrs())
    assert response.status_code == 200
    assert stub.updates[-1].startswith("DELETE WHERE") and "auto-log" in stub.updates[-1]


def test_deleting_an_unknown_trigger_is_404(client):
    assert client.delete("/named-trigger/nope", headers=hdrs()).status_code == 404


# --- manual evaluation ------------------------------------------------------------


def test_evaluating_one_trigger_does_not_fire_the_others_of_its_kind(client, stub):
    response = client.post("/named-trigger/auto-log/evaluate", json={}, headers=hdrs())
    assert response.status_code == 200, response.text
    body = response.json()
    assert {f["triggerId"] for f in body["firings"]} == {"auto-log"}
    # "min-age-violation" is also an Active StateTrigger; it must not have run.
    assert stub.domain._candidates == {}


def test_evaluating_an_auto_executing_trigger_needs_write_on_its_target(client, stub):
    stub.denied_write_graphs = {LOG_TARGET}
    assert client.post("/named-trigger/auto-log/evaluate", json={}, headers=hdrs()).status_code == 403


def test_evaluating_a_reviewed_trigger_needs_write_on_the_candidates_graph(client, stub):
    stub.denied_write_graphs = {CANDIDATES}
    response = client.post("/named-trigger/min-age-violation/evaluate", json={}, headers=hdrs())
    assert response.status_code == 403


async def test_evaluate_triggers_can_be_restricted_to_chosen_ids():
    domain = TriggerStub()
    conn = Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")
    firings = await evaluate_triggers(
        domain, conn, kind=TRIGGER_STATE, trigger_ids=frozenset({"auto-log"})
    )
    assert {f.trigger_id for f in firings} == {"auto-log"}


# --- the stored shape is the loaded shape -------------------------------------------


class _RowsClient:
    def __init__(self, rows):
        self.rows = rows

    async def select(self, conn, query, *, default_graph=None):
        if "SELECT ?t ?pred" in query:
            return {"results": {"bindings": [
                row(t=r["t"]["value"], pred=r["o"]["value"])
                for r in self.rows if r["p"]["value"].endswith("watchedPredicate")
            ]}}
        return {"results": {"bindings": self.rows}}


async def test_what_is_written_is_what_the_loader_reads_back():
    turtle = render_trigger_turtle(
        node="urn:ds:named-trigger:rt",
        trigger_id="rt",
        trigger_kind="TemporalTrigger",
        condition="c1",
        on_fire="r1",
        review_required=False,
        status="Suspended",
        label='A "quoted" label',
        description="line one\nline two",
        watched_predicates=["https://example.org/p"],
        registered_at="2026-11-01T00:00:00+00:00",
    )
    graph = Graph().parse(data=turtle, format="turtle")
    type_iri = next(graph.objects(URIRef("urn:ds:named-trigger:rt"), URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")))
    rows = [
        {"t": {"value": str(s)}, "type": {"value": str(type_iri)}, "p": {"value": str(p)},
         "o": {"value": str(o)}}
        for s, p, o in graph if str(p) != "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    ]
    conn = Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")
    loaded = await load_named_triggers(_RowsClient(rows), conn)

    (trigger,) = loaded.triggers
    assert loaded.warnings == []
    assert (trigger.id, trigger.trigger_kind, trigger.condition_id, trigger.on_fire_rule_id) == (
        "rt", "TemporalTrigger", "c1", "r1"
    )
    assert trigger.review_required is False and trigger.status == "Suspended"
    assert trigger.label == 'A "quoted" label' and trigger.description == "line one\nline two"
    assert trigger.watched_predicates == frozenset({"https://example.org/p"})


def test_validation_helpers():
    assert validate_watched_predicates(["urn:a:b", "urn:a:b"]) == ["urn:a:b"]
    for bad in ("", "relative/path", "http://x/ y", "http://x/>", 'http://x/"'):
        with pytest.raises(InvalidTrigger):
            validate_watched_predicates([bad])
    with pytest.raises(InvalidTrigger):
        render_trigger_turtle(
            node="urn:n", trigger_id="x", trigger_kind="Nope", condition="c", on_fire="r",
            review_required=True, status="Active", label=None, description=None,
            watched_predicates=[], registered_at="2026-11-01T00:00:00+00:00",
        )


# --- approve and reject over HTTP keep the decision record -----------------------


async def _stage_candidate(stub: RegStub) -> str:
    from holonbridge.conn import Conn as _Conn

    conn = _Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")
    await evaluate_triggers(stub.domain, conn, kind=TRIGGER_STATE)
    iri = next(iter(stub.domain._candidates))
    return iri.rsplit(":", 1)[1]


def test_approve_over_http_records_the_reviewer_and_the_reason(client, stub):
    import asyncio

    stub.admin = True
    cid = asyncio.run(_stage_candidate(stub))

    response = client.post(f"/candidate/{cid}/approve", json={"reason": "looks right"}, headers=hdrs())

    assert response.status_code == 200, response.text
    record = next(iter(stub.domain._candidates.values()))
    assert record["candidateStatusValue"] == "Approved"
    assert record["approvedBy"]
    assert record["decisionReason"] == "looks right"


def test_reject_over_http_works_without_a_body(client, stub):
    import asyncio

    stub.admin = True
    cid = asyncio.run(_stage_candidate(stub))

    response = client.post(f"/candidate/{cid}/reject", headers=hdrs())

    assert response.status_code == 200, response.text
    record = next(iter(stub.domain._candidates.values()))
    assert record["candidateStatusValue"] == "Rejected"
    assert record["rejectedBy"]
    assert "decisionReason" not in record


def test_a_reason_over_1000_characters_is_a_422_and_the_candidate_stays_pending(client, stub):
    import asyncio

    stub.admin = True
    cid = asyncio.run(_stage_candidate(stub))

    response = client.post(f"/candidate/{cid}/approve", json={"reason": "x" * 1001}, headers=hdrs())

    assert response.status_code == 422
    assert next(iter(stub.domain._candidates.values()))["candidateStatusValue"] == "Pending"


# --- fluent-update triggers (hev:onFireFluentQuery) -------------------------------

_FLUENT_CONN = Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")
EVENTS = _FLUENT_CONN.graph("events")
SCENE = _FLUENT_CONN.scene_graph
SEQUENCES = _FLUENT_CONN.graph("sequences")

FLUENT = {
    "id": "tick",
    "trigger_kind": "StateTrigger",
    "condition": "select-something",
    "on_fire_fluent_query": "advance-clock",
    "review_required": False,
}


@pytest.fixture
def fluent_stub(stub) -> RegStub:
    iri = "urn:ds:named-query:advance-clock"
    stub.domain.query_rows += [
        row(q=iri, type=f"{HB}NamedQuery", p=f"{HB}id", o="advance-clock"),
        row(
            q=iri,
            type=f"{HB}NamedQuery",
            p=f"{HB}sparql",
            o="SELECT ?fluent ?value WHERE { BIND(<urn:ds:fluent:clock> AS ?fluent) BIND(1 AS ?value) }",
        ),
    ]
    return stub


def test_a_fluent_trigger_can_be_registered_by_someone_who_may_write_fluents(client, fluent_stub):
    response = register(client, FLUENT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and body["reviewRequired"] is False
    (update,) = fluent_stub.updates
    assert "onFireFluentQuery" in update and '"advance-clock"' in update
    assert "onFire>" not in update.replace("onFireFluentQuery", "")
    # Written with the definer, so firings can run as them.
    assert "armedBy" in update


@pytest.mark.parametrize("graph", [EVENTS, SCENE, SEQUENCES])
def test_a_fluent_trigger_needs_write_on_the_events_scene_and_sequences_graphs(
    client, fluent_stub, graph
):
    fluent_stub.denied_write_graphs = {graph}
    denied = register(client, FLUENT)
    assert denied.status_code == 403
    assert graph in denied.json()["detail"]
    assert fluent_stub.updates == []


def test_a_fluent_trigger_cannot_be_reviewed(client, fluent_stub):
    response = register(client, {**FLUENT, "review_required": True})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "fluent_action_cannot_be_reviewed"
    assert fluent_stub.updates == []


def test_a_fluent_trigger_is_unreviewed_even_if_review_is_left_at_its_default(client, fluent_stub):
    body = {k: v for k, v in FLUENT.items() if k != "review_required"}
    response = register(client, body)
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "fluent_action_cannot_be_reviewed"


def test_a_trigger_names_exactly_one_action(client, fluent_stub):
    both = register(client, {**FLUENT, "on_fire": "log-rule"})
    neither = register(client, {k: v for k, v in FLUENT.items() if k != "on_fire_fluent_query"})
    for response in (both, neither):
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "invalid_trigger"
    assert fluent_stub.updates == []


def test_an_unknown_action_query_is_reported_with_the_known_ones(client, fluent_stub):
    response = register(client, {**FLUENT, "on_fire_fluent_query": "no-such-query"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_action_query"
    assert "advance-clock" in detail["available"]


def test_an_action_query_must_be_a_select(client, fluent_stub):
    response = register(client, {**FLUENT, "on_fire_fluent_query": "not-a-select"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "action_query_not_bindable"
    assert fluent_stub.updates == []


def _render(**overrides) -> str:
    args = dict(
        node="urn:ds:named-trigger:t", trigger_id="t", trigger_kind="StateTrigger",
        condition="select-something", on_fire=None, review_required=False, status="Active",
        label=None, description=None, watched_predicates=[],
        registered_at="2026-11-13T00:00:00+00:00", armed_by="urn:ds:person:definer",
        fluent_query="advance-clock",
    )
    args.update(overrides)
    return render_trigger_turtle(**args)


def test_the_registry_renderer_writes_the_fluent_action():
    graph = Graph()
    graph.parse(data=_render(), format="turtle")
    objects = {str(o) for s, p, o in graph if str(p).endswith("onFireFluentQuery")}
    assert objects == {"advance-clock"}
    assert not any(str(p).endswith("/onFire") for _, p, _ in graph)


def test_the_registry_renderer_refuses_a_reviewed_fluent_trigger_too():
    with pytest.raises(InvalidTrigger, match="reviewRequired=false"):
        _render(review_required=True)


def test_the_registry_renderer_refuses_two_actions():
    with pytest.raises(InvalidTrigger, match="exactly one action"):
        _render(on_fire="log-rule")


def test_activating_a_fluent_trigger_rechecks_the_grants(client, fluent_stub):
    assert register(client, FLUENT).status_code == 200
    fluent_stub.domain.trigger_rows += [
        row(t="urn:ds:named-trigger:tick", type=f"{HEV}NamedTrigger", p=f"{HEV}{k}", o=v)
        for k, v in {
            "id": "tick", "triggerKind": "StateTrigger", "condition": "select-something",
            "onFireFluentQuery": "advance-clock", "reviewRequired": "false",
            "triggerStatus": "Suspended",
        }.items()
    ]
    fluent_stub.denied_write_graphs = {SCENE}
    response = client.post(
        "/named-trigger/tick/status", json={"status": "Active"}, headers=hdrs()
    )
    assert response.status_code == 403


def test_evaluating_a_fluent_trigger_needs_write_on_the_fluent_graphs(client, fluent_stub):
    fluent_stub.domain.trigger_rows += [
        row(t="urn:ds:named-trigger:tick", type=f"{HEV}NamedTrigger", p=f"{HEV}{k}", o=v)
        for k, v in {
            "id": "tick", "triggerKind": "StateTrigger", "condition": "select-something",
            "onFireFluentQuery": "advance-clock", "reviewRequired": "false",
            "triggerStatus": "Active", "armedBy": "urn:ds:person:definer",
        }.items()
    ]
    fluent_stub.denied_write_graphs = {SEQUENCES}
    denied = client.post("/named-trigger/tick/evaluate", json={}, headers=hdrs())
    assert denied.status_code == 403
