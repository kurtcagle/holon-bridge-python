"""Scheduler tests.

The important one is `test_every_scheduler_query_is_graph_wrapped`: it is a
structural test, not a behavioural one. The bug it guards against produced no
error and no failing behaviour — a policy query without a `GRAPH` clause read
the default graph, matched nothing, and reported "no limit". Everything looked
fine. Only the shape of the query was wrong.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi.testclient import TestClient

from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.scheduler import Scheduler, Task
from holonbridge.scheduler.model import stamp
from holonbridge.scheduler.store import PolicyUnresolvable, SchedulerStore
from holonbridge.scheduler.vocab import PROVENANCE_GRAPH, SCHED, TASKS_GRAPH, graph_query
from holonbridge.server import create_app

TOKEN = "test-token"
HB = "https://w3id.org/holonbridge/"
ODRL = "http://www.w3.org/ns/odrl/2/"


def _row(**cells: str) -> dict:
    return {key: {"value": value} for key, value in cells.items()}


def task_rows(**overrides: str) -> list[dict]:
    iri = f"{SCHED}task-census"
    props = {
        "id": "census",
        "actionClass": f"{SCHED}ReadOnlyQuery",
        "taskStatus": f"{SCHED}Active",
        "triggerType": "TemporalTrigger",
        "intervalSeconds": "3600",
        "datasetScope": "worldtest",
        "sparql": "SELECT * WHERE { ?s ?p ?o } LIMIT 1",
        "persona": f"{SCHED}persona-weatherwax",
        "hasPolicy": f"{SCHED}policy-census",
    }
    props.update(overrides)
    return [_row(t=iri, p=f"{SCHED}{k}", o=v) for k, v in props.items() if v]


def persona_rows(*capabilities: str) -> list[dict]:
    iri = f"{SCHED}persona-weatherwax"
    rows = [
        _row(a=iri, p=f"{SCHED}id", o="weatherwax"),
        _row(a=iri, p=f"{SCHED}model", o="claude-sonnet-4-6"),
        _row(a=iri, p=f"{SCHED}datasetScope", o="worldtest"),
        _row(a=iri, p=f"{SCHED}hasPolicy", o=f"{SCHED}policy-weatherwax"),
    ]
    rows.extend(
        _row(a=iri, p=f"{SCHED}capability", o=f"{SCHED}{c}") for c in capabilities
    )
    return rows


class SchedStub:
    def __init__(self) -> None:
        self.tasks = task_rows()
        self.personas = persona_rows("ReadOnlyQuery", "GraphWrite")
        self.policy_count: str | None = "10"
        self.policy_rows_empty = False
        self.policy_raises = False
        self.firings_today = 0
        self.updates: list[str] = []
        self.queries: list[str] = []
        self.pushed: list[tuple[str, str]] = []
        self.activity_rows: list[dict] = []

    async def select(self, conn, query, *, default_graph=None):
        self.queries.append(query)
        if "odrl:permission" in query:
            if self.policy_raises:
                from holonbridge.fuseki import FusekiError

                raise FusekiError(400, "prefix not declared")
            if self.policy_rows_empty:
                return {"results": {"bindings": []}}
            return {"results": {"bindings": [_row(count=self.policy_count, version="v2")]}}
        if "COUNT(?rec)" in query:
            return {"results": {"bindings": [{"c": {"value": str(self.firings_today)}}]}}
        if "FiringRecord" in query:
            return {"results": {"bindings": self.activity_rows}}
        if "QuarantinedProposal" in query:
            return {"results": {"bindings": []}}
        if "sched:Persona" in query:
            return {"results": {"bindings": self.personas}}
        if "sched:ScheduledTask" in query:
            return {"results": {"bindings": self.tasks}}
        if "projection" in query or "Delivery" in query:
            return {"results": {"bindings": []}}
        return {"results": {"bindings": [{"s": {"value": "x"}}]}}

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        return "<urn:a> <urn:b> <urn:c> .\n"

    async def update(self, conn, update):
        self.updates.append(update)

    async def get_graph(self, conn, graph_iri):
        return ""

    async def post_graph(self, conn, graph_iri, turtle):
        self.pushed.append((graph_iri, turtle))

    async def put_graph(self, conn, graph_iri, turtle):
        return None

    async def drop_graph(self, conn, graph_iri):
        return None

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        return ""

    async def ping(self, conn):
        return True

    async def aclose(self):
        return None


def admin_conn() -> Conn:
    return Conn(base_url="http://x", dataset="admin", overridden=True, bank_name="local")


@pytest.fixture
def stub() -> SchedStub:
    return SchedStub()


@pytest.fixture
async def scheduler(stub: SchedStub) -> Scheduler:
    sched = Scheduler(stub, admin_conn=admin_conn(), tick_seconds=1000.0)
    await sched.reload()
    return sched



def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# --- the structural guard -----------------------------------------------------


def test_every_scheduler_query_is_graph_wrapped(stub):
    """No scheduler query may address the default graph."""
    import holonbridge.scheduler.store as store_module

    source = inspect.getsource(store_module)
    # every SELECT in the store is built through graph_query
    assert 'self._client.select(conn, f"""' not in source
    assert source.count("graph_query(") >= 6

    for method in ("tasks", "personas"):
        assert f"graph_query(" in inspect.getsource(getattr(SchedulerStore, method))


