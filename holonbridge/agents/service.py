"""Placing, approving, sending and settling agent calls.

No HTTP framework in here: the routes, the trigger evaluator, the pipeline
runner and the scheduler's dispatch job all call these functions, so a call
behaves identically however it came to exist.

The lifecycle, and where each gate sits:

``request_call``
    Checks the *requester* can do what the call will do -- place calls at all
    (write on the call ledger), read whatever the context query reads, and
    write the target graph the response is meant for. The context is run now and
    stored inside the request body, so the person approving later reviews the
    exact bytes that will be sent, not a description of them.

``approve_call`` / ``reject_call``
    The outbound gate. Approving a call that a person asked for, or that a
    trigger or pipeline asked for on an endpoint with ``callReview``, is a
    person's decision. A call a *policy* approved (``reviewRequired: false`` on
    the trigger, ``callReview: false`` on the endpoint) is queued as ``Approved``
    and sent by :func:`dispatch_approved`, outside any request.

``send_call``
    Moves ``Approved`` to ``Sending`` under the dataset write lock, *then* sends.
    That ordering is the at-most-once guarantee: whoever loses the race for the
    transition does not send. The HTTP call itself runs with the lock released.

settlement
    A reply that is not a well-formed success is recorded as ``Failed`` with a
    reason. A success whose Turtle is meant for a target graph is staged as a
    ``holon:CandidateStatus`` -- the same review queue triggers and pipelines
    use -- never written to the target directly. So there are two human gates
    around an agent: one on what leaves, one on what lands.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .. import write_lock
from ..config import Settings, get_settings
from ..conn import Conn
from ..fuseki import FusekiClient, FusekiError
from ..named_queries import NamedQuery, apply_query_params
from ..params import ParameterError
from ..sparql_kind import form
from ..trigger_authz import TriggerDenied, authorize_reads, require_grant
from ..turtle import TurtleSyntaxError, looks_like_rdf12, parse
from . import audit as agent_audit
from . import ledger
from .ledger import CallStore
from .model import (
    APPROVAL_HUMAN,
    APPROVAL_POLICY,
    APPROVED,
    FAILED,
    MAX_CONTEXT_ROWS,
    PENDING,
    REJECTED,
    REQUEST_SCHEMA,
    RESPONDED,
    SENDING,
    STALE_SENDING_GRACE_SECONDS,
    Call,
    Endpoint,
)
from .policy import check_agent_endpoint
from .sender import AgentSender, HttpAgentSender, Reply, SendFailed

log = logging.getLogger("holonbridge.agents")

#: Why a call was not sent when its audit entry could not be written.
AUDIT_UNAVAILABLE = "the audit log could not be written, so the call was not sent"

QueryFn = Callable[[str], Awaitable[dict]]

#: How much of an agent's reply is kept in the ledger when it is an error.
ERROR_EXCERPT = 300
SUMMARY_LIMIT = 1000


class CallNotFound(LookupError):
    """No such call."""


class CallStateError(RuntimeError):
    """The call is not in a state that allows this. The message says which."""


class CallInvalid(ValueError):
    """The request cannot become a call as given. Safe to show the caller."""


class QueueFull(CallInvalid):
    """Too many calls to one endpoint are already open. A kind of
    :class:`CallInvalid` so every caller already handles it, but nothing is wrong
    with the request itself: a route says 429, not 400."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_call_id() -> str:
    return uuid.uuid4().hex


# --- authorisation -------------------------------------------------------------------


async def authorize_request(
    query_fn: QueryFn,
    conn: Conn,
    person: str | None,
    *,
    context_sparql: str | None = None,
    context_id: str = "",
    target_graph: str | None = None,
) -> None:
    """Everything the requester must be able to do for a call to be theirs.

    The same questions are asked when a call is placed, when a trigger is
    registered, and again when a trigger fires (of the person who armed it).
    ``TriggerDenied`` carries the status and detail either way.
    """
    if person is None:
        raise TriggerDenied(401, "unresolved identity")
    await require_grant(query_fn, conn, person, conn.graph("agent-calls"))
    if context_sparql:
        await authorize_reads(
            query_fn, conn, person, label="context", ident=context_id, sparql=context_sparql
        )
    if target_graph:
        # The response is staged for review, and approving it writes this graph.
        await require_grant(query_fn, conn, person, conn.graph("candidates"))
        await require_grant(query_fn, conn, person, target_graph)


# --- building the request ------------------------------------------------------------


