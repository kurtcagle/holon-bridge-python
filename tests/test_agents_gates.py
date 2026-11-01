"""Agent calls under the scheduler's gates, and in the audit graph.

Two things were true of 4.5 that this slice closes:

* the ``agent-dispatch`` job passed the scheduler's capability and ODRL gates
  like any other task, but the gates did not know it sends data to a third
  party: a persona that may only read could run it, and the daily limit counted
  *firings* (so one firing could send any number of calls);
* the ledger recorded what happened to a call, but the audit graph -- the one
  place an administrator looks to ask "what left this dataset" -- did not.

What these pin down:

* a dispatch task needs the ``AgentCall`` capability whatever class it declares;
* the daily count bounds *calls sent*: a firing sends no more than what is left
  of the tightest limit, a manual run does not draw on it, and an idle or
  refused batch costs nothing;
* every step of a call's life is in the audit graph with the call, the endpoint
  and the digest of the body -- never the body;
* **a call is not sent if its ``send`` entry cannot be written** (unless audit
  is switched off for the dataset);
* a queue of unanswered calls is bounded.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from holonbridge import audit, config
from holonbridge.agents import service as svc
from holonbridge.agents.ledger import CallStore
from holonbridge.agents.model import APPROVED, FAILED, PENDING, REJECTED, RESPONDED
from holonbridge.agents.registry import endpoint_node
from holonbridge.agents.sender import SendFailed
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.scheduler.model import FiringRecord, Task
from holonbridge.scheduler.runner import DISPATCH_BATCH, Scheduler
from holonbridge.scheduler.store import SchedulerStore
from holonbridge.scheduler.vocab import PROVENANCE_GRAPH
from holonbridge.server import create_app
from holonbridge.triggers import TRIGGER_STATE, evaluate_triggers

from ._identity import PERSON_IRI, auth
from ._rdfstore import RdfStore
from .test_agents import CONN, LEDGER, FakeSender, add_query, make_endpoint, ok, request
from .test_agents_wiring import add_endpoint, add_trigger

SCHED = "https://w3id.org/holon/sched#"
ODRL = "http://www.w3.org/ns/odrl/2/"
TOKEN = "test-token"
AUDIT = audit.audit_graph(CONN.holons_graph)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_ALLOWLIST", "agent.example")
    monkeypatch.setenv("HB_AGENT_KEY", "k")
    monkeypatch.delenv("HOLONBRIDGE_AUDIT_LEVEL", raising=False)
    monkeypatch.delenv("HOLONBRIDGE_AUDIT_LEVELS", raising=False)
    config.get_settings.cache_clear()
    audit.forget_level(CONN)
    yield
    config.get_settings.cache_clear()
    audit.forget_level(CONN)


@pytest.fixture
def store() -> RdfStore:
    s = RdfStore()
    s.add_turtle("urn:ds:holons", "<urn:w:1> a <urn:Widget> . <urn:w:2> a <urn:Widget> .")
    add_query(s, "widgets", "SELECT ?focus WHERE { ?focus a <urn:Widget> }")
    return s


# --- helpers -------------------------------------------------------------------------


def entries(store: RdfStore, operation: str | None = None) -> list[dict]:
    """The agent-call entries in the audit graph, oldest first."""
    rows = store.graph(AUDIT).query(
        """PREFIX hb: <https://w3id.org/holonbridge/>