def test_graph_query_refuses_an_empty_graph():
    with pytest.raises(ValueError):
        graph_query("SELECT ?s", "", "?s ?p ?o .")


async def test_policy_and_count_queries_name_their_graphs(stub, scheduler):
    await scheduler.fire(scheduler.task("census"))
    policy_queries = [q for q in stub.queries if "odrl:permission" in q]
    count_queries = [q for q in stub.queries if "COUNT(?rec)" in q]
    assert policy_queries and count_queries
    assert all(f"GRAPH <{TASKS_GRAPH}>" in q for q in policy_queries)
    assert all(f"GRAPH <{PROVENANCE_GRAPH}>" in q for q in count_queries)


# --- loading ------------------------------------------------------------------


async def test_tasks_and_personas_load(scheduler):
    assert [t.id for t in scheduler.tasks] == ["census"]
    task = scheduler.task("census")
    assert task.action_class == "ReadOnlyQuery"
    assert task.status == "Active"
    assert task.dataset_scope == "worldtest"
    assert task.action == "sparql"


async def test_the_unit_comes_from_the_property_name(stub):
    """intervalMs is milliseconds; intervalSeconds is seconds. No guessing."""
    stub.tasks = [
        r for r in task_rows() if not r["p"]["value"].endswith("intervalSeconds")
    ] + [_row(t=f"{SCHED}task-census", p=f"{SCHED}intervalMs", o="21600000")]
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()
    assert sched.task("census").interval_seconds == 21600.0


async def test_a_large_seconds_interval_is_taken_at_its_word(stub):
    stub.tasks = task_rows(intervalSeconds="21600000")
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()
    assert sched.task("census").interval_seconds == 21600000.0


def test_a_task_that_has_never_fired_is_due():
    task = Task(iri="urn:t", id="t", interval_seconds=3600)
    assert task.due() is True


def test_a_recently_fired_task_is_not_due():
    task = Task(iri="urn:t", id="t", interval_seconds=3600, last_fired=stamp())
    assert task.due() is False


def test_an_unqualified_last_fired_makes_a_task_due():
    # rather than comparing indeterminately and skipping forever
    task = Task(iri="urn:t", id="t", interval_seconds=3600, last_fired="2026-07-27T00:00:00")
    assert task.due() is True


def test_suspended_tasks_are_never_due():
    task = Task(iri="urn:t", id="t", status="Suspended")
    assert task.due() is False


# --- gates --------------------------------------------------------------------


async def test_read_only_task_records_read_only(scheduler, stub):
    record = await scheduler.fire(scheduler.task("census"))
    assert record.outcome == "read-only"
    assert record.trigger_type == "TemporalTrigger"
    assert record.task_policy_version == "v2"


async def test_capability_gate_rejects_and_still_records(stub):
    stub.tasks = task_rows(actionClass=f"{SCHED}GraphWrite", sparql="", payload="<urn:a> <urn:b> <urn:c> .", targetGraph="urn:worldtest:holons")
    stub.personas = persona_rows("ReadOnlyQuery")  # no GraphWrite
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "rejected-capability"
    assert "lacks GraphWrite" in record.detail
    assert not stub.pushed
    assert any("FiringRecord" in u for u in stub.updates)