def bind_context(query: NamedQuery, focus: str | None) -> str:
    """The context query's SPARQL, with ``focus`` bound if it declares one.

    ``focus`` is only supplied to a query that *declares* a ``focus`` parameter
    (give it datatype ``anyURI`` so it renders as an IRI): an undeclared
    placeholder would render as a plain string, which is silently the wrong
    thing.
    """
    supplied: dict[str, Any] = {}
    if focus and "focus" in query.declared:
        supplied["focus"] = focus
    try:
        bound = apply_query_params(query, supplied)
    except ParameterError as exc:
        raise CallInvalid(f"context query {query.id!r}: {exc}") from exc
    if bound.missing:
        raise CallInvalid(
            f"context query {query.id!r} needs parameter(s) {', '.join(bound.missing)}; "
            "only 'focus' is supplied, and only if the query declares it (as anyURI)"
        )
    if form(bound.sparql) not in {"SELECT", "CONSTRUCT", "DESCRIBE"}:
        raise CallInvalid(
            f"context query {query.id!r} is a {form(bound.sparql)}; "
            "a context query must SELECT or CONSTRUCT"
        )
    return bound.sparql


async def run_context(client: FusekiClient, conn: Conn, sparql: str) -> dict[str, Any]:
    """Run the context query. CONSTRUCT/DESCRIBE come back as Turtle, SELECT as
    rows (bounded: a SELECT with no LIMIT must not ship the whole graph)."""
    if form(sparql) == "SELECT":
        bindings = (await client.select(conn, sparql)).get("results", {}).get("bindings", [])
        rows = [{k: v.get("value") for k, v in row.items()} for row in bindings[:MAX_CONTEXT_ROWS]]
        return {
            "format": "json-rows",
            "rows": rows,
            "truncated": len(bindings) > MAX_CONTEXT_ROWS,
        }
    return {"format": "text/turtle", "content": await client.construct(conn, sparql)}


def build_body(
    *,
    call_id: str,
    endpoint: Endpoint,
    conn: Conn,
    task: str,
    focus: str | None,
    context: dict[str, Any] | None,
    payload: Mapping[str, Any] | None,
    target_graph: str | None,
    requested_at: str,
) -> str:
    """The JSON text that will be sent. Deterministic, so its digest identifies it."""
    body = {
        "schema": REQUEST_SCHEMA,
        "callId": call_id,
        "idempotencyKey": call_id,
        "endpoint": endpoint.id,
        "dataset": conn.dataset,
        "task": task,
        "focus": focus,
        "context": context,
        "input": dict(payload) if payload else None,
        "respond": {
            "format": "application/json",
            "fields": {
                "status": "'ok' on success; anything else is treated as a failure",
                "summary": "one line describing what you did or why you could not",
                "turtle": "optional: Turtle to propose for the target graph"
                if target_graph
                else "not requested",
            },
            "targetGraph": target_graph,
        },
        "requestedAt": requested_at,
    }
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


