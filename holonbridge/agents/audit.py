"""Agent calls in the audit graph.

The audit middleware sees HTTP requests. An agent call is mostly *not* one: a
trigger places it while handling somebody else's write, the scheduler sends it
on a timer, and even when a person approves it over HTTP the request entry says
only "POST /agent-call/{id}/approve" -- not which endpoint received what. These
entries are about the call itself, written by the bridge to the same
``urn:{dataset}:audit`` graph (so the same admin-only reader, ``GET /audit``,
shows them) with the call, the endpoint, the digest and size of the body, who
asked and who authorised it:

``placed``
    A call was recorded (by a person, a trigger or a pipeline stage).
``approved`` / ``rejected``
    A person decided. (Policy approval is stated on ``placed``.)
``send``
    About to go out. **Written before any byte leaves, and the send does not
    happen if it cannot be written** -- see :func:`record`.
``result``
    How it ended: Responded or Failed, with the agent's HTTP status.
``dispatch refused``
    The scheduler declined to send a queued call (its requester lost the
    access, or the endpoint is no longer usable).

Bodies are never copied, only hashed and sized, as everywhere in the audit log.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .. import audit
from ..conn import Conn
from .model import Call, Endpoint

log = logging.getLogger("holonbridge.agents")

METHOD = "AGENT"


def _host(endpoint: Endpoint | None) -> str:
    if endpoint is None or not endpoint.url:
        return ""
    parts = urlsplit(endpoint.url)
    return parts.netloc.rsplit("@", 1)[-1]


def _entry(
    event: str,
    outcome: str,
    status: int,
    call: Call,
    *,
    actor: str | None,
    reason: str | None,
    authorised_reason: str | None = None,
) -> dict:
    body_bytes = len(call.body.encode("utf-8")) if call.body else None
    return {
        "at": datetime.now(timezone.utc),
        "outcome": outcome,
        "operation": f"AGENT-CALL {event}",
        "method": METHOD,
        "path": f"/agent-call/{call.id}",
        "status": status,
        "actor": actor or None,
        "graphs": [call.target_graph] if call.target_graph else [],
        "authorised_by": call.approved_by or None,
        "authorised_reason": authorised_reason
        or (f"{call.approval_mode} approval" if call.approval_mode else None),
        "payload_hash": call.digest or None,
        "payload_bytes": body_bytes,
        "reason": reason,
        "agent_call": call.iri,
        "agent_endpoint": call.endpoint_id,
    }


async def record(
    client,
    conn: Conn,
    event: str,
    outcome: str,
    call: Call,
    *,
    actor: str | None,
    status: int = 200,
    reason: str | None = None,
    endpoint: Endpoint | None = None,
    authorised_reason: str | None = None,
) -> bool:
    """Write one agent-call entry. ``True`` means "nothing is owed": it was
    written, or the dataset's audit level does not call for it.

    ``False`` means the level calls for an entry and it could not be written.
    Most callers ignore that, as the middleware does (a failed audit write is
    logged, never allowed to fail the operation). The one that does not is the
    ``send`` entry: sending a dataset's content to a third party with no record
    of it is the thing this log exists to prevent, so a send whose entry cannot
    be written does not happen.
    """
    try:
        level = await audit.level_for(client, conn)
    except Exception as exc:  # noqa: BLE001 - unknown level: assume the default, record
        log.warning("audit level unreadable (%s); recording at the default", exc)
        level = audit.DEFAULT_LEVEL
    if not audit.should_record(level, outcome, True, False):
        return True

    host = _host(endpoint)
    text = "; ".join(p for p in (reason, f"to {host}" if host else "") if p) or None
    return await audit.record(
        client,
        conn,
        _entry(event, outcome, status, call, actor=actor, reason=text,
               authorised_reason=authorised_reason),
    )


async def record_trigger_refused(client, conn: Conn, trigger, reason: str) -> bool:
    """A trigger's agent action was stopped because the person who armed it may
    no longer do what it does. There is no call to describe, so this names the
    trigger. Written when the verdict is first reached, not on every evaluation
    that reuses it (the re-check caches verdicts for a short while)."""
    try:
        level = await audit.level_for(client, conn)
    except Exception:  # noqa: BLE001
        level = audit.DEFAULT_LEVEL
    if not audit.should_record(level, "denied", True, False):
        return True
    return await audit.record(
        client,
        conn,
        {
            "at": datetime.now(timezone.utc),
            "outcome": "denied",
            "operation": "AGENT-CALL trigger refused",
            "method": METHOD,
            "path": f"/named-trigger/{trigger.id}",
            "status": 403,
            "actor": trigger.armed_by or None,
            "reason": reason[:300],
            "agent_endpoint": trigger.agent_endpoint,
        },
    )