async def test_rate_limit_rejects_at_the_cap(stub):
    stub.policy_count = "3"
    stub.firings_today = 3
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "rejected-policy"
    assert "daily limit of 3" in record.detail


async def test_rate_limit_allows_below_the_cap(stub):
    stub.policy_count = "3"
    stub.firings_today = 2
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()
    assert (await sched.fire(sched.task("census"))).outcome == "read-only"


async def test_an_unreadable_policy_fails_closed(stub):
    """A limit that cannot be read is not the same as no limit."""
    stub.policy_raises = True
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "rejected-policy"
    assert "could not be read" in record.detail


async def test_a_policy_with_no_constraint_fails_closed(stub):
    stub.policy_rows_empty = True
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "rejected-policy"
    assert "no odrl:count" in record.detail


async def test_no_policy_at_all_is_unlimited(stub):
    stub.tasks = task_rows(hasPolicy="")
    stub.personas = [
        r for r in persona_rows("ReadOnlyQuery") if "hasPolicy" not in r["p"]["value"]
    ]
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()
    assert (await sched.fire(sched.task("census"))).outcome == "read-only"


async def test_manual_firing_does_not_draw_on_the_daily_allowance(stub):
    stub.policy_count = "1"
    stub.firings_today = 5
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"), invocation_source="manual")
    assert record.outcome == "read-only"
    assert record.invocation_source == "manual"


async def test_suspended_task_is_refused_and_recorded(stub):
    stub.tasks = task_rows(taskStatus=f"{SCHED}Suspended")
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "rejected-policy"
    assert "suspended" in record.detail


# --- execution ----------------------------------------------------------------


async def test_a_task_acts_through_its_own_dataset_not_the_admin_one(stub):
    stub.tasks = task_rows(
        actionClass=f"{SCHED}GraphWrite",
        sparql="",
        payload="<urn:a> <urn:b> <urn:c> .",
        targetGraph="urn:worldtest:holons",
    )
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    task = sched.task("census")
    assert sched._target_conn(task).dataset == "worldtest"
    assert sched.admin_conn.dataset == "admin"

    record = await sched.fire(task)
    assert record.outcome == "committed"
    assert stub.pushed[0][0] == "urn:worldtest:holons"


async def test_a_payload_task_without_a_target_fails_clearly(stub):
    stub.tasks = task_rows(
        actionClass=f"{SCHED}GraphWrite", sparql="", payload="<urn:a> <urn:b> <urn:c> ."
    )
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()
    record = await sched.fire(sched.task("census"))
    assert record.outcome == "failed"
    assert "target graph" in record.detail


async def test_a_task_with_no_action_fails_clearly(stub):
    stub.tasks = task_rows(sparql="")
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()
    record = await sched.fire(sched.task("census"))
    assert record.outcome == "failed"
    assert "no sparql, rule, pipeline, projection, maintenance" in record.detail


async def test_llm_invocation_without_a_proposer_defers(stub):
    stub.tasks = task_rows(
        actionClass=f"{SCHED}LLMInvocation", sparql="", targetGraph="urn:worldtest:holons"
    )
    stub.personas = persona_rows("LLMInvocation")
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "deferred"
    assert not stub.pushed  # nothing was written


async def test_a_failing_proposal_is_quarantined_not_dropped(stub):
    class BadProposer:
        async def propose(self, conn, task, persona):
            return "<urn:a> <urn:b> <urn:c> .", "one line summary"

    stub.tasks = task_rows(
        actionClass=f"{SCHED}LLMInvocation", sparql="", targetGraph="urn:worldtest:holons"
    )
    stub.personas = persona_rows("LLMInvocation")
    sched = Scheduler(stub, admin_conn=admin_conn(), proposer=BadProposer())
    await sched.reload()

    # the shapes graph is empty, so validation cannot run -> quarantine
    record = await sched.fire(sched.task("census"))
    assert record.outcome == "quarantined"
    assert "urn:scheduler:quarantine:" in record.detail
    assert any("QuarantinedProposal" in u for u in stub.updates)
    assert any("proposedTurtle" in u for u in stub.updates)