async def request_call(
    client: FusekiClient,
    conn: Conn,
    *,
    requested_by: str,
    endpoint: Endpoint,
    task: str,
    focus: str | None = None,
    payload: Mapping[str, Any] | None = None,
    context_query: NamedQuery | None = None,
    target_graph: str | None = None,
    approval: str = APPROVAL_HUMAN,
    via_trigger: str = "",
    via_pipeline: str = "",
    via_stage: str = "",
    store: CallStore | None = None,
    settings: Settings | None = None,
) -> Call:
    """Authorise, assemble and record a call. Nothing is sent.

    ``approval=policy`` asks for the call to be queued without a person's
    approval; it is honoured only when the endpoint does not require review,
    otherwise the call waits as ``PendingApproval`` like any other.
    """
    if not endpoint.active:
        raise CallInvalid(f"endpoint {endpoint.id!r} is {endpoint.status.lower()}")
    if not (task or "").strip():
        raise CallInvalid("a call needs a task")
    try:
        await check_agent_endpoint(
            endpoint.url, has_credentials=endpoint.has_credentials, settings=settings
        )
    except Exception as exc:  # EndpointRefused -- reported as a property of the request
        raise CallInvalid(f"endpoint {endpoint.id!r} cannot be used: {exc}") from exc

    cap = (settings or get_settings()).agent_max_open_calls
    if cap:
        open_now = await (store or CallStore(client)).open_count(conn, endpoint.id)
        if open_now >= cap:
            raise QueueFull(
                f"{open_now} calls to endpoint {endpoint.id!r} are already open "
                f"(AGENT_MAX_OPEN_CALLS={cap}); approve, reject or dispatch some first"
            )

    context_sparql = bind_context(context_query, focus) if context_query else None

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    await authorize_request(
        query_fn, conn, requested_by,
        context_sparql=context_sparql,
        context_id=context_query.id if context_query else "",
        target_graph=target_graph,
    )

    context = await run_context(client, conn, context_sparql) if context_sparql else None

    call_id = new_call_id()
    requested_at = now()
    body = build_body(
        call_id=call_id, endpoint=endpoint, conn=conn, task=task, focus=focus,
        context=context, payload=payload, target_graph=target_graph, requested_at=requested_at,
    )
    size = len(body.encode("utf-8"))
    if size > endpoint.max_request_bytes:
        raise CallInvalid(
            f"the request is {size} bytes, over endpoint {endpoint.id!r}'s limit of "
            f"{endpoint.max_request_bytes}; narrow the context query"
        )

    queued = approval == APPROVAL_POLICY and not endpoint.call_review
    call = Call(
        id=call_id,
        iri=ledger.call_node(conn, call_id),
        endpoint_id=endpoint.id,
        status=APPROVED if queued else PENDING,
        requested_by=requested_by,
        requested_at=requested_at,
        task=task,
        body=body,
        digest="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        target_graph=target_graph or "",
        focus=focus or "",
        via_trigger=via_trigger,
        via_pipeline=via_pipeline,
        via_stage=via_stage,
        approval_mode=APPROVAL_POLICY if queued else "",
        approved_by=requested_by if queued else "",
        approved_at=requested_at if queued else "",
    )
    await (store or CallStore(client)).put(conn, call)
    via = (
        f"trigger {via_trigger}" if via_trigger
        else f"pipeline {via_pipeline}/{via_stage}" if via_pipeline
        else "request"
    )
    await agent_audit.record(
        client, conn, "placed", "allowed", call,
        actor=requested_by, endpoint=endpoint,
        reason=f"{via}; {'queued by policy' if queued else 'awaiting approval'}",
        authorised_reason="policy approval" if queued else None,
    )
    return call


# --- transitions ---------------------------------------------------------------------


async def _transition(
    client: FusekiClient,
    conn: Conn,
    call_id: str,
    *,
    allowed: tuple[str, ...],
    changes: Callable[[Call], dict[str, Any]],
    action: str,
    endpoint: Endpoint | None = None,
    store: CallStore | None = None,
) -> Call:
    """Move a call between states, atomically with respect to other movers.

    The check and the write happen under the dataset write lock, so two callers
    racing for the same transition cannot both win it. A ``Sending`` call that
    has outlived its endpoint's timeout by the grace period is treated as lost
    and may be retried.
    """
    store = store or CallStore(client)
    async with write_lock.for_conn(conn):
        call = await store.get(conn, call_id)
        if call is None:
            raise CallNotFound(call_id)
        if call.status not in allowed and not _stale_sending(call, endpoint, allowed):
            raise CallStateError(f"cannot {action} a call that is {call.status}")
        await store.replace(conn, call, **changes(call))
    return call


def _stale_sending(call: Call, endpoint: Endpoint | None, allowed: tuple[str, ...]) -> bool:
    if call.status != SENDING or FAILED not in allowed or not call.started_at:
        return False
    try:
        started = datetime.fromisoformat(call.started_at)
    except ValueError:
        return False
    if started.tzinfo is None:
        return False
    timeout = endpoint.timeout_seconds if endpoint else 0.0
    return datetime.now(timezone.utc) - started > timedelta(seconds=timeout + STALE_SENDING_GRACE_SECONDS)


async def approve_call(
    client: FusekiClient,
    conn: Conn,
    call_id: str,
    *,
    approver: str,
    endpoint: Endpoint,
    sender: AgentSender | None = None,
    send_now: bool = True,
    store: CallStore | None = None,
) -> Call:
    """A person approves a pending call, and it is sent unless ``send_now`` is off."""
    call = await _transition(
        client, conn, call_id,
        allowed=(PENDING,),
        action="approve",
        changes=lambda c: {
            "status": APPROVED, "approved_by": approver, "approved_at": now(),
            "approval_mode": APPROVAL_HUMAN,
        },
        store=store,
    )
    await agent_audit.record(
        client, conn, "approved", "allowed", call, actor=approver, endpoint=endpoint,
        reason=f"requested by {call.requested_by}",
    )
    if not send_now:
        return call
    return await send_call(
        client, conn, call_id, endpoint=endpoint, sender=sender, store=store, source="approval"
    )


