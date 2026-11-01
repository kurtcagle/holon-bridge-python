"""The agent gateway: policy, registry, sender, and the call lifecycle.

The lifecycle tests run against a real (in-memory) RDF store -- see
``_rdfstore`` -- because the ledger's value is that a sequence of SPARQL
updates leaves the right state behind. Only the network is faked.

What these pin down, in the order they appear:

* a credential can only travel to a host the operator listed, and only from the
  namespace the operator provisioned;
* an HTTP exchange never leaks the secret, never follows a redirect, and gives
  up on an oversized or slow reply;
* a call is recorded before it is sent, sent at most once per approval, and what
  is sent is byte-for-byte what was stored for the approver to read;
* an agent's answer is only ever a staged candidate, never a write.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from holonbridge.agents import policy
from holonbridge.agents import service as svc
from holonbridge.agents.ledger import CallStore
from holonbridge.agents.model import (
    APPROVED,
    FAILED,
    PENDING,
    REJECTED,
    RESPONDED,
    SENDING,
    Endpoint,
)
from holonbridge.agents.registry import (
    InvalidEndpoint,
    endpoint_node,
    load_endpoints,
    render_endpoint_turtle,
    validate_header_name,
)
from holonbridge.agents.sender import HttpAgentSender, Reply, SendFailed, sign
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.egress import EndpointRefused
from holonbridge.trigger_authz import TriggerDenied
from holonbridge.triggers import list_candidates

from ._identity import PERSON_IRI
from ._rdfstore import RdfStore

HB = "https://w3id.org/holonbridge/"
CONN = Conn(base_url="http://fuseki", dataset="ds", overridden=False, bank_name="b")
LEDGER = "urn:ds:agent-calls"
REGISTRY = "urn:ds:agents"
CANDIDATES = "urn:ds:candidates"
TARGET = "urn:ds:holons"
SETTINGS = Settings(bearer_token="t", agent_allowlist=("agent.example",))
SECRET = "s3cr3t-do-not-leak"


# --- fixtures and doubles ----------------------------------------------------------


def make_endpoint(**overrides) -> Endpoint:
    values = dict(id="triage", iri=endpoint_node(CONN, "triage"), url="https://agent.example/run")
    values.update(overrides)
    return Endpoint(**values)


def ok(**document) -> Reply:
    text = json.dumps({"status": "ok", "summary": "done", **document})
    return Reply(200, "application/json", text, len(text), False)


class FakeSender:
    """Records what it was asked to send and answers with a scripted reply."""

    def __init__(self, reply: Reply | None = None, *, exc: Exception | None = None, delay: float = 0):
        self.reply = reply or ok()
        self.exc = exc
        self.delay = delay
        self.sent: list[tuple[str, str]] = []  # (call id, body)

    async def send(self, endpoint, call, body):
        self.sent.append((call.id, body))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.reply


@pytest.fixture
def store() -> RdfStore:
    return RdfStore()


@pytest.fixture(autouse=True)
def _agent_secret(monkeypatch):
    monkeypatch.setenv("HB_AGENT_KEY", SECRET)
    monkeypatch.setenv("HB_AGENT_SIGNING", "signing-key")


def add_query(store: RdfStore, qid: str, sparql: str, *, focus_param: bool = False) -> None:
    extra = ""
    if focus_param:
        extra = f'; <{HB}parameter> [ <{HB}name> "focus" ; <{HB}datatype> "anyURI" ]'
    store.add_turtle(
        "urn:ds:named-queries",
        f'<urn:ds:named-query:{qid}> a <{HB}NamedQuery> ; <{HB}id> "{qid}" ; '
        f'<{HB}sparql> """{sparql}""" {extra} .',
    )


async def request(store, endpoint=None, **kwargs):
    kwargs.setdefault("requested_by", PERSON_IRI)
    kwargs.setdefault("task", "triage this")
    kwargs.setdefault("settings", SETTINGS)
    return await svc.request_call(store, CONN, endpoint=endpoint or make_endpoint(), **kwargs)


# --- policy ------------------------------------------------------------------------


class TestCredentialReferences:
    def test_namespaced_env_reference_is_accepted(self):
        assert policy.parse_credential_ref("env:HB_AGENT_KEY", settings=SETTINGS) == "HB_AGENT_KEY"

    @pytest.mark.parametrize(
        "ref",
        [
            "env:DATABASE_PASSWORD",  # outside the operator's namespace
            "env:hb_agent_lower",  # not an upper-case variable name
            "file:/etc/passwd",  # only env: is supported
            "HB_AGENT_KEY",  # no scheme
            "env:HB_AGENT_KEY; rm",  # not a name at all
            "",
        ],
    )
    def test_anything_else_is_refused(self, ref):
        with pytest.raises(policy.CredentialRefused):
            policy.parse_credential_ref(ref, settings=SETTINGS)

    def test_prefix_is_the_operators_to_set(self):
        custom = Settings(bearer_token="t", agent_credential_prefix="AGENTS_")
        assert policy.parse_credential_ref("env:AGENTS_X", settings=custom) == "AGENTS_X"
        with pytest.raises(policy.CredentialRefused):
            policy.parse_credential_ref("env:HB_AGENT_KEY", settings=custom)

    def test_unset_variable_is_named_but_nothing_else_is_revealed(self, monkeypatch):
        monkeypatch.delenv("HB_AGENT_KEY")
        with pytest.raises(policy.CredentialRefused) as caught:
            policy.resolve_secret("env:HB_AGENT_KEY", settings=SETTINGS)
        assert "env:HB_AGENT_KEY" in str(caught.value)


class TestEndpointPolicy:
    async def test_a_credential_needs_an_allowlist_at_all(self):
        bare = Settings(bearer_token="t")
        with pytest.raises(EndpointRefused, match="no AGENT_ALLOWLIST"):
            await policy.check_agent_endpoint(
                "https://agent.example/run", has_credentials=True, settings=bare
            )

    async def test_a_credential_only_goes_to_a_listed_host(self):
        with pytest.raises(EndpointRefused, match="agent allowlist"):
            await policy.check_agent_endpoint(
                "https://evil.example/run", has_credentials=True, settings=SETTINGS
            )
        await policy.check_agent_endpoint(
            "https://agent.example/run", has_credentials=True, settings=SETTINGS
        )

    async def test_without_credentials_the_default_policy_applies(self):
        bare = Settings(bearer_token="t")

        async def public(host, port):
            return ["93.184.216.34"]

        async def private(host, port):
            return ["10.0.0.5"]

        await policy.check_agent_endpoint(
            "https://anywhere.example/run", has_credentials=False, settings=bare, resolver=public
        )
        with pytest.raises(EndpointRefused, match="non-public"):
            await policy.check_agent_endpoint(
                "https://anywhere.example/run", has_credentials=False, settings=bare, resolver=private
            )
        with pytest.raises(EndpointRefused, match="https"):
            await policy.check_agent_endpoint(
                "http://anywhere.example/run", has_credentials=False, settings=bare, resolver=public
            )

    async def test_refusals_name_the_agent_setting_not_the_webhook_one(self):
        bare = Settings(bearer_token="t")

        async def private(host, port):
            return ["10.0.0.5"]

        with pytest.raises(EndpointRefused) as caught:
            await policy.check_agent_endpoint(
                "https://x.example/run", has_credentials=False, settings=bare, resolver=private
            )
        assert "AGENT_ALLOWLIST" in str(caught.value)
        assert "WEBHOOK_ALLOWLIST" not in str(caught.value)


# --- registry ----------------------------------------------------------------------


def render(**overrides) -> str:
    values = dict(
        node=endpoint_node(CONN, "triage"), endpoint_id="triage", url="https://agent.example/run",
        status="Active", label="Triage", description=None, auth_scheme="bearer", auth_header=None,
        credential_ref="env:HB_AGENT_KEY", signing_ref=None, timeout_seconds=20.0,
        max_response_bytes=50_000, max_request_bytes=60_000, call_review=True,
        registered_at="2026-11-01T00:00:00+00:00", registered_by=PERSON_IRI,
    )
    values.update(overrides)
    return render_endpoint_turtle(**values)


class TestRegistry:
    async def test_round_trip(self, store):
        store.add_turtle(REGISTRY, render())
        loaded = (await load_endpoints(store, CONN)).by_id("triage")
        assert loaded.url == "https://agent.example/run"
        assert loaded.auth_scheme == "bearer"
        assert loaded.credential_ref == "env:HB_AGENT_KEY"
        assert loaded.timeout_seconds == 20.0
        assert loaded.max_response_bytes == 50_000
        assert loaded.call_review is True
        assert loaded.registered_by == PERSON_IRI
        assert loaded.active

    async def test_the_summary_carries_the_reference_never_a_secret(self, store):
        store.add_turtle(REGISTRY, render())
        endpoint = (await load_endpoints(store, CONN)).by_id("triage")
        assert SECRET not in json.dumps(endpoint.summary())
        assert endpoint.summary()["credentialRef"] == "env:HB_AGENT_KEY"

    async def test_review_defaults_to_required_when_unstated(self, store):
        store.add_turtle(
            REGISTRY,
            f'<urn:ds:agent:bare> a <{HB}AgentEndpoint> ; <{HB}id> "bare" ; '
            f'<{HB}endpoint> "https://agent.example/x" .',
        )
        assert (await load_endpoints(store, CONN)).by_id("bare").call_review is True

    async def test_limits_are_clamped_not_trusted(self, store):
        store.add_turtle(
            REGISTRY,
            f'<urn:ds:agent:wild> a <{HB}AgentEndpoint> ; <{HB}id> "wild" ; '
            f'<{HB}endpoint> "https://agent.example/x" ; <{HB}timeoutSeconds> "99999" ; '
            f'<{HB}maxResponseBytes> "999999999999" .',
        )
        wild = (await load_endpoints(store, CONN)).by_id("wild")
        assert wild.timeout_seconds == 120.0
        assert wild.max_response_bytes == 10_000_000

    async def test_an_unknown_auth_scheme_is_skipped_with_a_warning(self, store):
        store.add_turtle(
            REGISTRY,
            f'<urn:ds:agent:odd> a <{HB}AgentEndpoint> ; <{HB}id> "odd" ; '
            f'<{HB}endpoint> "https://agent.example/x" ; <{HB}authScheme> "kerberos" .',
        )
        result = await load_endpoints(store, CONN)
        assert result.by_id("odd") is None
        assert any("kerberos" in w for w in result.warnings)

    def test_auth_combinations_that_make_no_sense_are_refused(self):
        with pytest.raises(InvalidEndpoint, match="needs a credentialRef"):
            render(credential_ref=None)
        with pytest.raises(InvalidEndpoint, match="authScheme is 'none'"):
            render(auth_scheme="none", credential_ref="env:HB_AGENT_KEY")
        with pytest.raises(InvalidEndpoint, match="only meaningful"):
            render(auth_header="X-Key")
        with pytest.raises(InvalidEndpoint):
            render(auth_scheme="header", auth_header=None)

    @pytest.mark.parametrize(
        "name", ["Host", "content-length", "Idempotency-Key", "X-HolonBridge-Signature", "bad name", ""]
    )
    def test_reserved_and_malformed_header_names_are_refused(self, name):
        with pytest.raises(InvalidEndpoint):
            validate_header_name(name)

    def test_a_clean_header_name_passes(self):
        assert validate_header_name("X-Api-Key") == "X-Api-Key"


# --- sender ------------------------------------------------------------------------


def transport(handler):
    return httpx.MockTransport(handler)


def sender_for(handler, **kwargs) -> HttpAgentSender:
    return HttpAgentSender(settings=SETTINGS, transport=transport(handler), **kwargs)


def a_call(store=None):
    from holonbridge.agents.model import Call

    return Call(id="c1", iri="urn:ds:agent-call:c1", endpoint_id="triage", body='{"x":1}')


class TestHttpSender:
    async def test_bearer_credential_and_identity_headers(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"status": "ok"})

        endpoint = make_endpoint(auth_scheme="bearer", credential_ref="env:HB_AGENT_KEY")
        await sender_for(handler).send(endpoint, a_call(), '{"x":1}')
        assert seen["authorization"] == f"Bearer {SECRET}"
        assert seen["idempotency-key"] == "c1"
        assert seen["x-holonbridge-call"] == "c1"
        assert seen["content-type"] == "application/json"
        assert seen["body"] == '{"x":1}'

    async def test_custom_header_credential(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, json={})

        endpoint = make_endpoint(
            auth_scheme="header", auth_header="X-Api-Key", credential_ref="env:HB_AGENT_KEY"
        )
        await sender_for(handler).send(endpoint, a_call(), "{}")
        assert seen["x-api-key"] == SECRET
        assert "authorization" not in seen

    async def test_body_signature_verifies_and_binds_the_timestamp(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, json={})

        endpoint = make_endpoint(signing_ref="env:HB_AGENT_SIGNING")
        await sender_for(handler).send(endpoint, a_call(), '{"x":1}')
        stamp = seen["x-holonbridge-timestamp"]
        expected = hmac.new(b"signing-key", f'{stamp}.{{"x":1}}'.encode(), hashlib.sha256).hexdigest()
        assert seen["x-holonbridge-signature"] == f"sha256={expected}"
        assert sign("signing-key", stamp, '{"x":1}') == f"sha256={expected}"
        # A different timestamp would not verify: the timestamp is signed.
        assert sign("signing-key", str(int(stamp) + 1), '{"x":1}') != seen["x-holonbridge-signature"]

    async def test_no_credentials_means_no_authorization_header(self):
        seen = {}

        def handler(request):
            seen.update(request.headers)
            return httpx.Response(200, json={})

        await sender_for(handler).send(make_endpoint(), a_call(), "{}")
        assert "authorization" not in seen

    async def test_redirects_are_not_followed(self):
        hits = []

        def handler(request):
            hits.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://elsewhere.example/steal"})

        reply = await sender_for(handler).send(make_endpoint(), a_call(), "{}")
        assert reply.status_code == 302
        assert hits == ["https://agent.example/run"]

    async def test_an_oversized_reply_is_abandoned_not_buffered(self):
        def handler(request):
            return httpx.Response(200, content=b"x" * 5_000)

        endpoint = make_endpoint(max_response_bytes=1_024)
        reply = await sender_for(handler).send(endpoint, a_call(), "{}")
        assert reply.truncated
        assert len(reply.text) <= 1_024

    async def test_a_slow_agent_times_out_as_a_failure(self):
        async def handler(request):
            await asyncio.sleep(5)
            return httpx.Response(200, json={})

        endpoint = make_endpoint(timeout_seconds=0.05)
        with pytest.raises(SendFailed, match="no complete response"):
            await sender_for(handler).send(endpoint, a_call(), "{}")

    async def test_a_missing_secret_fails_without_naming_any_value(self, monkeypatch):
        monkeypatch.delenv("HB_AGENT_KEY")
        endpoint = make_endpoint(auth_scheme="bearer", credential_ref="env:HB_AGENT_KEY")
        with pytest.raises(SendFailed) as caught:
            await sender_for(lambda r: httpx.Response(200)).send(endpoint, a_call(), "{}")
        assert "not set" in str(caught.value)

    async def test_the_endpoint_is_checked_again_at_send_time(self):
        # Stored when the allowlist allowed it; the operator has since tightened it.
        tightened = Settings(bearer_token="t", agent_allowlist=("other.example",))
        sender = HttpAgentSender(
            settings=tightened, transport=transport(lambda r: httpx.Response(200, json={}))
        )
        endpoint = make_endpoint(auth_scheme="bearer", credential_ref="env:HB_AGENT_KEY")
        with pytest.raises(SendFailed, match="endpoint refused"):
            await sender.send(endpoint, a_call(), "{}")

    async def test_a_credential_from_outside_the_namespace_is_not_resolved_at_send_time(self, monkeypatch):
        monkeypatch.setenv("DATABASE_PASSWORD", "hunter2")
        seen = []

        def handler(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json={})

        endpoint = make_endpoint(auth_scheme="bearer", credential_ref="env:DATABASE_PASSWORD")
        with pytest.raises(SendFailed):
            await sender_for(handler).send(endpoint, a_call(), "{}")
        assert seen == []  # nothing left the bridge

    async def test_transport_errors_become_short_failures(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        with pytest.raises(SendFailed, match="ConnectError"):
            await sender_for(handler).send(make_endpoint(), a_call(), "{}")


# --- placing a call ----------------------------------------------------------------


class TestRequestCall:
    async def test_a_call_is_recorded_and_nothing_is_sent(self, store):
        call = await request(store)
        assert call.status == PENDING
        stored = await CallStore(store).get(CONN, call.id)
        assert stored.status == PENDING
        assert stored.requested_by == PERSON_IRI
        assert stored.endpoint_id == "triage"

    async def test_the_stored_body_is_the_one_that_will_be_sent(self, store):
        call = await request(store, task="summarise", focus="urn:ds:thing:1", payload={"k": "v"})
        body = json.loads(call.body)
        assert body["schema"] == "holonbridge.agent-request/1"
        assert body["callId"] == call.id == body["idempotencyKey"]
        assert body["task"] == "summarise"
        assert body["focus"] == "urn:ds:thing:1"
        assert body["input"] == {"k": "v"}
        assert body["dataset"] == "ds"
        assert call.digest == "sha256:" + hashlib.sha256(call.body.encode()).hexdigest()
        assert (await CallStore(store).get(CONN, call.id)).body == call.body

    async def test_placing_calls_needs_the_ledger_grant(self, store):
        store.denied_write_graphs = {LEDGER}
        with pytest.raises(TriggerDenied) as caught:
            await request(store)
        assert caught.value.status == 403
        assert await CallStore(store).list_calls(CONN) == []

    async def test_an_unidentified_requester_is_refused(self, store):
        with pytest.raises(TriggerDenied) as caught:
            await request(store, requested_by=None)
        assert caught.value.status == 401

    async def test_an_inactive_endpoint_takes_no_calls(self, store):
        with pytest.raises(svc.CallInvalid, match="suspended"):
            await request(store, make_endpoint(status="Suspended"))

    async def test_an_empty_task_is_refused(self, store):
        with pytest.raises(svc.CallInvalid, match="needs a task"):
            await request(store, task="   ")

    async def test_an_endpoint_the_policy_would_now_refuse_takes_no_calls(self, store):
        with pytest.raises(svc.CallInvalid, match="cannot be used"):
            await request(store, make_endpoint(url="https://not-listed.example/run", credential_ref="env:HB_AGENT_KEY", auth_scheme="bearer"))

    async def test_an_oversized_request_is_refused_before_it_is_stored(self, store):
        with pytest.raises(svc.CallInvalid, match="over endpoint"):
            await request(store, make_endpoint(max_request_bytes=1_024), task="x" * 5_000)
        assert await CallStore(store).list_calls(CONN) == []

    async def test_a_target_graph_needs_write_access_to_it(self, store):
        store.denied_write_graphs = {TARGET}
        with pytest.raises(TriggerDenied):
            await request(store, target_graph=TARGET)

    async def test_a_target_graph_also_needs_the_candidates_graph(self, store):
        store.denied_write_graphs = {CANDIDATES}
        with pytest.raises(TriggerDenied):
            await request(store, target_graph=TARGET)

    async def test_policy_approval_queues_only_when_the_endpoint_allows_it(self, store):
        queued = await request(store, make_endpoint(call_review=False), approval="policy")
        assert queued.status == APPROVED
        assert queued.approval_mode == "policy"
        assert queued.approved_by == PERSON_IRI

        held = await request(store, make_endpoint(call_review=True), approval="policy")
        assert held.status == PENDING  # the endpoint insists on a person
        assert held.approved_by == ""

    async def test_a_human_request_is_never_self_approving(self, store):
        call = await request(store, make_endpoint(call_review=False), approval="human")
        assert call.status == PENDING


class TestContext:
    async def test_a_construct_context_is_run_now_and_stored_in_the_body(self, store):
        store.add_turtle(TARGET, '<urn:ds:thing:1> <urn:ds:name> "Widget" .')
        add_query(store, "ctx", "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")
        queries = await _queries(store)
        call = await request(store, context_query=queries.by_id("ctx"))
        context = json.loads(call.body)["context"]
        assert context["format"] == "text/turtle"
        assert "Widget" in context["content"]

    async def test_a_select_context_is_sent_as_rows_and_bounded(self, store, monkeypatch):
        for i in range(8):
            store.add_turtle(TARGET, f'<urn:ds:thing:{i}> <urn:ds:name> "n{i}" .')
        add_query(store, "rows", "SELECT ?s ?name WHERE { ?s <urn:ds:name> ?name }")
        monkeypatch.setattr(svc, "MAX_CONTEXT_ROWS", 3)
        queries = await _queries(store)
        call = await request(store, context_query=queries.by_id("rows"))
        context = json.loads(call.body)["context"]
        assert context["format"] == "json-rows"
        assert len(context["rows"]) == 3
        assert context["truncated"] is True

    async def test_focus_is_bound_only_into_a_query_that_declares_it(self, store):
        store.add_turtle(TARGET, '<urn:ds:a> <urn:ds:name> "A" . <urn:ds:b> <urn:ds:name> "B" .')
        add_query(
            store, "about",
            "CONSTRUCT { {{focus}} <urn:ds:name> ?n } WHERE { {{focus}} <urn:ds:name> ?n }",
            focus_param=True,
        )
        queries = await _queries(store)
        call = await request(store, context_query=queries.by_id("about"), focus="urn:ds:b")
        content = json.loads(call.body)["context"]["content"]
        assert '"B"' in content and '"A"' not in content

    async def test_an_undeclared_focus_placeholder_is_refused_not_guessed(self, store):
        add_query(store, "bad", "SELECT ?n WHERE { {{focus}} <urn:ds:name> ?n }")
        queries = await _queries(store)
        with pytest.raises(svc.CallInvalid, match="needs parameter"):
            await request(store, context_query=queries.by_id("bad"), focus="urn:ds:b")

    async def test_a_context_must_be_a_read(self, store):
        add_query(store, "writer", "INSERT DATA { <urn:ds:x> <urn:ds:y> <urn:ds:z> }")
        queries = await _queries(store)
        with pytest.raises(svc.CallInvalid, match="must SELECT or CONSTRUCT"):
            await request(store, context_query=queries.by_id("writer"))

    async def test_a_context_the_requester_cannot_read_is_refused(self, store):
        # ``GRAPH ?g`` can reach anything, so a non-admin may not use it.
        add_query(store, "wide", "SELECT ?s WHERE { GRAPH ?g { ?s ?p ?o } }")
        queries = await _queries(store)
        with pytest.raises(TriggerDenied) as caught:
            await request(store, context_query=queries.by_id("wide"))
        assert caught.value.status == 403
        assert await CallStore(store).list_calls(CONN) == []

    async def test_an_admin_may_use_a_wide_context(self, store):
        store.admin = True
        add_query(store, "wide", "SELECT ?s WHERE { GRAPH ?g { ?s ?p ?o } }")
        queries = await _queries(store)
        call = await request(store, context_query=queries.by_id("wide"))
        assert call.status == PENDING


async def _queries(store):
    from holonbridge.named_queries import load_named_queries

    return await load_named_queries(store, CONN)


# --- approval and sending ----------------------------------------------------------


async def approve(store, call, sender, endpoint=None):
    return await svc.approve_call(
        store, CONN, call.id, approver=PERSON_IRI, endpoint=endpoint or make_endpoint(), sender=sender
    )


class TestApproveAndSend:
    async def test_approval_sends_exactly_the_stored_body(self, store):
        call = await request(store)
        sender = FakeSender()
        done = await approve(store, call, sender)
        assert sender.sent == [(call.id, call.body)]
        assert done.status == RESPONDED
        stored = await CallStore(store).get(CONN, call.id)
        assert stored.status == RESPONDED
        assert stored.approved_by == PERSON_IRI
        assert stored.approval_mode == "human"
        assert stored.attempts == 1
        assert stored.http_status == 200
        assert stored.response_summary == "done"
        assert stored.completed_at

    async def test_a_reply_with_turtle_is_staged_and_never_written(self, store):
        call = await request(store, target_graph=TARGET)
        sender = FakeSender(ok(turtle='<urn:ds:thing:1> <urn:ds:status> "triaged" .'))
        done = await approve(store, call, sender)

        assert done.status == RESPONDED
        assert done.candidate
        # The target graph is untouched: the answer is only a proposal.
        assert len(store.graph(TARGET)) == 0
        pending = await list_candidates(store, CONN, status="Pending")
        assert [c.iri for c in pending] == [done.candidate]
        candidate = pending[0]
        assert candidate.agent_call_iri == call.iri
        assert candidate.agent_id == "triage"
        assert candidate.target_graph == TARGET
        assert "triaged" in candidate.turtle
        assert candidate.summary()["agentCall"] == call.iri

    async def test_turtle_with_no_target_graph_is_not_staged(self, store):
        call = await request(store)  # no target graph asked for
        done = await approve(store, call, FakeSender(ok(turtle='<urn:a> <urn:b> "c" .')))
        assert done.status == RESPONDED
        assert done.candidate == ""
        assert await list_candidates(store, CONN) == []

    async def test_a_reply_without_turtle_is_just_a_recorded_answer(self, store):
        call = await request(store, target_graph=TARGET)
        done = await approve(store, call, FakeSender(ok(result={"label": "urgent"})))
        assert done.status == RESPONDED and done.candidate == ""

    async def test_rdf12_turtle_is_passed_on_for_jena_to_judge(self, store):
        call = await request(store, target_graph=TARGET)
        rdf12 = '<urn:a> <urn:b> <urn:c> ~ <urn:r> .'
        done = await approve(store, call, FakeSender(ok(turtle=rdf12)))
        assert done.status == RESPONDED and done.candidate

    async def test_turtle_that_does_not_parse_fails_the_call(self, store):
        call = await request(store, target_graph=TARGET)
        done = await approve(store, call, FakeSender(ok(turtle="this is not turtle {{")))
        assert done.status == FAILED
        assert "did not parse" in done.error
        assert await list_candidates(store, CONN) == []

    @pytest.mark.parametrize(
        "reply, fragment",
        [
            (Reply(500, "text/plain", "boom", 4, False), "returned 500: boom"),
            (Reply(404, "text/plain", "nope", 4, False), "returned 404"),
            (Reply(302, "text/plain", "", 0, False), "redirect"),
            (Reply(200, "text/html", "<html>", 6, False), "not JSON"),
            (Reply(200, "application/json", "[1,2]", 5, False), "not an object"),
            (Reply(200, "application/json", '{"status":"error","summary":"cannot"}', 38, False), "'error': cannot"),
            (Reply(200, "application/json", '{"summary":"hi"}', 15, False), "None"),
            (Reply(200, "application/json", "{", 1, True), "exceeded"),
        ],
    )
    async def test_bad_replies_fail_the_call_with_a_reason(self, store, reply, fragment):
        call = await request(store)
        done = await approve(store, call, FakeSender(reply))
        assert done.status == FAILED
        assert fragment in done.error
        assert (await CallStore(store).get(CONN, call.id)).status == FAILED

    async def test_a_send_failure_is_recorded(self, store):
        call = await request(store)
        done = await approve(store, call, FakeSender(exc=SendFailed("ConnectError: refused")))
        assert done.status == FAILED and "ConnectError" in done.error

    async def test_a_sender_bug_cannot_strand_a_call_in_sending(self, store):
        call = await request(store)
        done = await approve(store, call, FakeSender(exc=RuntimeError("secret detail")))
        assert done.status == FAILED
        assert done.error == "sender error: RuntimeError"  # the type, not the message
        assert (await CallStore(store).get(CONN, call.id)).status == FAILED

    async def test_a_call_is_sent_at_most_once_per_approval(self, store):
        call = await request(store)
        sender = FakeSender()
        await approve(store, call, sender)
        with pytest.raises(svc.CallStateError, match="Responded"):
            await approve(store, call, sender)
        assert len(sender.sent) == 1

    async def test_racing_approvals_send_once(self, store):
        call = await request(store)
        sender = FakeSender(delay=0.05)
        results = await asyncio.gather(
            approve(store, call, sender), approve(store, call, sender), approve(store, call, sender),
            return_exceptions=True,
        )
        assert len(sender.sent) == 1
        assert sum(isinstance(r, svc.CallStateError) for r in results) == 2

    async def test_sending_is_recorded_before_the_network_is_touched(self, store):
        call = await request(store)
        observed = {}

        class Peeking(FakeSender):
            async def send(self, endpoint, call, body):
                observed["status"] = (await CallStore(store).get(CONN, call.id)).status
                return await super().send(endpoint, call, body)

        await approve(store, call, Peeking())
        assert observed["status"] == SENDING

    async def test_the_secret_never_reaches_the_ledger(self, store):
        call = await request(store, make_endpoint(auth_scheme="bearer", credential_ref="env:HB_AGENT_KEY"))
        bad = Reply(401, "text/plain", "invalid token", 13, False)
        await approve(store, call, FakeSender(bad))
        dump = store.graph(LEDGER).serialize(format="turtle")
        assert SECRET not in dump
        assert SECRET not in json.dumps((await CallStore(store).get(CONN, call.id)).detail())


class TestRejectAndRetry:
    async def test_rejecting_sends_nothing_and_closes_the_call(self, store):
        call = await request(store)
        done = await svc.reject_call(store, CONN, call.id, rejecter=PERSON_IRI, reason="not now")
        assert done.status == REJECTED
        stored = await CallStore(store).get(CONN, call.id)
        assert stored.rejected_reason == "not now" and stored.rejected_by == PERSON_IRI
        with pytest.raises(svc.CallStateError):
            await approve(store, call, FakeSender())

    async def test_a_sent_call_cannot_be_rejected(self, store):
        call = await request(store)
        await approve(store, call, FakeSender())
        with pytest.raises(svc.CallStateError):
            await svc.reject_call(store, CONN, call.id, rejecter=PERSON_IRI)

    async def test_an_unknown_call_is_not_found(self, store):
        with pytest.raises(svc.CallNotFound):
            await svc.reject_call(store, CONN, "nope", rejecter=PERSON_IRI)

    async def test_retry_resends_the_same_call_under_the_same_key(self, store):
        call = await request(store)
        sender = FakeSender(Reply(503, "text/plain", "later", 5, False))
        await approve(store, call, sender)
        sender.reply = ok()
        done = await svc.retry_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=sender
        )
        assert done.status == RESPONDED
        assert done.attempts == 2
        assert done.error == ""
        assert [sent[0] for sent in sender.sent] == [call.id, call.id]
        assert sender.sent[0][1] == sender.sent[1][1]  # same bytes, same key

    async def test_only_a_failed_call_can_be_retried(self, store):
        call = await request(store)
        with pytest.raises(svc.CallStateError, match="PendingApproval"):
            await svc.retry_call(
                store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=FakeSender()
            )
        await approve(store, call, FakeSender())
        with pytest.raises(svc.CallStateError, match="Responded"):
            await svc.retry_call(
                store, CONN, call.id, approver=PERSON_IRI, endpoint=make_endpoint(), sender=FakeSender()
            )

    async def test_a_sending_call_is_retriable_only_once_presumed_lost(self, store):
        call = await request(store)
        stored = await CallStore(store).get(CONN, call.id)
        endpoint = make_endpoint(timeout_seconds=10.0)

        await CallStore(store).replace(CONN, stored, status=SENDING, started_at=svc.now())
        with pytest.raises(svc.CallStateError, match="Sending"):  # in flight, probably
            await svc.retry_call(
                store, CONN, call.id, approver=PERSON_IRI, endpoint=endpoint, sender=FakeSender()
            )

        await CallStore(store).replace(CONN, stored, started_at="2020-01-01T00:00:00+00:00")
        done = await svc.retry_call(
            store, CONN, call.id, approver=PERSON_IRI, endpoint=endpoint, sender=FakeSender()
        )
        assert done.status == RESPONDED


# --- dispatch ----------------------------------------------------------------------


class TestDispatch:
    async def queue(self, store, n=1, **kwargs):
        return [
            await request(store, make_endpoint(call_review=False), approval="policy", **kwargs)
            for _ in range(n)
        ]

    async def test_queued_calls_are_sent_and_pending_ones_are_not(self, store):
        [queued] = await self.queue(store)
        waiting = await request(store)  # PendingApproval
        sender = FakeSender()
        summary = await svc.dispatch_approved(
            store, CONN, endpoints={"triage": make_endpoint(call_review=False)}, sender=sender
        )
        assert summary == {"queued": 1, "sent": 1, "responded": 1, "failed": 0}
        assert [s[0] for s in sender.sent] == [queued.id]
        assert (await CallStore(store).get(CONN, waiting.id)).status == PENDING

    async def test_the_limit_is_respected_oldest_first(self, store):
        calls = await self.queue(store, n=3)
        sender = FakeSender()
        summary = await svc.dispatch_approved(
            store, CONN, endpoints={"triage": make_endpoint(call_review=False)}, sender=sender, limit=2
        )
        assert summary["sent"] == 2
        assert len(sender.sent) == 2
        remaining = await CallStore(store).list_calls(CONN, status=APPROVED)
        assert len(remaining) == 1

    async def test_a_requester_who_lost_the_grant_stops_the_call(self, store):
        [queued] = await self.queue(store)
        store.denied_write_graphs = {LEDGER}  # revoked while it sat in the queue
        sender = FakeSender()
        summary = await svc.dispatch_approved(
            store, CONN, endpoints={"triage": make_endpoint(call_review=False)}, sender=sender
        )
        assert sender.sent == []
        assert summary["failed"] == 1
        stored = await CallStore(store).get(CONN, queued.id)
        assert stored.status == FAILED
        assert "may no longer place this call" in stored.error

    async def test_a_suspended_or_vanished_endpoint_stops_the_call(self, store):
        [queued] = await self.queue(store)
        sender = FakeSender()
        await svc.dispatch_approved(store, CONN, endpoints={}, sender=sender)
        stored = await CallStore(store).get(CONN, queued.id)
        assert stored.status == FAILED and "unknown" in stored.error

        [again] = await self.queue(store)
        await svc.dispatch_approved(
            store, CONN, endpoints={"triage": make_endpoint(call_review=False, status="Suspended")},
            sender=sender,
        )
        assert "suspended" in (await CallStore(store).get(CONN, again.id)).error
        assert sender.sent == []

    async def test_a_failed_dispatch_is_not_retried_by_the_next_run(self, store):
        await self.queue(store)
        sender = FakeSender(Reply(500, "text/plain", "x", 1, False))
        endpoints = {"triage": make_endpoint(call_review=False)}
        await svc.dispatch_approved(store, CONN, endpoints=endpoints, sender=sender)
        await svc.dispatch_approved(store, CONN, endpoints=endpoints, sender=sender)
        assert len(sender.sent) == 1  # a failure needs a person's retry

    async def test_an_empty_queue_is_a_quiet_no_op(self, store):
        summary = await svc.dispatch_approved(store, CONN, endpoints={}, sender=FakeSender())
        assert summary == {"queued": 0, "sent": 0, "responded": 0, "failed": 0}
