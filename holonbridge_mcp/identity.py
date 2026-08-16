"""Per-request caller identity, threaded from the inbound credential check
(:class:`holonbridge_mcp.remote.BearerGate`) to the outbound calls this
process makes to the REST bridge (:func:`holonbridge_mcp.server._headers`).

A plain module-level ``ContextVar``, not request state hung off any
framework object, because the two ends of this handoff sit on opposite
sides of a boundary that isn't obviously one call stack: the credential is
checked in raw ASGI middleware, but the code that needs it runs inside a
FastMCP tool handler, which the ``mcp`` SDK executes in a task spawned per
incoming message (``Server.run``'s ``tg.start_soon`` in
``mcp.server.lowlevel.server``), not in the task that received the HTTP
request carrying that particular tool call.

This still works, and deliberately relies on a specific, verified property
of that spawn rather than assuming it: both asyncio's and anyio's task
creation copy the *current* contextvars context into the new task. For the
SSE transport, the task group in ``Server.run`` is created once per
session, from the context active when the ``GET /sse`` connection itself
was authorized -- individual tool-invoking messages arrive later over a
separate ``POST /messages/`` request that merely feeds the session's
queue, and do not themselves contribute to that inherited context. So the
identity only needs to be captured once, at that connection, not
re-threaded on every message -- which is also the right granularity
functionally, since a session authenticates once and keeps one identity
for its lifetime.

``None`` means "this credential is valid but carries no per-user
identity" -- the static shared token (``MCP_INBOUND_TOKEN``) is exactly
this case by design (see ``remote.py``'s module docstring: "there is no
way to tell callers apart"). It is a legitimate value, not a missing one.
"""

from __future__ import annotations

import contextvars

current_github_login: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_github_login", default=None
)