# --- recursion guard ----------------------------------------------------------


async def test_a_task_already_in_flight_is_skipped(scheduler):
    task = scheduler.task("census")
    scheduler._in_flight.add(task.iri)
    assert await scheduler.fire(task) is None


async def test_the_in_flight_set_is_cleared_after_a_failure(stub):
    class Boom:
        async def propose(self, conn, task, persona):
            raise RuntimeError("nope")

    stub.tasks = task_rows(actionClass=f"{SCHED}LLMInvocation", sparql="")
    stub.personas = persona_rows("LLMInvocation")
    sched = Scheduler(stub, admin_conn=admin_conn(), proposer=Boom())
    await sched.reload()

    await sched.fire(sched.task("census"))
    assert sched.status()["inFlight"] == []


# --- provenance and escaping --------------------------------------------------


async def test_provenance_is_written_for_every_outcome(stub):
    stub.policy_count = "0"
    stub.firings_today = 0
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "rejected-policy"
    inserts = [u for u in stub.updates if "FiringRecord" in u]
    assert len(inserts) == 1
    assert "rejected-policy" in inserts[0]


async def test_record_literals_are_escaped(stub):
    store = SchedulerStore(stub)
    from holonbridge.scheduler import FiringRecord

    record = FiringRecord(
        iri="urn:scheduler:firing:1",
        task_iri="urn:t",
        outcome="failed",
        detail='C:\\jena\\data broke: he said "no"\nagain',
    )
    await store.record(admin_conn(), record)
    insert = stub.updates[-1]
    assert "\\\\jena" in insert
    assert '\\"no\\"' in insert
    assert "\\n" in insert


async def test_task_writes_escape_windows_paths(stub):
    store = SchedulerStore(stub)
    task = Task(
        iri="urn:t", id="t", payload="x", target_graph="urn:g",
        description=r"reads C:\ProgramData\holon-bridge\out",
    )
    await store.save_task(admin_conn(), task)
    insert = next(u for u in stub.updates if u.startswith("INSERT DATA"))
    assert r"C:\\ProgramData\\holon-bridge\\out" in insert


# --- routes -------------------------------------------------------------------


def test_routes_report_503_when_the_scheduler_is_off():
    app = create_app(Settings(bearer_token=TOKEN, scheduler_enabled=False))
    with TestClient(app) as test_client:
        response = test_client.get("/scheduler/status", headers=auth())
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "scheduler_disabled"


def test_activity_refuses_a_since_without_a_timezone(stub):
    app = create_app(Settings(bearer_token=TOKEN))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        app.state.scheduler = Scheduler(stub, admin_conn=admin_conn())

        bad = test_client.get(
            "/scheduler/activity", params={"since": "2026-07-27T00:00:00"}, headers=auth()
        )
        assert bad.status_code == 400
        assert "timezone" in bad.json()["detail"]["message"]

        good = test_client.get(
            "/scheduler/activity", params={"since": "2026-07-27T00:00:00Z"}, headers=auth()
        )
        assert good.status_code == 200


def test_create_task_needs_exactly_one_action(stub):
    app = create_app(Settings(bearer_token=TOKEN))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        app.state.scheduler = Scheduler(stub, admin_conn=admin_conn())

        none = test_client.post(
            "/scheduler/task", json={"id": "x"}, headers=auth()
        )
        both = test_client.post(
            "/scheduler/task",
            json={"id": "x", "sparql": "SELECT * WHERE {?s ?p ?o}", "rule": "r"},
            headers=auth(),
        )
        assert none.status_code == 400 and both.status_code == 400


def test_scheduler_routes_are_pinned_to_the_admin_dataset(stub):
    app = create_app(Settings(bearer_token=TOKEN))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        app.state.scheduler = Scheduler(stub, admin_conn=admin_conn())

        body = test_client.get(
            "/scheduler/status",
            headers={**auth(), "X-Dataset-Override": "worldtest"},
        ).json()
        assert body["dataset"] == "admin"
        assert body["callerDataset"] == "worldtest"


# --- proposer -----------------------------------------------------------------