SELECT ?e ?at ?op ?outcome ?status ?call ?endpoint ?hash ?bytes ?reason ?by ?actor ?path WHERE {
  ?e a hb:AuditEntry ; hb:at ?at ; hb:operation ?op ; hb:outcome ?outcome ; hb:status ?status ;
     hb:path ?path .
  FILTER(STRSTARTS(?op, "AGENT-CALL"))
  OPTIONAL { ?e hb:agentCall ?call }
  OPTIONAL { ?e hb:agentEndpoint ?endpoint }
  OPTIONAL { ?e hb:payloadHash ?hash }
  OPTIONAL { ?e hb:payloadBytes ?bytes }
  OPTIONAL { ?e hb:reason ?reason }
  OPTIONAL { ?e hb:authorisedBy ?by }
  OPTIONAL { ?e hb:actor ?actor }
} ORDER BY ?at"""
    )
    out = [
        {str(k): (str(v) if v is not None else None) for k, v in zip(rows.vars, row)}
        for row in rows
    ]
    for item in out:
        item["status"] = int(item["status"])
    if operation:
        out = [e for e in out if e["op"] == f"AGENT-CALL {operation}"]
    return out


def ops(store: RdfStore) -> list[str]:
    return [e["op"].removeprefix("AGENT-CALL ") for e in entries(store)]


def add_persona(store: RdfStore, pid: str, *capabilities: str, policy: str | None = None) -> str:
    iri = f"urn:persona:{pid}"
    caps = " ".join(f"<{SCHED}capability> <{SCHED}{c}> ;" for c in capabilities)
    pol = f"<{SCHED}hasPolicy> <{policy}> ;" if policy else ""
    store.add_turtle(
        "urn:scheduler:personas",
        f'<{iri}> a <{SCHED}Persona> ; {caps} {pol} <{SCHED}id> "{pid}" .',
    )
    return iri


def add_policy(store: RdfStore, name: str, count: int) -> str:
    iri = f"urn:policy:{name}"
    store.add_turtle(
        "urn:scheduler:tasks",
        f"<{iri}> <{ODRL}permission> [ <{ODRL}constraint> "
        f"[ <{ODRL}leftOperand> <{ODRL}count> ; <{ODRL}rightOperand> {count} ] ] .",
    )
    return iri


def dispatch_task(**overrides) -> Task:
    values = dict(
        iri="urn:ds:task:d", id="d", maintenance="agent-dispatch", action_class="AgentCall"
    )
    values.update(overrides)
    return Task(**values)


async def scheduler(store, sender) -> Scheduler:
    sched = Scheduler(store, admin_conn=CONN, tick_seconds=1000.0, agent_sender=sender)
    await sched.reload()
    return sched


async def queue(store, n: int = 1, endpoint=None, **kwargs):
    """``n`` policy-approved calls waiting for the dispatch job."""
    endpoint = endpoint or make_endpoint(call_review=False)
    return [await request(store, endpoint, approval="policy", **kwargs) for _ in range(n)]


async def used_today(store, task: Task) -> int:
    day = FiringRecord(iri="x", task_iri=task.iri, outcome="").fired_at[:10]
    return await SchedulerStore(store).firings_today(
        CONN, subject_iri=task.iri, predicate="task", day=day
    )


async def calls_with(store, status):
    return await CallStore(store).list_calls(CONN, status=status)


# --- the task knows what it does ---------------------------------------------------------


class TestRequiredClass:
    def test_a_dispatch_task_needs_agent_call_whatever_it_declares(self):
        for job in ("agent-dispatch", "dispatch-agent-calls", " Agent-Dispatch "):
            task = Task(iri="urn:t", id="t", maintenance=job, action_class="ReadOnlyQuery")
            assert task.sends_agent_calls
            assert task.required_class == "AgentCall"
            assert task.summary()["requiredClass"] == "AgentCall"

    def test_other_tasks_need_what_they_declare(self):
        plain = Task(iri="urn:t", id="t", sparql="SELECT * WHERE {?s ?p ?o}", action_class="GraphWrite")
        other_job = Task(iri="urn:t", id="t", maintenance="snapshot", action_class="ReadOnlyQuery")
        assert plain.required_class == "GraphWrite"
        assert other_job.required_class == "ReadOnlyQuery" and not other_job.sends_agent_calls


# --- the capability gate -------------------------------------------------------------------


class TestCapabilityGate:
    async def test_a_read_only_persona_cannot_run_the_dispatch_even_if_the_task_says_it_is_a_read(
        self, store
    ):
        add_endpoint(store, call_review=False)
        await queue(store)
        persona = add_persona(store, "reader", "ReadOnlyQuery")
        sender = FakeSender()
        sched = await scheduler(store, sender)

        # The class the task declares is the default; what it *does* is send.
        task = dispatch_task(action_class="ReadOnlyQuery", persona_iri=persona)
        record = await sched.fire(task)

        assert record.outcome == "rejected-capability"
        assert "AgentCall" in record.detail and "ReadOnlyQuery" in record.detail
        assert sender.sent == []
        assert len(await calls_with(store, APPROVED)) == 1

    async def test_a_persona_with_the_capability_runs_it(self, store):
        add_endpoint(store, call_review=False)
        await queue(store)
        persona = add_persona(store, "agent", "ReadOnlyQuery", "AgentCall")
        sender = FakeSender()
        record = await (await scheduler(store, sender)).fire(dispatch_task(persona_iri=persona))
        assert record.outcome == "committed"
        assert len(sender.sent) == 1

    async def test_a_task_with_no_persona_runs(self, store):
        add_endpoint(store, call_review=False)
        await queue(store)
        sender = FakeSender()
        record = await (await scheduler(store, sender)).fire(dispatch_task())
        assert record.outcome == "committed" and len(sender.sent) == 1


# --- the daily allowance counts calls --------------------------------------------------------


class TestDailyAllowance:
    async def test_a_firing_sends_no_more_calls_than_the_limit_allows(self, store):
        add_endpoint(store, call_review=False)
        await queue(store, 5)
        policy = add_policy(store, "two", 2)
        sender = FakeSender()
        sched = await scheduler(store, sender)
        task = dispatch_task(policy_iri=policy)

        first = await sched.fire(task)
        assert first.outcome == "committed"
        assert first.units == 2 and len(sender.sent) == 2
        assert "(limit 2)" in first.detail
        assert await used_today(store, task) == 2
        assert len(await calls_with(store, APPROVED)) == 3  # the rest wait

        second = await sched.fire(task)
        assert second.outcome == "rejected-policy"
        assert "daily limit of 2" in second.detail
        assert len(sender.sent) == 2

    @pytest.mark.parametrize("task_limit, persona_limit", [(4, 1), (1, 4)])
    async def test_the_tighter_of_the_task_and_persona_limits_applies(
        self, store, task_limit, persona_limit
    ):
        add_endpoint(store, call_review=False)
        await queue(store, 5)
        task_policy = add_policy(store, "task", task_limit)
        persona_policy = add_policy(store, "persona", persona_limit)
        persona = add_persona(store, "agent", "AgentCall", policy=persona_policy)
        sender = FakeSender()
        record = await (await scheduler(store, sender)).fire(
            dispatch_task(policy_iri=task_policy, persona_iri=persona)
        )
        assert record.units == 1 and len(sender.sent) == 1
        assert "(limit 1)" in record.detail

    async def test_what_an_earlier_firing_used_is_taken_off(self, store):
        add_endpoint(store, call_review=False)
        policy = add_policy(store, "three", 3)
        sender = FakeSender()
        sched = await scheduler(store, sender)
        task = dispatch_task(policy_iri=policy)

        await queue(store, 2)
        assert (await sched.fire(task)).units == 2
        await queue(store, 3)
        later = await sched.fire(task)
        assert later.units == 1  # three a day, two already sent
        assert len(sender.sent) == 3

    async def test_without_a_limit_a_firing_is_capped_by_the_batch_size(self, store):
        add_endpoint(store, call_review=False)
        await queue(store, DISPATCH_BATCH + 2)
        sender = FakeSender()
        record = await (await scheduler(store, sender)).fire(dispatch_task())
        assert record.units == DISPATCH_BATCH and len(sender.sent) == DISPATCH_BATCH
        assert "(limit" not in record.detail

    async def test_an_idle_firing_costs_nothing(self, store):
        add_endpoint(store, call_review=False)
        policy = add_policy(store, "one", 1)
        task = dispatch_task(policy_iri=policy)
        sched = await scheduler(store, FakeSender())

        idle = await sched.fire(task)
        assert idle.outcome == "committed" and idle.units == 0
        assert await used_today(store, task) == 0

        # ...so the one call that does arrive can still go.
        await queue(store, 1)
        assert (await sched.fire(task)).units == 1

    async def test_calls_the_agent_refuses_are_still_calls_sent(self, store):
        # A broken endpoint must not be hammered past the limit: attempts count.
        add_endpoint(store, call_review=False)
        await queue(store, 3)
        policy = add_policy(store, "two", 2)
        sender = FakeSender(exc=SendFailed("agent said no"))
        task = dispatch_task(policy_iri=policy)
        sched = await scheduler(store, sender)

        record = await sched.fire(task)
        assert record.outcome == "committed"
        assert record.units == 2 and len(sender.sent) == 2
        assert "2 failed" in record.detail
        assert await used_today(store, task) == 2
        assert (await sched.fire(task)).outcome == "rejected-policy"

    async def test_a_batch_where_nothing_was_attempted_is_a_failure_and_free(self, store):
        add_endpoint(store, call_review=False, status="Suspended")
        await queue(store, 2)
        sender = FakeSender()
        task = dispatch_task(policy_iri=add_policy(store, "two", 2))
        record = await (await scheduler(store, sender)).fire(task)
        assert record.outcome == "failed"
        assert record.units == 0 and sender.sent == []
        assert await used_today(store, task) == 0

    async def test_a_manual_run_ignores_the_allowance_and_does_not_draw_on_it(self, store):
        add_endpoint(store, call_review=False)
        await queue(store, 5)
        task = dispatch_task(policy_iri=add_policy(store, "one", 1))
        sender = FakeSender()
        sched = await scheduler(store, sender)

        manual = await sched.fire(task, invocation_source="manual")
        assert manual.outcome == "committed"
        assert manual.units == 0 and manual.allowance is None
        assert len(sender.sent) == 5
        assert await used_today(store, task) == 0

    async def test_units_are_stored_and_older_records_still_count_as_one(self, store):
        add_endpoint(store, call_review=False)
        await queue(store, 3)
        task = dispatch_task()
        await (await scheduler(store, FakeSender())).fire(task)
        assert await used_today(store, task) == 3

        day = FiringRecord(iri="x", task_iri=task.iri, outcome="").fired_at
        store.add_turtle(
            PROVENANCE_GRAPH,
            f'<urn:old:1> a <{SCHED}FiringRecord> ; <{SCHED}task> <{task.iri}> ; '
            f'<{SCHED}outcome> "committed" ; <{SCHED}firedAt> "{day}"^^<http://www.w3.org/2001/XMLSchema#dateTime> .',
        )
        assert await used_today(store, task) == 4  # the old record has no units: one


# --- creating a dispatch task --------------------------------------------------------------------


class TestCreateTaskRoute:
    @pytest.fixture
    def admin_api(self, store):
        store.admin = True
        admin = Conn(base_url="http://x", dataset="admin", overridden=True, bank_name="local")
        app = create_app(Settings(bearer_token=TOKEN, fuseki_dataset="admin"))
        with TestClient(app) as client:
            app.state.fuseki = store
            app.state.scheduler = Scheduler(store, admin_conn=admin)
            yield client

    def test_a_dispatch_task_must_declare_agent_call(self, admin_api):
        refused = admin_api.post(
            "/scheduler/task", json={"id": "d", "maintenance": "agent-dispatch"}, headers=auth()
        )
        assert refused.status_code == 400
        assert "AgentCall" in refused.text

        alias = admin_api.post(
            "/scheduler/task",
            json={"id": "d", "maintenance": "dispatch-agent-calls", "action_class": "ReadOnlyQuery"},
            headers=auth(),
        )
        assert alias.status_code == 400

    def test_declaring_it_makes_the_task(self, admin_api):
        made = admin_api.post(
            "/scheduler/task",
            json={"id": "d", "maintenance": "agent-dispatch", "action_class": "AgentCall"},
            headers=auth(),
        )
        assert made.status_code == 200, made.text
        assert made.json()["requiredClass"] == "AgentCall"


# --- the audit trail ----------------------------------------------------------------------------


class TestAuditTrail:
    async def test_a_call_leaves_an_entry_for_each_step(self, store):
        call = await request(store)
        await svc.approve_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=FakeSender()
        )
        assert ops(store) == ["placed", "approved", "send", "result"]

        for entry in entries(store):
            assert entry["call"] == call.iri
            assert entry["endpoint"] == "triage"
            assert entry["hash"] == call.digest
            assert entry["path"] == f"/agent-call/{call.id}"
            assert "to agent.example" in (entry["reason"] or "")
        send = entries(store, "send")[0]
        assert send["outcome"] == "allowed" and send["by"] == PERSON_IRI
        assert int(send["bytes"]) == len(call.body.encode("utf-8"))
        assert entries(store, "result")[0]["status"] == 200

    async def test_bodies_are_hashed_and_sized_never_copied(self, store):
        call = await request(store, payload={"secret_field": "do-not-copy-me"})
        await svc.approve_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=FakeSender()
        )
        text = store.graph(AUDIT).serialize(format="turtle")
        assert "do-not-copy-me" not in text and "triage this" not in text
        assert call.digest in text

    async def test_a_failed_send_is_recorded_as_an_error(self, store):
        call = await request(store)
        done = await svc.approve_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(),
            sender=FakeSender(exc=SendFailed("connection refused")),
        )
        assert done.status == FAILED
        result = entries(store, "result")[0]
        assert result["outcome"] == "error" and result["status"] == 502
        assert "connection refused" in result["reason"]

    async def test_a_rejection_is_recorded(self, store):
        call = await request(store)
        await svc.reject_call(store, CONN, call.id, rejecter=PERSON_IRI, reason="not this one")
        assert ops(store) == ["placed", "rejected"]
        rejected = entries(store, "rejected")[0]
        assert rejected["actor"] == PERSON_IRI and "not this one" in rejected["reason"]

    async def test_a_policy_queued_call_says_so_when_it_is_placed(self, store):
        [call] = await queue(store)
        placed = entries(store, "placed")[0]
        assert "queued by policy" in placed["reason"] and placed["call"] == call.iri

    async def test_a_call_the_dispatch_refuses_is_recorded_as_denied(self, store):
        [call] = await queue(store)
        store.denied_write_graphs = {LEDGER}  # the requester lost the grant while it waited
        sender = FakeSender()
        await svc.dispatch_approved(
            store, CONN, endpoints={"triage": make_endpoint(call_review=False)}, sender=sender,
            source="scheduled task 'd'",
        )
        assert sender.sent == []
        refused = entries(store, "dispatch refused")
        assert len(refused) == 1
        assert refused[0]["outcome"] == "denied" and refused[0]["status"] == 403
        assert refused[0]["call"] == call.iri and "scheduled task 'd'" in refused[0]["reason"]
        assert "send" not in ops(store)

    async def test_the_scheduler_names_itself_on_the_send_entry(self, store):
        add_endpoint(store, call_review=False)
        await queue(store)
        await (await scheduler(store, FakeSender())).fire(dispatch_task())
        assert "scheduled task 'd'" in entries(store, "send")[0]["reason"]

    async def test_a_trigger_refused_because_its_definer_lost_access_is_recorded_once(self, store):
        add_endpoint(store)
        add_trigger(store)
        store.denied_write_graphs = {LEDGER}

        first = await evaluate_triggers(store, CONN, kind=TRIGGER_STATE)
        assert {f.outcome for f in first} == {"skipped-definer-unauthorised"}
        refused = entries(store, "trigger refused")
        assert len(refused) == 1
        assert refused[0]["outcome"] == "denied" and refused[0]["status"] == 403
        assert refused[0]["path"] == "/named-trigger/t1"
        assert refused[0]["endpoint"] == "triage"
        assert refused[0]["actor"] == PERSON_IRI

        # The verdict is cached for a short while; reusing it is not a new event.
        await evaluate_triggers(store, CONN, kind=TRIGGER_STATE)
        assert len(entries(store, "trigger refused")) == 1

    async def test_the_audit_level_decides_what_is_written(self, store, monkeypatch):
        monkeypatch.setenv("HOLONBRIDGE_AUDIT_LEVEL", "denials")
        audit.forget_level(CONN)
        call = await request(store)
        await svc.approve_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=FakeSender()
        )
        assert entries(store) == []  # nothing was refused

        monkeypatch.setenv("HOLONBRIDGE_AUDIT_LEVEL", "off")
        audit.forget_level(CONN)
        await request(store)
        assert entries(store) == []

    def test_the_audit_listing_shows_the_call_and_endpoint(self, store):
        store.admin = True
        app = create_app(Settings(bearer_token=TOKEN, agent_allowlist=("agent.example",)))
        with TestClient(app) as client:
            app.state.fuseki = store
            app.state.agent_sender = FakeSender(ok())
            assert client.post(
                "/agent-endpoint", json={"id": "triage", "url": "https://agent.example/run"},
                headers=auth(),
            ).status_code == 200
            placed = client.post(
                "/agent-call", json={"endpoint": "triage", "task": "look"}, headers=auth()
            ).json()
            client.post(f"/agent-call/{placed['id']}/approve", json={}, headers=auth())

            listing = client.get("/audit", params={"limit": 200}, headers=auth())
            assert listing.status_code == 200, listing.text
            agent_entries = [
                e for e in listing.json()["entries"] if e["operation"].startswith("AGENT-CALL")
            ]
            assert {e["operation"] for e in agent_entries} >= {
                "AGENT-CALL placed", "AGENT-CALL approved", "AGENT-CALL send", "AGENT-CALL result",
            }
            assert {e["agent_endpoint"] for e in agent_entries} == {"triage"}
            assert {e["agent_call"] for e in agent_entries} == {
                next(e for e in agent_entries if e["agent_call"])["agent_call"]
            }
            assert all(e["agent_call"] for e in agent_entries)


# --- no record, no send -------------------------------------------------------------------------------


class TestNoRecordNoSend:
    async def test_a_call_whose_send_cannot_be_recorded_is_not_sent(self, store):
        call = await request(store)
        store.failing_post_graphs = {AUDIT}
        sender = FakeSender()

        done = await svc.approve_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=sender
        )

        assert sender.sent == []
        assert done.status == FAILED and done.error == svc.AUDIT_UNAVAILABLE
        assert (await CallStore(store).get(CONN, call.id)).status == FAILED

    async def test_it_can_be_retried_once_the_log_is_back(self, store):
        call = await request(store)
        store.failing_post_graphs = {AUDIT}
        await svc.approve_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=FakeSender()
        )
        store.failing_post_graphs = set()
        sender = FakeSender()
        done = await svc.retry_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=sender
        )
        assert done.status == RESPONDED and len(sender.sent) == 1
        assert "send" in ops(store)

    async def test_with_audit_off_an_unwritable_log_does_not_matter(self, store, monkeypatch):
        monkeypatch.setenv("HOLONBRIDGE_AUDIT_LEVEL", "off")
        audit.forget_level(CONN)
        call = await request(store)
        store.failing_post_graphs = {AUDIT}
        sender = FakeSender()
        done = await svc.approve_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=sender
        )
        assert done.status == RESPONDED and len(sender.sent) == 1

    async def test_placing_a_call_does_not_depend_on_the_log(self, store):
        store.failing_post_graphs = {AUDIT}
        call = await request(store)
        assert (await CallStore(store).get(CONN, call.id)).status == PENDING
        rejected = await svc.reject_call(store, CONN, call.id, rejecter=PERSON_IRI)
        assert rejected.status == REJECTED

    async def test_a_dispatch_that_cannot_record_sends_nothing_and_is_not_billed(self, store):
        add_endpoint(store, call_review=False)
        calls = await queue(store, 2)
        store.failing_post_graphs = {AUDIT}
        sender = FakeSender()
        task = dispatch_task(policy_iri=add_policy(store, "two", 2))

        record = await (await scheduler(store, sender)).fire(task)

        assert sender.sent == []
        assert record.outcome == "failed" and record.units == 0
        assert "0 sent" in record.detail and "2 failed" in record.detail
        assert await used_today(store, task) == 0
        assert {c.status for c in await CallStore(store).list_calls(CONN)} == {FAILED}
        assert {c.error for c in await CallStore(store).list_calls(CONN)} == {svc.AUDIT_UNAVAILABLE}
        assert len(calls) == 2


# --- a bounded queue -------------------------------------------------------------------------------------


def capped(n: int) -> Settings:
    return Settings(bearer_token="t", agent_allowlist=("agent.example",), agent_max_open_calls=n)


class TestOpenCallCap:
    async def test_the_cap_stops_the_next_call(self, store):
        await request(store, settings=capped(2))
        await request(store, settings=capped(2))
        with pytest.raises(svc.QueueFull) as stopped:
            await request(store, settings=capped(2))
        assert "AGENT_MAX_OPEN_CALLS=2" in str(stopped.value)
        assert len(await CallStore(store).list_calls(CONN)) == 2
        assert issubclass(svc.QueueFull, svc.CallInvalid)

    async def test_a_decision_frees_a_place(self, store):
        first = await request(store, settings=capped(1))
        with pytest.raises(svc.QueueFull):
            await request(store, settings=capped(1))
        await svc.reject_call(store, CONN, first.id, rejecter=PERSON_IRI)
        await request(store, settings=capped(1))

    async def test_answered_calls_are_not_open(self, store):
        first = await request(store, settings=capped(1))
        await svc.approve_call(
            store, CONN, first.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=FakeSender()
        )
        await request(store, settings=capped(1))

    async def test_the_cap_is_per_endpoint(self, store):
        other = make_endpoint(id="other", iri=endpoint_node(CONN, "other"))
        await request(store, settings=capped(1))
        await request(store, other, settings=capped(1))
        with pytest.raises(svc.QueueFull):
            await request(store, other, settings=capped(1))

    async def test_zero_means_no_limit(self, store):
        for _ in range(6):
            await request(store, settings=capped(0))
        assert len(await CallStore(store).list_calls(CONN)) == 6

    def test_the_setting_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_OPEN_CALLS", "7")
        config.get_settings.cache_clear()
        assert config.get_settings().agent_max_open_calls == 7
        monkeypatch.setenv("AGENT_MAX_OPEN_CALLS", "-3")
        config.get_settings.cache_clear()
        assert config.get_settings().agent_max_open_calls == 0
        monkeypatch.delenv("AGENT_MAX_OPEN_CALLS")
        config.get_settings.cache_clear()
        assert config.get_settings().agent_max_open_calls == 200

    async def test_a_trigger_matching_more_than_fits_fails_the_rest(self, store, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_OPEN_CALLS", "1")
        config.get_settings.cache_clear()
        add_endpoint(store)
        add_trigger(store)

        firings = await evaluate_triggers(store, CONN, kind=TRIGGER_STATE)

        assert sorted(f.outcome for f in firings) == ["failed", "requested"]
        failed = next(f for f in firings if f.outcome == "failed")
        assert "AGENT_MAX_OPEN_CALLS" in failed.detail
        assert len(await CallStore(store).list_calls(CONN)) == 1

    def test_the_route_answers_429(self, store):
        app = create_app(
            Settings(bearer_token=TOKEN, agent_allowlist=("agent.example",), agent_max_open_calls=1)
        )
        with TestClient(app) as client:
            app.state.fuseki = store
            app.state.agent_sender = FakeSender(ok())
            assert client.post(
                "/agent-endpoint", json={"id": "triage", "url": "https://agent.example/run"},
                headers=auth(),
            ).status_code == 200
            assert client.post(
                "/agent-call", json={"endpoint": "triage", "task": "one"}, headers=auth()
            ).status_code == 200
            full = client.post(
                "/agent-call", json={"endpoint": "triage", "task": "two"}, headers=auth()
            )
            assert full.status_code == 429
            assert full.json()["detail"]["error"] == "too_many_open_calls"
