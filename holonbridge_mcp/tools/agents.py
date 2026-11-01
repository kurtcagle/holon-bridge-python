"""Agent gateway tools: register external agents, hand them work, review the result.

Added 2026-11-01. The bridge does not run agents. It keeps a registry of where
tasks may be sent (``hb:AgentEndpoint``), and a ledger of every task it did send
(``hb:AgentCall``). Two people-gates surround each call:

1. **What leaves.** A call waits as ``PendingApproval`` with the exact request
   body stored; ``get_agent_call`` shows it. ``approve_agent_call`` sends that
   body, no other.
2. **What lands.** An agent's Turtle is never written. It is staged in the
   candidate queue (``list_candidates`` / ``approve_candidate``) like every other
   proposal, through the ordinary ACL- and SHACL-gated write.

Credentials are never passed through these tools: an endpoint names an
environment variable (``env:HB_AGENT_...``) that the bridge's operator sets.

Tool names are prefixed ``agent`` to keep them apart from the bank endpoint
tools (``list_endpoints``, ``get_endpoint``, ``set_endpoint``), which are
unrelated.
"""

from __future__ import annotations

from typing import Any

from ..session import mcp, _call


@mcp.tool()
async def list_agent_endpoints(endpoint_status: str | None = None, refresh: bool = False) -> dict:
    """List registered agent endpoints (``Active``, ``Suspended`` or ``Deprecated``).

    Each entry shows where the bridge will send, how it authenticates (by
    *reference* to an environment variable -- never the secret), its limits, and
    whether every call needs a person's approval (``callReview``).
    """
    params: dict[str, Any] = {}
    if endpoint_status:
        params["endpoint_status"] = endpoint_status
    if refresh:
        params["refresh"] = refresh
    return await _call("GET", "/agent-endpoints", params=params or None)


@mcp.tool()
async def get_agent_endpoint(endpoint_id: str) -> dict:
    """One agent endpoint's definition."""
    return await _call("GET", f"/agent-endpoint/{endpoint_id}")


@mcp.tool()
async def register_agent_endpoint(
    id: str,
    url: str,
    auth_scheme: str = "none",
    auth_header: str | None = None,
    credential_ref: str | None = None,
    signing_ref: str | None = None,
    timeout_seconds: float = 30.0,
    max_response_bytes: int = 1_000_000,
    max_request_bytes: int = 1_000_000,
    call_review: bool = True,
    status: str = "Active",
    label: str | None = None,
    description: str | None = None,
) -> dict:
    """Register (or overwrite) an agent endpoint. Needs write access to the
    agent registry.

    ``auth_scheme`` is ``none``, ``bearer`` (``Authorization: Bearer ...``) or
    ``header`` (the secret goes in the header named by ``auth_header``).
    ``credential_ref`` is ``env:NAME``: the *name* of an environment variable set
    by the bridge's operator, which must start with the operator's prefix
    (default ``HB_AGENT_``). ``signing_ref`` likewise names an HMAC key; each
    request body is then signed (``X-HolonBridge-Signature``).

    The URL must pass the egress policy: https to a public address, or a host
    the operator listed in ``AGENT_ALLOWLIST``. An endpoint that carries a
    credential must be on that allowlist: credentials only travel to hosts the
    operator named.

    ``call_review`` (default true) means a person approves every call, whatever
    asked for it. Set it false only for an endpoint whose calls may be sent by
    policy, with nobody looking first.
    """
    return await _call(
        "POST",
        "/agent-endpoint",
        json_body={
            "id": id,
            "url": url,
            "auth_scheme": auth_scheme,
            "auth_header": auth_header,
            "credential_ref": credential_ref,
            "signing_ref": signing_ref,
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
            "max_request_bytes": max_request_bytes,
            "call_review": call_review,
            "status": status,
            "label": label,
            "description": description,
        },
    )


@mcp.tool()
async def set_agent_endpoint_status(endpoint_id: str, status: str) -> dict:
    """Set an endpoint ``Active``, ``Suspended`` or ``Deprecated``. Making it
    Active re-runs the egress and credential checks."""
    return await _call(
        "POST", f"/agent-endpoint/{endpoint_id}/status", json_body={"status": status}
    )