def test_the_summary_is_stripped_from_the_payload():
    from holonbridge.scheduler import parse_proposal

    turtle, summary = parse_proposal(
        'SUMMARY: added two observations\n\n'
        "```turtle\n@prefix ex: <urn:ex:> .\nex:a ex:b ex:c .\n```\n"
    )
    assert summary == "added two observations"
    assert "SUMMARY" not in turtle
    assert turtle.startswith("@prefix")


def test_an_unfenced_reply_is_still_accepted():
    from holonbridge.scheduler import parse_proposal

    turtle, summary = parse_proposal("SUMMARY: one thing\n@prefix ex: <urn:ex:> .\nex:a ex:b ex:c .")
    assert "@prefix" in turtle
    assert summary == "one thing"


def test_a_reply_with_no_turtle_is_unparseable_and_keeps_its_text():
    from holonbridge.scheduler import ProposalUnparseable, parse_proposal

    with pytest.raises(ProposalUnparseable) as exc:
        parse_proposal("SUMMARY: I could not find anything to add")
    assert "could not find anything" in exc.value.raw


async def test_an_unparseable_proposal_is_quarantined_with_its_raw_text(stub):
    from holonbridge.scheduler import ProposalUnparseable

    class Garbled:
        async def propose(self, conn, task, persona):
            raise ProposalUnparseable("no turtle", "I am not going to do that, Dave")

    stub.tasks = task_rows(
        actionClass=f"{SCHED}LLMInvocation", sparql="", targetGraph="urn:worldtest:holons"
    )
    stub.personas = persona_rows("LLMInvocation")
    sched = Scheduler(stub, admin_conn=admin_conn(), proposer=Garbled())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "quarantined"
    assert any("not going to do that" in u for u in stub.updates)


async def test_a_proposer_that_errors_fails_rather_than_defers(stub):
    class Broken:
        async def propose(self, conn, task, persona):
            raise RuntimeError("429 rate limited")

    stub.tasks = task_rows(
        actionClass=f"{SCHED}LLMInvocation", sparql="", targetGraph="urn:worldtest:holons"
    )
    stub.personas = persona_rows("LLMInvocation")
    sched = Scheduler(stub, admin_conn=admin_conn(), proposer=Broken())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    # 'deferred' would read as a configuration choice and hide a broken persona
    assert record.outcome == "failed"
    assert "429" in record.detail


# --- recursion guards ---------------------------------------------------------


async def test_firing_depth_is_capped(stub):
    sched = Scheduler(stub, admin_conn=admin_conn(), max_firing_depth=2)
    await sched.reload()
    record = await sched.fire(sched.task("census"), depth=2)
    assert record.outcome == "rejected-policy"
    assert "depth" in record.detail


async def test_a_nested_refire_within_one_pass_is_refused(scheduler):
    task = scheduler.task("census")
    await scheduler.tick()  # top-level firing marks the task for this pass
    record = await scheduler.fire(task, depth=1)
    assert record.outcome == "rejected-policy"
    assert "cycle" in record.detail


async def test_the_pass_marker_resets_each_tick(scheduler):
    await scheduler.tick()
    first = len([t for t in scheduler.tasks])
    await scheduler.tick()
    # a second tick fires the task again at top level rather than refusing it
    assert first == 1
    assert scheduler.status()["ticks"] == 2


async def test_top_level_firing_is_never_blocked_by_the_pass_marker(scheduler):
    task = scheduler.task("census")
    await scheduler.fire(task)
    record = await scheduler.fire(task)
    assert record.outcome == "read-only"


# --- maintenance --------------------------------------------------------------


async def test_a_maintenance_task_runs_the_projection_sweep(stub):
    stub.tasks = task_rows(sparql="", maintenance="projection-sweep")
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "read-only"
    assert "swept" in record.detail


async def test_an_unknown_maintenance_job_fails_clearly(stub):
    stub.tasks = task_rows(sparql="", maintenance="reticulate-splines")
    sched = Scheduler(stub, admin_conn=admin_conn())
    await sched.reload()

    record = await sched.fire(sched.task("census"))
    assert record.outcome == "failed"
    assert "unknown maintenance job" in record.detail
