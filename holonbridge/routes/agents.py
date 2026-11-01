"""Agent endpoints and agent calls.

Two registries' worth of access, deliberately separate:

* **Endpoints** (``urn:{dataset}:agents``) say where the bridge may send work
  and how it authenticates. Registering, re-activating or deleting one is an
  administrative act: ``holon:grantsWrite`` on the registry graph (and
  ``grantsReplace`` to delete), exactly as for triggers. Registration also runs
  the egress and credential policy (``agents/policy.py``), and refuses anything
  it would later refuse to send.
* **Calls** (``urn:{dataset}:agent-calls``) are operational: placing, approving,
  rejecting, retrying and listing them all need ``holon:grantsWrite`` on the call
  ledger. "May use agents" is therefore one explicit grant on one graph, using
  the grant model that already exists. The ledger holds request bodies, which
  contain graph data, so *reading* it is held to the same grant rather than to
  any identified caller.

Placing a call additionally needs read access to whatever its context query
reads and write access to the graph its answer is meant for (see
``agents.service.authorize_request``).

Not yet Toolset/persona-reachability-gated, like the trigger routes: unreachable
tools are not hidden here. Deferred, not dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .. import audit
from ..agents import (
    CALL_STATUSES,
    Endpoint,
    InvalidEndpoint,
    load_endpoints,
)
from ..agents import service as agent_service
from ..agents.ledger import CallStore
from ..agents.model import (
    APPROVAL_HUMAN,
    AUTH_SCHEMES,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    ENDPOINT_STATUSES,
    HARD_MAX_REQUEST_BYTES,
    HARD_MAX_RESPONSE_BYTES,
    MAX_TIMEOUT_SECONDS,
)
from ..agents.policy import CredentialRefused, check_agent_endpoint, parse_credential_ref
from ..agents.registry import (
    deletion_update,
    endpoint_node,
    registration_update,
    render_endpoint_turtle,
    status_update,
)
from ..agents.sender import HttpAgentSender
from ..cache import RegistryCache
from ..deps import AnimusDep, ClientDep, ConnDep, RegistryDep, SettingsDep
from ..egress import EndpointRefused
from ..fuseki import FusekiError
from ..named_queries import load_named_queries
from ..trigger_authz import TriggerDenied, require_grant

router = APIRouter(tags=["agents"])
KIND = "agents"


class RegisterEndpointRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    url: str = Field(..., min_length=1)
    status: str = Field(default="Active", pattern="^(Active|Suspended|Deprecated)$")
    label: str | None = None
    description: str | None = None
    auth_scheme: str = Field(default="none", description="none | bearer | header")
    auth_header: str | None = Field(default=None, description="header name, for auth_scheme=header")
    credential_ref: str | None = Field(
        default=None, description="env:NAME of the secret; the value is never stored"
    )
    signing_ref: str | None = Field(
        default=None, description="env:NAME of an HMAC key; signs each request body"
    )
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=MAX_TIMEOUT_SECONDS)
    max_response_bytes: int = Field(
        default=DEFAULT_MAX_RESPONSE_BYTES, ge=1024, le=HARD_MAX_RESPONSE_BYTES
    )
    max_request_bytes: int = Field(
        default=DEFAULT_MAX_REQUEST_BYTES, ge=1024, le=HARD_MAX_REQUEST_BYTES
    )
    call_review: bool = Field(
        default=True,
        description="true: a person must approve every call to this endpoint, whatever asked for it",
    )


class EndpointStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(Active|Suspended|Deprecated)$")


class RequestCallRequest(BaseModel):
    endpoint: str = Field(..., min_length=1)
    task: str = Field(..., min_length=1, max_length=20_000)
    focus: str | None = Field(default=None, description="IRI the task is about")
    input: dict | None = Field(default=None, description="extra JSON passed to the agent as-is")
    context_query: str | None = Field(
        default=None, description="id of a named SELECT/CONSTRUCT whose result is sent as context"
    )
    target_graph: str | None = Field(
        default=None,
        description="graph the agent's Turtle is proposed for (staged as a candidate, never written)",
    )


class RejectRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


# --- plumbing -------------------------------------------------------------------


def _query_fn(client, conn):
    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    return query_fn


def _identified(animus) -> str:
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    return animus.person


def _http(exc: TriggerDenied) -> HTTPException:
    if exc.status in (401, 403):
        audit.annotate(reason=exc.reason)
    return HTTPException(exc.status, detail=exc.detail)


async def _require_grant(conn, client, animus, graph: str, *, replace: bool = False) -> None:
    person = _identified(animus)
    try:
        await require_grant(_query_fn(client, conn), conn, person, graph, replace=replace)
    except TriggerDenied as exc:
        raise _http(exc) from exc


def _sender(request: Request, settings):
    # A seam for tests and for deployments that front agents with something
    # other than plain HTTP; the default is the one implementation there is.
    return getattr(request.app.state, "agent_sender", None) or HttpAgentSender(settings=settings)


def _bad(error: str, message: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail={"error": error, "message": message})


def _not_found_endpoint(endpoint_id: str, available: list[str]) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"error": "unknown_agent_endpoint", "id": endpoint_id, "available": available},
    )


def _call_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, agent_service.CallNotFound):
        return HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_agent_call", "id": str(exc)}
        )
    if isinstance(exc, agent_service.CallStateError):
        return HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "call_state", "message": str(exc)}
        )
    if isinstance(exc, agent_service.CallInvalid):
        return _bad("invalid_call", str(exc))
    if isinstance(exc, TriggerDenied):
        return _http(exc)
    if isinstance(exc, FusekiError):
        return HTTPException(exc.status or 502, detail=exc.as_dict())
    raise exc


async def _endpoints(client, conn, cache: RegistryCache, *, refresh: bool = False):
    return await cache.get(client, conn, kind=KIND, loader=load_endpoints, refresh=refresh)


async def _check_endpoint_policy(url: str, credential_ref, signing_ref, settings) -> None:
    """The registration-time half of ``policy.py``: refuse anything a send would."""
    try:
        for ref in (credential_ref, signing_ref):
            if ref:
                parse_credential_ref(ref, settings=settings)
        await check_agent_endpoint(
            url, has_credentials=bool(credential_ref or signing_ref), settings=settings
        )
    except (CredentialRefused, EndpointRefused) as exc:
        raise _bad("endpoint_refused", str(exc)) from exc


# --- endpoints ------------------------------------------------------------------


@router.get("/agent-endpoints")
async def list_agent_endpoints(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
    endpoint_status: str | None = Query(default=None, pattern="^(Active|Suspended|Deprecated)$"),
    refresh: bool = Query(default=False),
) -> dict:
    result = await _endpoints(client, conn, cache, refresh=refresh)
    endpoints = result.endpoints
    if endpoint_status:
        endpoints = [e for e in endpoints if e.status == endpoint_status]
    return {
        "dataset": conn.dataset,
        "graph": conn.graph("agents"),
        "ledger": conn.graph("agent-calls"),
        "count": len(endpoints),
        "statuses": list(ENDPOINT_STATUSES),
        "authSchemes": list(AUTH_SCHEMES),
        "endpoints": [e.summary() for e in endpoints],
        "warnings": result.warnings,
    }


@router.get("/agent-endpoint/{endpoint_id}")
async def get_agent_endpoint(
    endpoint_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep, cache: RegistryDep
) -> dict:
    result = await _endpoints(client, conn, cache)
    endpoint = result.by_id(endpoint_id)
    if endpoint is None:
        raise _not_found_endpoint(endpoint_id, [e.id for e in result.endpoints])
    return endpoint.summary()


@router.post("/agent-endpoint")
async def register_agent_endpoint(
    body: RegisterEndpointRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
    settings: SettingsDep,
) -> dict:
    """Register (or overwrite) an agent endpoint."""
    registry = conn.graph("agents")
    # Access first: a caller who may not register learns nothing about which
    # URLs or credentials the policy would have accepted.
    await _require_grant(conn, client, animus, registry)
    await _check_endpoint_policy(body.url, body.credential_ref, body.signing_ref, settings)

    node = endpoint_node(conn, body.id)
    try:
        turtle = render_endpoint_turtle(
            node=node,
            endpoint_id=body.id,
            url=body.url.strip(),
            status=body.status,
            label=body.label,
            description=body.description,
            auth_scheme=body.auth_scheme,
            auth_header=body.auth_header,
            credential_ref=body.credential_ref,
            signing_ref=body.signing_ref,
            timeout_seconds=body.timeout_seconds,
            max_response_bytes=body.max_response_bytes,
            max_request_bytes=body.max_request_bytes,
            call_review=body.call_review,
            registered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            registered_by=animus.person,
        )
    except InvalidEndpoint as exc:
        raise _bad("invalid_endpoint", str(exc)) from exc

    audit.annotate(graphs=[registry])
    try:
        await client.update(conn, registration_update(registry, node, turtle))
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    cache.invalidate(conn, KIND)
    return {
        "ok": True,
        "id": body.id,
        "iri": node,
        "graph": registry,
        "endpointStatus": body.status,
        "callReview": body.call_review,
    }


@router.post("/agent-endpoint/{endpoint_id}/status")
async def set_agent_endpoint_status(
    endpoint_id: str,
    body: EndpointStatusRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
    settings: SettingsDep,
) -> dict:
    """Suspend, resume or deprecate an endpoint. Resuming re-runs the policy
    check: the allowlist may have tightened since it was registered."""
    registry = conn.graph("agents")
    await _require_grant(conn, client, animus, registry)

    result = await _endpoints(client, conn, cache)
    endpoint = result.by_id(endpoint_id)
    if endpoint is None:
        raise _not_found_endpoint(endpoint_id, [e.id for e in result.endpoints])

    if body.status == "Active":
        await _check_endpoint_policy(
            endpoint.url, endpoint.credential_ref, endpoint.signing_ref, settings
        )

    audit.annotate(graphs=[registry])
    try:
        await client.update(
            conn,
            status_update(
                registry, endpoint.iri, body.status,
                registered_by=animus.person if body.status == "Active" else None,
            ),
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    cache.invalidate(conn, KIND)
    return {"ok": True, "id": endpoint_id, "endpointStatus": body.status}


@router.delete("/agent-endpoint/{endpoint_id}")
async def delete_agent_endpoint(
    endpoint_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep, cache: RegistryDep
) -> dict:
    """Remove an endpoint. Needs replace access to the registry. Calls already
    made to it stay in the ledger: they are history."""
    registry = conn.graph("agents")
    await _require_grant(conn, client, animus, registry, replace=True)

    result = await _endpoints(client, conn, cache)
    endpoint = result.by_id(endpoint_id)
    if endpoint is None:
        raise _not_found_endpoint(endpoint_id, [e.id for e in result.endpoints])

    audit.annotate(graphs=[registry])
    try:
        await client.update(conn, deletion_update(registry, endpoint.iri))
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    cache.invalidate(conn, KIND)
    return {"ok": True, "id": endpoint_id, "graph": registry}


@router.post("/agent-endpoints/reload")
async def reload_agent_endpoints(conn: ConnDep, client: ClientDep, cache: RegistryDep) -> dict:
    cache.invalidate(conn, KIND)
    result = await _endpoints(client, conn, cache, refresh=True)
    return {"ok": True, "dataset": conn.dataset, "count": len(result.endpoints), "warnings": result.warnings}


# --- calls ----------------------------------------------------------------------


async def _endpoint_for(client, conn, cache, endpoint_id: str) -> Endpoint:
    result = await _endpoints(client, conn, cache)
    endpoint = result.by_id(endpoint_id)
    if endpoint is None:
        raise _not_found_endpoint(endpoint_id, [e.id for e in result.endpoints])
    return endpoint


@router.post("/agent-call")
async def request_agent_call(
    body: RequestCallRequest,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
    settings: SettingsDep,
) -> dict:
    """Place a call. Nothing is sent: the call waits as ``PendingApproval`` until
    a person approves it (``POST /agent-call/{id}/approve``)."""
    person = _identified(animus)
    ledger_graph = conn.graph("agent-calls")
    await _require_grant(conn, client, animus, ledger_graph)

    endpoint = await _endpoint_for(client, conn, cache, body.endpoint)

    context_query = None
    if body.context_query:
        queries = await load_named_queries(client, conn)
        context_query = queries.by_id(body.context_query)
        if context_query is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "unknown_context_query",
                    "id": body.context_query,
                    "available": sorted(q.id for q in queries.queries),
                },
            )

    audit.annotate(graphs=[ledger_graph])
    try:
        call = await agent_service.request_call(
            client, conn,
            requested_by=person,
            endpoint=endpoint,
            task=body.task,
            focus=body.focus,
            payload=body.input,
            context_query=context_query,
            target_graph=body.target_graph,
            approval=APPROVAL_HUMAN,
            settings=settings,
        )
    except (agent_service.CallInvalid, TriggerDenied, FusekiError) as exc:
        raise _call_errors(exc) from exc
    return {"ok": True, **call.detail()}


@router.get("/agent-calls")
async def list_agent_calls(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    call_status: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    if call_status and call_status not in CALL_STATUSES:
        raise _bad("invalid_status", f"call_status must be one of {', '.join(CALL_STATUSES)}")
    await _require_grant(conn, client, animus, conn.graph("agent-calls"))
    calls = await CallStore(client).list_calls(conn, status=call_status, endpoint=endpoint, limit=limit)
    return {
        "dataset": conn.dataset,
        "graph": conn.graph("agent-calls"),
        "statuses": list(CALL_STATUSES),
        "count": len(calls),
        "calls": [c.summary() for c in calls],
    }


@router.get("/agent-call/{call_id}")
async def get_agent_call(call_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    await _require_grant(conn, client, animus, conn.graph("agent-calls"))
    call = await CallStore(client).get(conn, call_id)
    if call is None:
        raise _call_errors(agent_service.CallNotFound(call_id))
    return call.detail()


async def _load_call(client, conn, call_id: str):
    call = await CallStore(client).get(conn, call_id)
    if call is None:
        raise _call_errors(agent_service.CallNotFound(call_id))
    return call


@router.post("/agent-call/{call_id}/approve")
async def approve_agent_call(
    call_id: str,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
    settings: SettingsDep,
) -> dict:
    """Approve and send. The request body that was stored when the call was
    placed is exactly what is sent; ``GET /agent-call/{id}`` shows it."""
    person = _identified(animus)
    ledger_graph = conn.graph("agent-calls")
    await _require_grant(conn, client, animus, ledger_graph)
    call = await _load_call(client, conn, call_id)
    endpoint = await _endpoint_for(client, conn, cache, call.endpoint_id)

    audit.annotate(graphs=[ledger_graph])
    try:
        done = await agent_service.approve_call(
            client, conn, call_id, approver=person, endpoint=endpoint,
            sender=_sender(request, settings),
        )
    except (agent_service.CallNotFound, agent_service.CallStateError, FusekiError) as exc:
        raise _call_errors(exc) from exc
    return {"ok": True, **done.summary()}


@router.post("/agent-call/{call_id}/reject")
async def reject_agent_call(
    call_id: str, body: RejectRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    person = _identified(animus)
    ledger_graph = conn.graph("agent-calls")
    await _require_grant(conn, client, animus, ledger_graph)
    audit.annotate(graphs=[ledger_graph])
    try:
        done = await agent_service.reject_call(
            client, conn, call_id, rejecter=person, reason=body.reason
        )
    except (agent_service.CallNotFound, agent_service.CallStateError, FusekiError) as exc:
        raise _call_errors(exc) from exc
    return {"ok": True, **done.summary()}


@router.post("/agent-call/{call_id}/retry")
async def retry_agent_call(
    call_id: str,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
    settings: SettingsDep,
) -> dict:
    """Send a failed call again, under the same id (the idempotency key)."""
    person = _identified(animus)
    ledger_graph = conn.graph("agent-calls")
    await _require_grant(conn, client, animus, ledger_graph)
    call = await _load_call(client, conn, call_id)
    endpoint = await _endpoint_for(client, conn, cache, call.endpoint_id)

    audit.annotate(graphs=[ledger_graph])
    try:
        done = await agent_service.retry_call(
            client, conn, call_id, approver=person, endpoint=endpoint,
            sender=_sender(request, settings),
        )
    except (agent_service.CallNotFound, agent_service.CallStateError, FusekiError) as exc:
        raise _call_errors(exc) from exc
    return {"ok": True, **done.summary()}


@router.post("/agent-calls/dispatch")
async def dispatch_agent_calls(
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
    settings: SettingsDep,
    limit: int = Query(default=10, ge=1, le=100),
) -> dict:
    """Send calls that were approved by policy. The scheduler's ``agent-dispatch``
    job does this on a timer; this is the same thing on demand."""
    ledger_graph = conn.graph("agent-calls")
    await _require_grant(conn, client, animus, ledger_graph)
    result = await _endpoints(client, conn, cache)
    audit.annotate(graphs=[ledger_graph])
    try:
        summary = await agent_service.dispatch_approved(
            client, conn,
            endpoints={e.id: e for e in result.endpoints},
            sender=_sender(request, settings),
            limit=limit,
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, **summary}
