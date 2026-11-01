"""The post-write hook, the touched-predicate derivation behind it, and the
definer re-check at firing.

The hook is tested end to end through the real routes with a spy standing in for
``evaluate_triggers``: what matters at each write path is *that* it is called,
with what it knew about the write, and that nothing about it can break the write.
The re-check is tested against ``evaluate_triggers`` directly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from holonbridge import triggers as triggers_mod
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiError
from holonbridge.server import create_app
from holonbridge.touched import turtle_predicates, update_predicates
from holonbridge.trigger_hook import after_write, only_system_graphs
from holonbridge.triggers import (
    TRIGGER_STATE,
    evaluate_triggers,
    list_candidates,
    load_named_triggers,
)

from ._identity import PERSON_IRI, auth, row
from .test_trigger_registration import RegStub
from .test_triggers import HB, HEV

TOKEN = "test-token"
CANDIDATES = "urn:ds:candidates"


def conn() -> Conn:
    return Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")


# --- touched predicates -----------------------------------------------------------


def test_turtle_predicates():
    assert turtle_predicates("<urn:a> <urn:p> 1 ; a <urn:T> .") == {
        "urn:p", "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    }
    assert turtle_predicates("") == frozenset()


@pytest.mark.parametrize("text", ["not turtle {", "<urn:a> <urn:p> <<( <urn:b> <urn:q> 2 )>> ."])
def test_unreadable_turtle_is_unknown_not_empty(text):
    assert turtle_predicates(text) is None


def test_update_predicates_come_from_the_templates_only():
    assert update_predicates(
        "DELETE { ?s <urn:old> ?o } INSERT { ?s <urn:new> ?o } WHERE { ?s <urn:read> ?o }"
    ) == {"urn:old", "urn:new"}
    assert update_predicates(
        "INSERT DATA { GRAPH <urn:g> { <urn:a> <urn:p> 1 } } ; DELETE WHERE { ?s <urn:z> ?o }"
    ) == {"urn:p", "urn:z"}


@pytest.mark.parametrize(
    "text",
    [
        "INSERT { ?s ?p ?o } WHERE { ?s ?p ?o }",  # variable predicate
        "DELETE WHERE { ?s ?p ?o }",
        "CLEAR GRAPH <urn:g>",
        "DROP GRAPH <urn:g>",
        "COPY <urn:a> TO <urn:b>",
        "this is not sparql",
    ],
)
def test_updates_that_touch_unknown_predicates_are_unknown(text):
    assert update_predicates(text) is None


# --- the hook itself -------------------------------------------------------------


@pytest.fixture
def spy(monkeypatch):
    calls: list[dict] = []

    async def fake(client, conn, *, kind, touched_predicates=None, trigger_ids=None):
        calls.append({"kind": kind, "touched": touched_predicates, "ids": trigger_ids})
        return []

    monkeypatch.setattr(triggers_mod, "evaluate_triggers", fake)
    return calls


def test_only_bookkeeping_graphs_are_system_graphs():
    c = conn()
    assert only_system_graphs(c, [c.graph("candidates"), c.graph("trigger-log")])
    assert not only_system_graphs(c, [c.graph("candidates"), c.graph("holons")])
    assert not only_system_graphs(c, ["urn:some:user:graph"])
    assert not only_system_graphs(c, None)
    assert not only_system_graphs(c, [])


async def test_a_write_to_domain_data_evaluates_state_triggers(spy):
    await after_write(None, conn(), graphs=["urn:ds:holons"], touched=frozenset({"urn:p"}))
    assert spy == [{"kind": TRIGGER_STATE, "touched": frozenset({"urn:p"}), "ids": None}]


async def test_a_write_to_bookkeeping_alone_does_not(spy):
    await after_write(None, conn(), graphs=[CANDIDATES])
    assert spy == []


async def test_unknown_graphs_still_evaluate(spy):
    await after_write(None, conn(), graphs=None)
    assert len(spy) == 1


async def test_a_failing_evaluation_is_swallowed(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("trigger blew up")

    monkeypatch.setattr(triggers_mod, "evaluate_triggers", boom)
    await after_write(None, conn(), graphs=["urn:ds:holons"])  # must not raise


# --- evaluate_triggers: lazy narrowing and re-entrancy ---------------------------------


async def test_touched_is_only_worked_out_when_some_trigger_watches_predicates():
    stub = RegStub().domain
    stub.watched_rows = []  # nothing declares a watchedPredicate
    called = []

    def lazy():
        called.append(1)
        return frozenset()

    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE, touched_predicates=lazy)
    assert called == []

    stub.watched_rows = [row(t="urn:ds:named-trigger:min-age-violation", pred="urn:watched")]
    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE, touched_predicates=lazy)
    assert called == [1]


async def test_unknown_touched_narrows_nothing():
    stub = RegStub().domain
    firings = await evaluate_triggers(
        stub, conn(), kind=TRIGGER_STATE, touched_predicates=lambda: None
    )
    assert {f.trigger_id for f in firings} >= {"min-age-violation", "auto-log"}


async def test_known_touched_skips_triggers_watching_other_predicates():
    stub = RegStub().domain  # min-age-violation watches rosterMemberOf
    firings = await evaluate_triggers(
        stub, conn(), kind=TRIGGER_STATE, touched_predicates=frozenset({"urn:unrelated"})
    )
    assert "min-age-violation" not in {f.trigger_id for f in firings}
    assert "auto-log" in {f.trigger_id for f in firings}  # watches nothing: always runs

    stub2 = RegStub().domain
    firings = await evaluate_triggers(
        stub2, conn(), kind=TRIGGER_STATE,
        touched_predicates=frozenset({"https://w3id.org/sportsleague/rosterMemberOf"}),
    )
    assert "min-age-violation" in {f.trigger_id for f in firings}


async def test_a_nested_evaluation_does_nothing():
    stub = RegStub().domain
    token = triggers_mod._evaluating.set(True)
    try:
        assert await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE) == []
    finally:
        triggers_mod._evaluating.reset(token)
    assert await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)  # and it resets


async def test_a_firing_does_not_re_enter_the_evaluator_through_its_own_write(monkeypatch):
    """An auto-executing rule writes; that write reaches the hook; the hook must
    not start a second evaluation inside the first."""
    stub = RegStub().domain
    real_update = stub.update
    reentered: list[list] = []

    async def update_then_hook(c, update):
        await real_update(c, update)
        if "log-target" in update or "logged" in update:
            reentered.append(await after_write_nested(stub))

    async def after_write_nested(client):
        return await evaluate_triggers(client, conn(), kind=TRIGGER_STATE)

    monkeypatch.setattr(stub, "update", update_then_hook)
    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert all(result == [] for result in reentered)


# --- every write path calls the hook ------------------------------------------------------


@pytest.fixture
def stub() -> RegStub:
    s = RegStub()
    s.domain.query_rows += [
        row(q="urn:ds:named-query:bump", type=f"{HB}NamedQuery", p=f"{HB}id", o="bump"),
        row(
            q="urn:ds:named-query:bump", type=f"{HB}NamedQuery", p=f"{HB}sparql",
            o="INSERT DATA { GRAPH <urn:ds:holons> { <urn:a> <urn:bumped> true } }",
        ),
    ]
    return s


@pytest.fixture
def client(stub):
    app = create_app(Settings(bearer_token=TOKEN))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        yield test_client


def hdrs():
    return auth(TOKEN)


def test_push_turtle_calls_it_with_the_predicates_it_wrote(client, spy):
    response = client.post(
        "/graph/push",
        json={"turtle": "<urn:a> <urn:p> 1 .", "graph_iri": "urn:ds:holons", "mode": "merge"},
        headers=hdrs(),
    )
    assert response.status_code == 200, response.text
    (call,) = spy
    assert call["kind"] == TRIGGER_STATE
    assert call["touched"]() == {"urn:p"}


def test_a_replace_push_cannot_narrow(client, spy):
    client.post(
        "/graph/push",
        json={"turtle": "<urn:a> <urn:p> 1 .", "graph_iri": "urn:ds:holons", "mode": "replace"},
        headers=hdrs(),
    )
    (call,) = spy
    assert call["touched"] is None  # whatever was there before is gone too


def test_a_refused_write_does_not_call_it(client, stub, spy):
    stub.write_grant = False
    response = client.post(
        "/graph/push",
        json={"turtle": "<urn:a> <urn:p> 1 .", "graph_iri": "urn:ds:holons", "mode": "merge"},
        headers=hdrs(),
    )
    assert response.status_code == 403
    assert spy == []


def test_sparql_update_calls_it(client, spy):
    response = client.post(
        "/sparql/update",
        json={"update": "INSERT DATA { GRAPH <urn:ds:holons> { <urn:a> <urn:p> 1 } }"},
        headers=hdrs(),
    )
    assert response.status_code == 200, response.text
    (call,) = spy
    assert call["touched"]() == {"urn:p"}


def test_sparql_update_of_bookkeeping_only_does_not(client, spy):
    response = client.post(
        "/sparql/update",
        json={"update": f"INSERT DATA {{ GRAPH <{CANDIDATES}> {{ <urn:a> <urn:p> 1 }} }}"},
        headers=hdrs(),
    )
    assert response.status_code == 200, response.text
    assert spy == []


def test_dropping_a_graph_calls_it(client, spy):
    response = client.delete("/graph", params={"iri": "urn:ds:holons"}, headers=hdrs())
    assert response.status_code == 200, response.text
    assert len(spy) == 1


def test_graph_op_calls_it(client, spy):
    response = client.post(
        "/graph-op", json={"operation": "create", "target": "urn:ds:newgraph"}, headers=hdrs()
    )
    assert response.status_code == 200, response.text
    assert len(spy) == 1


def test_running_a_rule_calls_it(client, spy):
    response = client.post("/named-rule/log-rule/run", json={}, headers=hdrs())
    assert response.status_code == 200, response.text
    assert len(spy) == 1


def test_a_dry_run_rule_does_not(client, spy):
    client.post("/named-rule/log-rule/run", json={"dry_run": True}, headers=hdrs())
    assert spy == []


def test_running_a_named_update_calls_it(client, spy):
    response = client.post("/named-query/bump/run", json={}, headers=hdrs())
    assert response.status_code == 200, response.text
    (call,) = spy
    assert call["touched"]() == {"urn:bumped"}


def test_a_named_select_does_not(client, spy):
    client.post("/named-query/select-something/run", json={}, headers=hdrs())
    assert spy == []


def test_a_broken_trigger_does_not_break_the_write(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("trigger blew up")

    monkeypatch.setattr(triggers_mod, "evaluate_triggers", boom)
    response = client.post(
        "/sparql/update",
        json={"update": "INSERT DATA { GRAPH <urn:ds:holons> { <urn:a> <urn:p> 1 } }"},
        headers=hdrs(),
    )
    assert response.status_code == 200


async def test_a_scheduled_payload_task_calls_it(spy):
    from holonbridge.scheduler import Scheduler, Task
    from holonbridge.scheduler.model import FiringRecord

    stub = RegStub()
    sched = Scheduler(stub, admin_conn=conn(), tick_seconds=1000.0)
    task = Task(
        iri="urn:t", id="t", action_class="GraphWrite", status="Active", interval_seconds=1,
        payload="<urn:a> <urn:p> 1 .", target_graph="urn:ds:holons",
    )
    record = FiringRecord(iri="urn:rec", task_iri=task.iri, outcome="")
    await sched._execute(task, None, record)
    assert record.outcome == "committed"
    assert len(spy) == 1


async def test_a_read_only_scheduled_task_does_not(spy):
    from holonbridge.scheduler import Scheduler, Task
    from holonbridge.scheduler.model import FiringRecord

    stub = RegStub()
    sched = Scheduler(stub, admin_conn=conn(), tick_seconds=1000.0)
    task = Task(
        iri="urn:t", id="t", action_class="ReadOnlyQuery", status="Active",
        interval_seconds=1, sparql="SELECT * WHERE { ?s ?p ?o } LIMIT 1",
    )
    await sched._execute(task, None, FiringRecord(iri="urn:rec", task_iri=task.iri, outcome=""))
    assert spy == []


# --- the definer is re-checked at firing -------------------------------------------------


def arm(stub: RegStub, trigger_id: str, person: str = PERSON_IRI) -> None:
    iri = f"urn:ds:named-trigger:{trigger_id}"
    stub.domain.trigger_rows.append(
        row(t=iri, type=f"{HEV}NamedTrigger", p=f"{HEV}armedBy", o=person)
    )


def outcomes(firings, trigger_id):
    return [f.outcome for f in firings if f.trigger_id == trigger_id]


async def test_armed_by_is_loaded():
    stub = RegStub()
    arm(stub, "min-age-violation")
    loaded = await load_named_triggers(stub.domain, conn())
    assert loaded.by_id("min-age-violation").armed_by == PERSON_IRI
    assert loaded.by_id("min-age-violation").summary()["armedBy"] == PERSON_IRI
    assert loaded.by_id("auto-log").armed_by == ""


async def test_a_definer_who_still_has_access_fires_normally():
    stub = RegStub()
    arm(stub, "min-age-violation")
    firings = await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert outcomes(firings, "min-age-violation") == ["proposed"]


async def test_a_definer_who_lost_access_does_not_fire():
    stub = RegStub()
    arm(stub, "min-age-violation")
    stub.denied_write_graphs = {CANDIDATES}
    firings = await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)

    assert outcomes(firings, "min-age-violation") == ["skipped-definer-unauthorised"]
    (skipped,) = [f for f in firings if f.trigger_id == "min-age-violation"]
    assert PERSON_IRI in skipped.detail
    assert await list_candidates(stub.domain, conn()) == []
    # Nothing was recorded in the ledger, so it is not "already handled".
    assert not any("TriggerFiring" in u and "min-age-violation" in u for u in stub.updates)


async def test_it_fires_once_access_is_restored():
    stub = RegStub()
    arm(stub, "min-age-violation")
    stub.denied_write_graphs = {CANDIDATES}
    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)

    stub.denied_write_graphs = set()
    triggers_mod.clear_definer_cache()
    firings = await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert outcomes(firings, "min-age-violation") == ["proposed"]


async def test_an_auto_executing_trigger_stops_when_the_definer_loses_the_target():
    stub = RegStub()
    arm(stub, "auto-log")
    stub.denied_write_graphs = {"urn:ds:log-target"}
    firings = await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert outcomes(firings, "auto-log") == ["skipped-definer-unauthorised"]
    assert not any("logged" in c for c in stub.domain.constructed)


async def test_the_verdict_is_reused_for_a_short_while(monkeypatch):
    stub = RegStub()
    arm(stub, "min-age-violation")
    now = [1000.0]
    monkeypatch.setattr(triggers_mod, "_clock", lambda: now[0])

    grant_checks = lambda: sum(1 for q in stub.asked if "grantsWrite" in q)  # noqa: E731
    stub.asked = []
    real_select = stub.select

    async def counting(c, query, *, default_graph=None):
        stub.asked.append(query)
        return await real_select(c, query, default_graph=default_graph)

    monkeypatch.setattr(stub, "select", counting)

    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    first = grant_checks()
    assert first > 0

    stub.domain.focus_rows["underage"] = []
    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)  # re-arms
    stub.domain.focus_rows["underage"] = ["urn:ds:person:isla"]
    now[0] += triggers_mod.DEFINER_RECHECK_TTL_SECONDS - 1
    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert grant_checks() == first  # served from the cache

    now[0] += 2  # past the TTL
    stub.domain.focus_rows["underage"] = []
    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    stub.domain.focus_rows["underage"] = ["urn:ds:person:isla"]
    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert grant_checks() > first  # asked again


async def test_nothing_is_checked_when_there_is_nothing_to_fire():
    stub = RegStub()
    arm(stub, "min-age-violation")
    stub.domain.focus_rows["underage"] = []
    stub.asked = []
    real_select = stub.select

    async def counting(c, query, *, default_graph=None):
        stub.asked.append(query)
        return await real_select(c, query, default_graph=default_graph)

    stub.select = counting
    await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert not any("grantsWrite" in q for q in stub.asked)


async def test_a_failure_to_check_skips_without_caching_the_failure(monkeypatch):
    stub = RegStub()
    arm(stub, "min-age-violation")
    real_select = stub.select
    broken = [True]

    async def flaky(c, query, *, default_graph=None):
        if broken[0] and "grantsWrite" in query:
            raise FusekiError(500, "store unavailable")
        return await real_select(c, query, default_graph=default_graph)

    monkeypatch.setattr(stub, "select", flaky)
    firings = await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert outcomes(firings, "min-age-violation") == ["skipped-definer-unauthorised"]
    assert "could not verify" in [f for f in firings if f.trigger_id == "min-age-violation"][0].detail

    broken[0] = False
    firings = await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert outcomes(firings, "min-age-violation") == ["proposed"]


async def test_a_hand_authored_trigger_still_fires_by_default():
    stub = RegStub()
    firings = await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert outcomes(firings, "min-age-violation") == ["proposed"]


async def test_require_definer_refuses_one_that_cannot_be_traced(monkeypatch):
    from holonbridge import config

    monkeypatch.setattr(
        config, "get_settings", lambda: config.Settings(triggers_require_definer=True)
    )
    stub = RegStub()
    arm(stub, "min-age-violation")  # this one is traced; the others are not
    firings = await evaluate_triggers(stub, conn(), kind=TRIGGER_STATE)
    assert outcomes(firings, "min-age-violation") == ["proposed"]
    assert outcomes(firings, "auto-log") == ["skipped-definer-unauthorised"]


# --- the definer is recorded by the routes --------------------------------------------------


def test_registering_records_who_armed_it(client, stub):
    response = client.post(
        "/named-trigger",
        json={
            "id": "fresh", "trigger_kind": "StateTrigger",
            "condition": "select-underage-varsity", "on_fire": "flag-violation",
        },
        headers=hdrs(),
    )
    assert response.status_code == 200, response.text
    assert f"<{PERSON_IRI}>" in stub.updates[0] and "armedBy" in stub.updates[0]


def test_activating_hands_responsibility_to_the_activator(client, stub):
    response = client.post(
        "/named-trigger/auto-log/status", json={"status": "Active"}, headers=hdrs()
    )
    assert response.status_code == 200, response.text
    assert f"<{PERSON_IRI}>" in stub.updates[-1] and "armedBy" in stub.updates[-1]


def test_suspending_does_not_touch_it(client, stub):
    client.post("/named-trigger/auto-log/status", json={"status": "Suspended"}, headers=hdrs())
    assert "armedBy" not in stub.updates[-1].split("INSERT DATA")[1]