async def reject_call(
    client: FusekiClient,
    conn: Conn,
    call_id: str,
    *,
    rejecter: str,
    reason: str = "",
    store: CallStore | None = None,
) -> Call:
    call = await _transition(
        client, conn, call_id,
        allowed=(PENDING, APPROVED),
        action="reject",
        changes=lambda c: {
            "status": REJECTED, "rejected_reason": reason[:ERROR_EXCERPT],
            "rejected_by": rejecter, "completed_at": now(),
        },
        store=store,
    )
    await agent_audit.record(
        client, conn, "rejected", "allowed", call, actor=rejecter,
        reason=reason[:200] or None,
    )
    return call


async def retry_call(
    client: FusekiClient,
    conn: Conn,
    call_id: str,
    *,
    approver: str,
    endpoint: Endpoint,
    sender: AgentSender | None = None,
    store: CallStore | None = None,
) -> Call:
    """Send a failed (or presumed-lost) call again. The id is the idempotency
    key, so an agent that deduplicates sees the same task."""
    await _transition(
        client, conn, call_id,
        allowed=(FAILED,),
        action="retry",
        endpoint=endpoint,
        changes=lambda c: {
            "status": APPROVED, "approved_by": approver, "approved_at": now(),
            "approval_mode": APPROVAL_HUMAN, "error": "", "completed_at": "",
        },
        store=store,
    )
    return await send_call(
        client, conn, call_id, endpoint=endpoint, sender=sender, store=store, source="retry"
    )


async def send_call(
    client: FusekiClient,
    conn: Conn,
    call_id: str,
    *,
    endpoint: Endpoint,
    sender: AgentSender | None = None,
    store: CallStore | None = None,
    source: str = "approval",
) -> Call:
    """Claim an ``Approved`` call, send it, and settle the outcome.

    The ``send`` audit entry is written after the claim and before the send. If
    the audit level calls for it and it cannot be written, the call is failed
    unsent (it can be retried once the log is back): see ``agents/audit.py``.
    """
    store = store or CallStore(client)
    sender = sender or HttpAgentSender()

    claimed = await _transition(
        client, conn, call_id,
        allowed=(APPROVED,),
        action="send",
        changes=lambda c: {"status": SENDING, "started_at": now(), "attempts": c.attempts + 1},
        store=store,
    )
    # ``replace`` updates the call it is handed, so this already reads Sending.
    call = claimed

    sender_of_record = call.approved_by or call.requested_by
    if not await agent_audit.record(
        client, conn, "send", "allowed", call, actor=sender_of_record, endpoint=endpoint,
        reason=f"{source}; requested by {call.requested_by}",
    ):
        return await _fail(store, conn, call, AUDIT_UNAVAILABLE)

    try:
        reply = await sender.send(endpoint, call, call.body)
    except SendFailed as exc:
        done = await _fail(store, conn, call, str(exc))
    except Exception as exc:  # noqa: BLE001 - a sender bug must not strand the call in Sending
        log.exception("agent call %s: sender raised", call.id)
        done = await _fail(store, conn, call, f"sender error: {type(exc).__name__}")
    else:
        done = await _settle(client, conn, store, call, endpoint, reply)

    ok = done.status == RESPONDED
    await agent_audit.record(
        client, conn, "result", "allowed" if ok else "error", done,
        actor=sender_of_record, endpoint=endpoint, status=done.http_status or (200 if ok else 502),
        reason=(done.response_summary if ok else done.error)[:200] or None,
    )
    return done


async def _fail(
    store: CallStore, conn: Conn, call: Call, error: str, *, reply: Reply | None = None
) -> Call:
    changes: dict[str, Any] = {"status": FAILED, "error": error[:ERROR_EXCERPT], "completed_at": now()}
    if reply is not None:
        changes["http_status"] = reply.status_code
        changes["response_bytes"] = reply.size
    await store.replace(conn, call, **changes)
    log.warning("agent call %s failed: %s", call.id, error[:ERROR_EXCERPT])
    return call


