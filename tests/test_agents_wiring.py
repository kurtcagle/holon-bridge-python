"""Agent calls from the places that act on their own: triggers, pipelines and
the scheduler -- and the routes that register and operate them.

Everything runs against the in-memory RDF store, so a trigger really is
evaluated (condition SELECT, firing ledger, re-arm) and a call really lands in
the ledger. Only the network is replaced.

What these pin down:

* a trigger's agent action places calls *as the person who armed it*, and stops
  when that person loses the access, exactly as a rule trigger does;
* either of two switches is enough to hold a call for a person -- the trigger's
  ``reviewRequired`` and the endpoint's ``callReview``;
* nothing is sent from inside the evaluation: a policy-approved call waits as
  ``Approved`` for the scheduler's ``agent-dispatch`` job;
* a pipeline's agent stage needs a person, places a call, and ends ``Deferred``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from holonbridge import config, triggers as triggers_mod
from holonbridge.agents.ledger import CallStore
from holonbridge.agents.model import APPROVED, PENDING, RESPONDED
from holonbridge.agents.registry import endpoint_node, render_endpoint_turtle
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.messages import Message
from holonbridge.pipeline import PipelineError, load_manifest, run_pipeline
from holonbridge.scheduler.model import FiringRecord, Task
from holonbridge.scheduler.runner import Scheduler
from holonbridge.server import create_app
from holonbridge.trigger_registry import render_trigger_turtle, trigger_node
from holonbridge.triggers import TRIGGER_STATE, TRIGGER_TEMPORAL, evaluate_triggers, load_named_triggers

from ._identity import PERSON_IRI, auth
from ._rdfstore import RdfStore
from .test_agents import FakeSender, add_query, ok

HB = "https://w3id.org/holonbridge/"
BUILD = "https://w3id.org/databook/ns#"
CONN = Conn(base_url="http://fuseki", dataset="ds", overridden=False, bank_name="b")
TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_ALLOWLIST", "agent.example")
    monkeypatch.setenv("HB_AGENT_KEY", "k")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def store() -> RdfStore:
    s = RdfStore()
    # Two widgets match the condition.
    s.add_turtle("urn:ds:holons", "<urn:w:1> a <urn:Widget> . <urn:w:2> a <urn:Widget> .")
    add_query(s, "widgets", "SELECT ?focus WHERE { ?focus a <urn:Widget> }")
    return s


def add_endpoint(store: RdfStore, *, call_review: bool = True, status: str = "Active", **kw) -> None:
    store.add_turtle(
        "urn:ds:agents",
        render_endpoint_turtle(
            node=endpoint_node(CONN, "triage"), endpoint_id="triage", url="https://agent.example/run",
            status=status, label=None, description=None,
            auth_scheme=kw.get("auth_scheme", "none"), auth_header=None,
            credential_ref=kw.get("credential_ref"), signing_ref=None,
            timeout_seconds=30.0, max_response_bytes=1_000_000, max_request_bytes=1_000_000,
            call_review=call_review, registered_at="2026-11-01T00:00:00+00:00",
            registered_by=PERSON_IRI,
        ),
    )


def add_trigger(store: RdfStore, *, review_required: bool = True, armed_by: str | None = PERSON_IRI,
                kind: str = TRIGGER_STATE, **overrides) -> None:
    values = dict(
        node=trigger_node(CONN, "t1"), trigger_id="t1", trigger_kind=kind, condition="widgets",
        on_fire=None, review_required=review_required, status="Active", label=None,
        description=None, watched_predicates=[], registered_at="2026-11-01T00:00:00+00:00",
        armed_by=armed_by, agent_endpoint="triage", agent_task="Look at this widget.",
        agent_context=None, agent_target_graph=None,
    )
    values.update(overrides)
    store.add_turtle("urn:ds:named-triggers", render_trigger_turtle(**values))


async def fire(store, kind=TRIGGER_STATE):
    return await evaluate_triggers(store, CONN, kind=kind)


async def calls(store, **kw):
    return await CallStore(store).list_calls(CONN, **kw)


# --- the trigger action ---------------------------------------------------------------


class TestAgentTrigger:
    async def test_it_loads_with_its_action_and_not_a_rule(self, store):
        add_endpoint(store)
        add_trigger(store, agent_context="widgets", agent_target_graph="urn:ds:holons")
        trig = (await load_named_triggers(store, CONN)).by_id("t1")
        assert trig.is_agent
        assert trig.on_fire_rule_id == ""
        assert trig.agent_endpoint == "triage"
        assert trig.agent_task == "Look at this widget."
        assert trig.agent_context == "widgets"
        assert trig.agent_target_graph == "urn:ds:holons"
        assert trig.summary()["onFireAgent"]["endpoint"] == "triage"

    async def test_a_trigger_naming_both_actions_or_no_task_is_skipped(self, store):
        both = (
            f'<urn:t:both> a <{triggers_mod.HEV_NAMESPACE}NamedTrigger> ; '
            f'<{triggers_mod.HEV_NAMESPACE}id> "both" ; <{triggers_mod.HEV_NAMESPACE}triggerKind> "StateTrigger" ; '
            f'<{triggers_mod.HEV_NAMESPACE}condition> "widgets" ; <{triggers_mod.HEV_NAMESPACE}onFire> "r" ; '
            f'<{triggers_mod.HEV_NAMESPACE}onFireAgent> "triage" ; <{triggers_mod.HEV_NAMESPACE}agentTask> "x" .'
            f'<urn:t:notask> a <{triggers_mod.HEV_NAMESPACE}NamedTrigger> ; '
            f'<{triggers_mod.HEV_NAMESPACE}id> "notask" ; <{triggers_mod.HEV_NAMESPACE}triggerKind> "StateTrigger" ; '
            f'<{triggers_mod.HEV_NAMESPACE}condition> "widgets" ; <{triggers_mod.HEV_NAMESPACE}onFireAgent> "triage" .'
        )
        store.add_turtle("urn:ds:named-triggers", both)
        result = await load_named_triggers(store, CONN)
        assert result.triggers == []
        assert any("both onFire and onFireAgent" in w for w in result.warnings)
        assert any("no agentTask" in w for w in result.warnings)

    async def test_each_matching_focus_gets_one_call_placed_as_the_definer(self, store):
        add_endpoint(store)
        add_trigger(store)
        firings = await fire(store)

        assert sorted(f.outcome for f in firings) == ["requested", "requested"]
        placed = await calls(store)
        assert sorted(c.focus for c in placed) == ["urn:w:1", "urn:w:2"]
        for call in placed:
            assert call.status == PENDING  # nothing sent, a person decides
            assert call.requested_by == PERSON_IRI
            assert call.via_trigger == trigger_node(CONN, "t1")
            assert json.loads(call.body)["task"] == "Look at this widget."
        assert all(f.call_iri for f in firings)

    async def test_the_firing_is_recorded_so_a_focus_fires_once(self, store):
        add_endpoint(store)
        add_trigger(store)
        await fire(store)
        again = await fire(store)
        assert again == []  # the same focus nodes do not fire twice
        assert len(await calls(store)) == 2
        ledger = store.graph("urn:ds:trigger-log").serialize(format="turtle")
        assert "requestedCall" in ledger

    async def test_a_rearmed_focus_waits_while_its_earlier_call_is_open(self, store):
        add_endpoint(store)
        add_trigger(store)
        await fire(store)
        # The condition goes false and true again for w:1 while its call is still pending.
        store.graph("urn:ds:holons").remove((None, None, None))
        await fire(store)  # falling edge: clears both firings
        store.add_turtle("urn:ds:holons", "<urn:w:1> a <urn:Widget> .")
        firings = await fire(store)
        assert [f.outcome for f in firings] == ["skipped-open-call"]
        assert len(await calls(store)) == 2  # no third call

    async def test_a_resolved_call_lets_a_rearmed_focus_fire_again(self, store):
        add_endpoint(store)
        add_trigger(store)
        await fire(store)
        for call in await calls(store):
            from holonbridge.agents import service as svc

            await svc.reject_call(store, CONN, call.id, rejecter=PERSON_IRI)
        store.graph("urn:ds:holons").remove((None, None, None))
        await fire(store)
        store.add_turtle("urn:ds:holons", "<urn:w:1> a <urn:Widget> .")
        firings = await fire(store)
        assert [f.outcome for f in firings] == ["requested"]

    async def test_either_switch_holds_a_call_for_a_person(self, store):
        add_endpoint(store, call_review=False)
        add_trigger(store, review_required=True)  # the trigger insists
        await fire(store)
        assert {c.status for c in await calls(store)} == {PENDING}

    async def test_the_endpoint_can_insist_even_when_the_trigger_does_not(self, store):
        add_endpoint(store, call_review=True)
        add_trigger(store, review_required=False)
        await fire(store)
        assert {c.status for c in await calls(store)} == {PENDING}

    async def test_both_switches_off_queues_the_call_without_sending_it(self, store):
        add_endpoint(store, call_review=False)
        add_trigger(store, review_required=False)
        sender = FakeSender()
        await fire(store)
        assert {c.status for c in await calls(store)} == {APPROVED}
        assert sender.sent == []  # nothing left from inside the evaluation
        assert {c.approval_mode for c in await calls(store)} == {"policy"}

    async def test_a_definer_who_lost_the_ledger_grant_stops_the_trigger(self, store):
        add_endpoint(store)
        add_trigger(store)
        store.denied_write_graphs = {"urn:ds:agent-calls"}
        firings = await fire(store)
        assert {f.outcome for f in firings} == {"skipped-definer-unauthorised"}
        assert await calls(store) == []

    async def test_a_definer_who_can_no_longer_read_the_context_stops_the_trigger(self, store):
        add_endpoint(store)
        add_query(store, "wide", "SELECT ?s WHERE { GRAPH ?g { ?s ?p ?o } }")
        add_trigger(store, agent_context="wide")
        firings = await fire(store)
        assert {f.outcome for f in firings} == {"skipped-definer-unauthorised"}

    async def test_a_hand_authored_agent_trigger_cannot_place_calls(self, store):
        # No recorded definer means no one to place the call as.
        add_endpoint(store)
        add_trigger(store, armed_by=None)
        firings = await fire(store)
        assert {f.outcome for f in firings} == {"failed"}
        assert "recorded definer" in firings[0].detail
        assert await calls(store) == []

    async def test_an_unknown_endpoint_fails_without_calls(self, store):
        add_trigger(store)  # no endpoint registered
        firings = await fire(store)
        assert [f.outcome for f in firings] == ["failed"]
        assert "unknown agent endpoint" in firings[0].detail

    async def test_a_suspended_endpoint_fails_each_focus(self, store):
        add_endpoint(store, status="Suspended")
        add_trigger(store)
        firings = await fire(store)
        # The definer check reports the endpoint as unusable, so nothing is placed.
        assert not [f for f in firings if f.outcome == "requested"]
        assert await calls(store) == []

    async def test_context_and_target_travel_with_each_call(self, store):
        add_endpoint(store)
        store.add_turtle("urn:ds:holons", '<urn:w:1> <urn:name> "One" .')
        add_query(
            store, "about",
            "CONSTRUCT { {{focus}} <urn:name> ?n } WHERE { {{focus}} <urn:name> ?n }",
            focus_param=True,
        )
        add_trigger(store, agent_context="about", agent_target_graph="urn:ds:holons")
        await fire(store)
        one = next(c for c in await calls(store) if c.focus == "urn:w:1")
        body = json.loads(one.body)
        assert "One" in body["context"]["content"]
        assert body["respond"]["targetGraph"] == "urn:ds:holons"
        assert one.target_graph == "urn:ds:holons"

    async def test_a_temporal_agent_trigger_is_placed_by_the_sweep(self, store):
        add_endpoint(store)
        add_trigger(store, kind=TRIGGER_TEMPORAL)
        assert await fire(store, TRIGGER_STATE) == []
        sweep = await fire(store, TRIGGER_TEMPORAL)
        assert {f.outcome for f in sweep} == {"requested"}


# --- the scheduler's dispatch job ---------------------------------------------------------


def scheduler(store, sender) -> Scheduler:
    return Scheduler(store, admin_conn=CONN, tick_seconds=1000.0, agent_sender=sender)


async def run_job(store, sender, job="agent-dispatch") -> FiringRecord:
    task = Task(iri="urn:ds:task:d", id="d", maintenance=job)
    record = FiringRecord(iri="urn:ds:firing:1", task_iri=task.iri, outcome="")
    await scheduler(store, sender)._run_maintenance(task, record, CONN)
    return record


class TestDispatchJob:
    async def test_it_sends_policy_approved_calls_and_leaves_the_rest(self, store):
        add_endpoint(store, call_review=False)
        add_trigger(store, review_required=False)
        await fire(store)
        waiting = await _place_pending(store)

        sender = FakeSender(ok())
        record = await run_job(store, sender)

        assert record.outcome == "committed"
        assert "2 sent" in record.detail and "2 responded" in record.detail
        assert len(sender.sent) == 2
        assert (await CallStore(store).get(CONN, waiting.id)).status == PENDING
        assert {c.status for c in await calls(store, status=RESPONDED)} == {RESPONDED}

    async def test_an_empty_queue_is_a_quiet_success(self, store):
        add_endpoint(store)
        record = await run_job(store, FakeSender())
        assert record.outcome == "committed" and record.detail.startswith("0 queued")

    async def test_the_alias_name_works_too(self, store):
        add_endpoint(store)
        assert (await run_job(store, FakeSender(), job="dispatch-agent-calls")).outcome == "committed"

    async def test_unknown_jobs_are_still_unknown(self, store):
        assert (await run_job(store, FakeSender(), job="nonsense")).outcome == "failed"


async def _place_pending(store):
    from holonbridge.agents import service as svc
    from holonbridge.agents.registry import load_endpoints

    endpoint = (await load_endpoints(store, CONN)).by_id("triage")
    return await svc.request_call(
        store, CONN, requested_by=PERSON_IRI, endpoint=endpoint, task="manual",
        settings=Settings(bearer_token="t", agent_allowlist=("agent.example",)),
    )


# --- the pipeline stage ---------------------------------------------------------------------


def add_pipeline(store: RdfStore, **stage: str) -> None:
    fields = {"transformer": "agent", "agentEndpoint": "triage", "promptText": "Summarise {{input}}."}
    fields.update(stage)
    pairs = " ; ".join(f'<{BUILD}{k}> "{v}"' for k, v in fields.items() if v is not None)
    store.add_turtle(
        "urn:ds:pipeline:p1",
        f'<urn:ds:pipeline:p1:stage> a <{BUILD}Stage> ; <{BUILD}id> "ask" ; {pairs} .',
    )


async def run(store, requested_by=PERSON_IRI, params=None):
    manifest = await load_manifest(store, CONN, "p1")
    message = Message(id="m1", pipeline="p1")
    await run_pipeline(CONN, store, manifest, message, params=params, requested_by=requested_by)
    return message


class TestPipelineStage:
    async def test_the_stage_places_a_call_and_ends_deferred(self, store):
        add_endpoint(store)
        add_pipeline(store)
        message = await run(store, params={"input": "the incident"})
        [stage] = message.stages
        assert stage.status == "Deferred"
        [call] = await calls(store)
        assert stage.result == call.id
        assert call.status == PENDING
        assert call.requested_by == PERSON_IRI
        assert (call.via_pipeline, call.via_stage) == ("p1", "ask")
        body = json.loads(call.body)
        assert body["task"] == "Summarise the incident."
        assert body["input"]["pipeline"] == "p1" and body["input"]["stage"] == "ask"

    async def test_a_pipeline_never_queues_a_call_for_policy_dispatch(self, store):
        add_endpoint(store, call_review=False)
        add_pipeline(store)
        await run(store)
        assert {c.status for c in await calls(store)} == {PENDING}

    async def test_a_run_without_a_person_cannot_place_a_call(self, store):
        add_endpoint(store)
        add_pipeline(store)
        message = await run(store, requested_by=None)
        assert message.stages[0].status == "Failed"
        assert "no identified person" in message.stages[0].detail
        assert await calls(store) == []

    @pytest.mark.parametrize(
        "stage, fragment",
        [
            ({"agentEndpoint": "ghost"}, "unknown agent endpoint"),
            ({"promptText": None}, "no promptText"),
            ({"agentEndpoint": None}, "names no agentEndpoint"),
            ({"agentContext": "nothing"}, "unknown context query"),
        ],
    )
    async def test_a_misconfigured_stage_fails_with_a_reason(self, store, stage, fragment):
        add_endpoint(store)
        add_pipeline(store, **stage)
        message = await run(store)
        assert message.stages[0].status == "Failed"
        assert fragment in message.stages[0].detail

    async def test_a_person_without_the_ledger_grant_fails_the_stage(self, store):
        add_endpoint(store)
        add_pipeline(store)
        store.denied_write_graphs = {"urn:ds:agent-calls"}
        message = await run(store)
        assert message.stages[0].status == "Failed"
        assert "could not place its call" in message.stages[0].detail
        assert await calls(store) == []

    async def test_the_stage_is_listed_as_executable(self, store):
        add_endpoint(store)
        add_pipeline(store)
        node = (await load_manifest(store, CONN, "p1")).by_id("ask")
        assert node.summary()["executable"] is True
        assert node.summary()["agent"] == "triage"


# --- the routes -----------------------------------------------------------------------------


@pytest.fixture
def api(store):
    app = create_app(Settings(bearer_token=TOKEN, agent_allowlist=("agent.example",)))
    with TestClient(app) as test_client:
        app.state.fuseki = store
        app.state.agent_sender = FakeSender(ok(summary="routed"))
        yield test_client, store


def post(client, path, body=None, **kw):
    return client.post(path, json=body or {}, headers=auth(), **kw)


class TestRoutes:
    def test_the_whole_lifecycle_over_http(self, api):
        client, store = api
        reg = post(client, "/agent-endpoint", {"id": "triage", "url": "https://agent.example/run"})
        assert reg.status_code == 200, reg.text

        placed = post(client, "/agent-call", {"endpoint": "triage", "task": "do it"})
        assert placed.status_code == 200, placed.text
        call_id = placed.json()["id"]
        assert placed.json()["callStatus"] == PENDING
        assert json.loads(placed.json()["request"])["task"] == "do it"

        done = post(client, f"/agent-call/{call_id}/approve")
        assert done.status_code == 200, done.text
        assert done.json()["callStatus"] == RESPONDED

        again = post(client, f"/agent-call/{call_id}/approve")
        assert again.status_code == 409

        listed = client.get("/agent-calls", headers=auth()).json()
        assert [c["id"] for c in listed["calls"]] == [call_id]

    def test_registering_a_trigger_with_an_agent_action(self, api):
        client, store = api
        post(client, "/agent-endpoint", {"id": "triage", "url": "https://agent.example/run"})
        resp = post(
            client, "/named-trigger",
            {"id": "t1", "trigger_kind": "StateTrigger", "condition": "widgets",
             "on_fire_agent": "triage", "agent_task": "look"},
        )
        assert resp.status_code == 200, resp.text
        trig = client.get("/named-trigger/t1", headers=auth()).json()
        assert trig["onFireAgent"]["endpoint"] == "triage"
        assert trig["armedBy"] == PERSON_IRI

    @pytest.mark.parametrize(
        "extra",
        [
            {},  # neither action
            {"on_fire": "r", "on_fire_agent": "triage", "agent_task": "x"},  # both
            {"on_fire_agent": "triage"},  # no task
            {"on_fire_agent": "ghost", "agent_task": "x"},  # unknown endpoint
        ],
    )
    def test_a_trigger_must_name_exactly_one_valid_action(self, api, extra):
        client, store = api
        post(client, "/agent-endpoint", {"id": "triage", "url": "https://agent.example/run"})
        resp = post(
            client, "/named-trigger",
            {"id": "t2", "trigger_kind": "StateTrigger", "condition": "widgets", **extra},
        )
        assert resp.status_code == 400
        assert client.get("/named-trigger/t2", headers=auth()).status_code == 404

    def test_a_definer_without_the_ledger_grant_cannot_register_an_agent_trigger(self, api):
        client, store = api
        post(client, "/agent-endpoint", {"id": "triage", "url": "https://agent.example/run"})
        store.denied_write_graphs = {"urn:ds:agent-calls"}
        resp = post(
            client, "/named-trigger",
            {"id": "t3", "trigger_kind": "StateTrigger", "condition": "widgets",
             "on_fire_agent": "triage", "agent_task": "x"},
        )
        assert resp.status_code == 403