@mcp.tool()
async def delete_agent_endpoint(endpoint_id: str) -> dict:
    """Remove an endpoint. Needs replace access to the agent registry. Calls
    already made to it stay in the ledger."""
    return await _call("DELETE", f"/agent-endpoint/{endpoint_id}")


@mcp.tool()
async def reload_agent_endpoints() -> dict:
    """Re-read the agent registry, discarding the cached copy."""
    return await _call("POST", "/agent-endpoints/reload")


@mcp.tool()
async def request_agent_call(
    endpoint: str,
    task: str,
    focus: str | None = None,
    input: dict | None = None,
    context_query: str | None = None,
    target_graph: str | None = None,
) -> dict:
    """Place a call to an agent. **Nothing is sent**: the call waits for approval.

    ``task`` is what the agent is asked to do. ``context_query`` is the id of a
    registered named SELECT or CONSTRUCT whose result is run now and included in
    the request (so it is visible to whoever approves); you must be able to read
    everything it reads. If it declares a ``focus`` parameter (datatype
    ``anyURI``) it is bound to ``focus``. ``input`` is extra JSON passed as-is.

    ``target_graph`` is where the agent's Turtle answer is *proposed* for. It is
    staged in the candidate queue, never written, and you need write access to
    that graph to ask.

    Needs write access to the call ledger (``urn:{dataset}:agent-calls``): that
    grant is what "may use agents" means. Returns the call, including the exact
    request body, with ``callStatus: PendingApproval``.
    """
    return await _call(
        "POST",
        "/agent-call",
        json_body={
            "endpoint": endpoint,
            "task": task,
            "focus": focus,
            "input": input,
            "context_query": context_query,
            "target_graph": target_graph,
        },
    )


@mcp.tool()
async def list_agent_calls(
    call_status: str | None = None, endpoint: str | None = None, limit: int = 50
) -> dict:
    """List calls, newest first. ``call_status`` is one of ``PendingApproval``,
    ``Approved``, ``Sending``, ``Responded``, ``Failed``, ``Rejected``. Needs
    write access to the call ledger, which holds request bodies."""
    params: dict[str, Any] = {"limit": limit}
    if call_status:
        params["call_status"] = call_status
    if endpoint:
        params["endpoint"] = endpoint
    return await _call("GET", "/agent-calls", params=params)


@mcp.tool()
async def get_agent_call(call_id: str) -> dict:
    """One call in full, including ``request``: the exact JSON that is (or was)
    sent. Read this before approving."""
    return await _call("GET", f"/agent-call/{call_id}")


@mcp.tool()
async def approve_agent_call(call_id: str) -> dict:
    """Approve a ``PendingApproval`` call and send it now. Sends the stored
    request body exactly. If the agent answered with Turtle, it is staged as a
    candidate (see ``list_candidates``); this does not write it."""
    return await _call("POST", f"/agent-call/{call_id}/approve")


@mcp.tool()
async def reject_agent_call(call_id: str, reason: str = "") -> dict:
    """Reject a call that has not been sent. Nothing leaves the bridge."""
    return await _call("POST", f"/agent-call/{call_id}/reject", json_body={"reason": reason})


@mcp.tool()
async def retry_agent_call(call_id: str) -> dict:
    """Send a ``Failed`` call again (or a ``Sending`` one presumed lost). The
    call id is sent as the idempotency key, so an agent that deduplicates will
    recognise the task."""
    return await _call("POST", f"/agent-call/{call_id}/retry")


@mcp.tool()
async def dispatch_agent_calls(limit: int = 10) -> dict:
    """Send calls that a policy approved (a trigger registered with
    ``review_required=false`` against an endpoint with ``call_review=false``).
    The scheduler's ``agent-dispatch`` job does this on a timer; this does it
    now."""
    return await _call("POST", "/agent-calls/dispatch", params={"limit": limit})