async def _settle(
    client: FusekiClient,
    conn: Conn,
    store: CallStore,
    call: Call,
    endpoint: Endpoint,
    reply: Reply,
) -> Call:
    if reply.truncated:
        return await _fail(
            store, conn, call,
            f"response exceeded the endpoint's {endpoint.max_response_bytes}-byte limit", reply=reply,
        )
    if 300 <= reply.status_code < 400:
        return await _fail(
            store, conn, call,
            f"agent answered {reply.status_code} (redirect); redirects are not followed", reply=reply,
        )
    if reply.status_code != 200:
        return await _fail(
            store, conn, call,
            f"agent returned {reply.status_code}: {reply.text.strip()[:ERROR_EXCERPT]}", reply=reply,
        )

    try:
        document = json.loads(reply.text)
    except ValueError:
        return await _fail(
            store, conn, call,
            f"agent reply was not JSON: {reply.text.strip()[:ERROR_EXCERPT]}", reply=reply,
        )
    if not isinstance(document, dict):
        return await _fail(store, conn, call, "agent reply was JSON but not an object", reply=reply)

    summary = str(document.get("summary") or "")[:SUMMARY_LIMIT]
    if document.get("status") != "ok":
        return await _fail(
            store, conn, call,
            f"agent reported {document.get('status')!r}: {summary or '(no summary)'}", reply=reply,
        )

    turtle = document.get("turtle")
    candidate = ""
    if isinstance(turtle, str) and turtle.strip() and call.target_graph:
        try:
            parse(turtle)
        except TurtleSyntaxError as exc:
            # Jena can read RDF 1.2 that rdflib cannot; approval merges through
            # the normal write path, which handles it. Anything else is junk.
            if not looks_like_rdf12(turtle):
                return await _fail(
                    store, conn, call, f"agent's Turtle did not parse: {str(exc)[:200]}", reply=reply
                )
        try:
            from ..triggers import propose_agent_candidate  # noqa: PLC0415 - import cycle

            candidate = await propose_agent_candidate(
                client, conn,
                call_iri=call.iri, agent_id=endpoint.id, turtle=turtle, target_graph=call.target_graph,
            )
        except FusekiError as exc:
            return await _fail(
                store, conn, call, f"could not stage the agent's proposal: {exc}", reply=reply
            )

    await store.replace(
        conn, call,
        status=RESPONDED, completed_at=now(), http_status=reply.status_code,
        response_bytes=reply.size, response_summary=summary, error="", candidate=candidate,
    )
    return call


# --- dispatch ------------------------------------------------------------------------


async def dispatch_approved(
    client: FusekiClient,
    conn: Conn,
    *,
    endpoints: Mapping[str, Endpoint],
    sender: AgentSender | None = None,
    limit: int = 10,
    store: CallStore | None = None,
    source: str = "dispatch",
) -> dict[str, Any]:
    """Send calls that were approved by policy. Run by the scheduler, never
    inside a request.

    The requester's access is checked again before each send: a call can sit
    queued long enough for a grant to be revoked, and "the trigger's definer
    may no longer do this" has to stop the call, not just the next firing.
    """
    store = store or CallStore(client)
    queued = sorted(
        await store.list_calls(conn, status=APPROVED, limit=10_000), key=lambda c: c.requested_at
    )[: max(limit, 0)]

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    sent = responded = failed = 0
    for call in queued:
        endpoint = endpoints.get(call.endpoint_id)
        if endpoint is None or not endpoint.active:
            await _claim_and_fail(
                client, conn, store, call,
                f"endpoint {call.endpoint_id!r} is "
                f"{'unknown' if endpoint is None else endpoint.status.lower()}",
                source=source,
            )
            failed += 1
            continue
        try:
            await authorize_request(
                query_fn, conn, call.requested_by, target_graph=call.target_graph or None
            )
        except TriggerDenied as exc:
            await _claim_and_fail(
                client, conn, store, call,
                f"requester <{call.requested_by}> may no longer place this call: {exc.reason}",
                source=source,
            )
            failed += 1
            continue
        except (FusekiError, KeyError):
            continue  # could not find out; leave it queued for the next run

        try:
            done = await send_call(
                client, conn, call.id, endpoint=endpoint, sender=sender, store=store, source=source
            )
        except CallStateError:
            continue  # someone else took it
        if done.error == AUDIT_UNAVAILABLE:
            failed += 1  # claimed but never sent: not an outbound call, so not counted as one
            continue
        sent += 1
        if done.status == RESPONDED:
            responded += 1
        else:
            failed += 1
    return {"queued": len(queued), "sent": sent, "responded": responded, "failed": failed}


async def _claim_and_fail(
    client: FusekiClient, conn: Conn, store: CallStore, call: Call, reason: str,
    *, source: str = "dispatch",
) -> None:
    """Fail a queued call without sending it. Goes through the same claim so it
    cannot race a concurrent sender."""
    try:
        await _transition(
            client, conn, call.id,
            allowed=(APPROVED,), action="fail",
            changes=lambda c: {"status": FAILED, "error": reason[:ERROR_EXCERPT], "completed_at": now()},
            store=store,
        )
    except CallStateError:
        return
    log.warning("agent call %s not sent: %s", call.id, reason)
    await agent_audit.record(
        client, conn, "dispatch refused", "denied", call, actor=call.requested_by,
        status=403, reason=f"{source}: {reason}"[:300],
    )
