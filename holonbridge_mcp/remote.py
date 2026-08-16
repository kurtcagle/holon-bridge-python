"""Remote transport for holonbridge-mcp.

The stdio server is a child process Claude Desktop launches; nothing else can
reach it, so it needs no authentication of its own. Over a tunnel that stops
being true. The server holds the bridge's bearer token, so anyone who finds
the URL gets the whole tool surface — including `sparql_update` and
`push_turtle` — without ever seeing that token.

So the remote transport authenticates its own inbound requests, via one or
both of two credential kinds:

- **A static shared token** (``MCP_INBOUND_TOKEN``). Anyone holding it has
  full access; there is no way to tell callers apart. Fine for one operator
  testing against their own tunnel, which is exactly the case it exists for.
- **A GitHub-identified session token**, issued by the OAuth layer in
  :mod:`holonbridge_mcp.github_oauth` once ``GITHUB_OAUTH_CLIENT_ID`` is set.
  This is what makes a *caller* — a `sub` claim, an allowlisted GitHub login
  — visible in provenance, rather than every request being indistinguishable.

Both can be active at once. Nothing here requires choosing.

CHANGED 2026-08-15: ``BearerGate`` now captures *which* identity a
credential establishes, not just whether it is valid, and threads it
through :mod:`holonbridge_mcp.identity` so the outbound calls this process
makes to the REST bridge can present it. Previously the verified GitHub
login was checked and immediately discarded — correct for gating this
transport, useless for anything downstream that needed to know who was
asking. See ``identity.py`` for why a ContextVar is the right mechanism
here and what it does and doesn't guarantee about session lifetime.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Awaitable, Callable

from starlette.routing import Route

from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount

from .identity import current_github_login

log = logging.getLogger("holonbridge_mcp.remote")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

#: The one path with a canned response of its own — it isn't a route on the
#: inner app at all, so this is the only path that short-circuits.
HEALTH_PATH = "/healthz"

#: Paths exempt from the credential check but still forwarded to the inner
#: app to get their real response. When GitHub OAuth is configured, this is
#: its own endpoints: an OAuth authorization server that requires a bearer
#: token to reach `/authorize` cannot issue its first one — the exact bug
#: ("well-known routes returning 401 because they were placed after the auth
#: middleware") that shape gets into. Conflating "skip auth" with "return the
#: canned health stub" here was a real bug during development: it made every
#: OAuth endpoint return `{"ok":true}` instead of running its handler.
BASE_OPEN_PATHS: tuple[str, ...] = ()


class _RejectedType:
    """Sentinel: a checker does not accept the presented credential.

    Distinct from ``None``, which a checker returns for a credential that
    *is* valid but establishes no per-user identity (the static token) —
    conflating the two would make "invalid" and "valid but anonymous"
    indistinguishable to ``_authorised``.
    """

    def __repr__(self) -> str:  # pragma: no cover
        return "REJECTED"


REJECTED = _RejectedType()

#: A checker takes the raw presented credential (already stripped of the
#: "Bearer " prefix) and returns REJECTED if it doesn't recognise it,
#: otherwise the identity that credential establishes — a GitHub login for
#: the OAuth checker, or None for the static token, which by design cannot
#: tell callers apart. Kept this narrow so a new credential kind is one
#: function, not a BearerGate subclass.
Checker = Callable[[str], "str | None | _RejectedType"]


class BearerGate:
    """Raw ASGI middleware enforcing bearer authentication.

    Deliberately ASGI rather than Starlette's ``BaseHTTPMiddleware``: that one
    buffers the response, which breaks SSE. A middleware that silently turns a
    stream into a single blob is a bad way to discover the difference.
    """

    def __init__(
        self, app: Callable, checkers: list[Checker], *, open_paths: tuple[str, ...]
    ) -> None:
        if not checkers:
            raise ValueError("BearerGate needs at least one credential checker")
        self._app = app
        self._checkers = checkers
        self._open_paths = open_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if scope.get("path") == HEALTH_PATH:
            await self._respond(send, 200, b'{"ok":true}')
            return

        if scope.get("path") in self._open_paths:
            # Auth-exempt, but still a real route on the inner app — forward
            # it rather than answering on its behalf.
            await self._app(scope, receive, send)
            return

        result = self._authorised(scope)
        if result is REJECTED:
            await self._respond(
                send,
                401,
                b'{"error":"unauthorized"}',
                extra=[(b"www-authenticate", b"Bearer")],
            )
            return

        # result is now str | None: the identity this credential established,
        # or None for a valid-but-anonymous one (the static token). Set
        # before calling onward so it's present in the context that any
        # spawned task inherits — see identity.py for why that matters and
        # why setting it here, even on requests that never spawn a task that
        # reads it (an ordinary POST /messages/), is harmless rather than
        # something that needs special-casing.
        token = current_github_login.set(result)  # type: ignore[arg-type]
        try:
            await self._app(scope, receive, send)
        finally:
            current_github_login.reset(token)

    def _authorised(self, scope: Scope) -> "str | None | _RejectedType":
        for name, value in scope.get("headers", []):
            if name.lower() != b"authorization":
                continue
            presented = value.decode("latin-1", "replace")
            if not presented.lower().startswith("bearer "):
                return REJECTED
            credential = presented.split(" ", 1)[1].strip()
            for check in self._checkers:
                result = check(credential)
                if result is not REJECTED:
                    return result
            return REJECTED
        return REJECTED

    @staticmethod
    async def _respond(
        send: Send,
        status: int,
        body: bytes,
        *,
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *(extra or []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _static_token_checker(token: str) -> Checker:
    def check(presented: str) -> "str | None | _RejectedType":
        if secrets.compare_digest(presented, token):
            return None  # valid, but this credential carries no per-user identity
        return REJECTED

    return check


def _github_oauth_checker(oauth_config) -> Checker:  # noqa: ANN001
    from . import github_oauth  # noqa: PLC0415 - optional, only needed here

    def check(presented: str) -> "str | None | _RejectedType":
        login = github_oauth.verify_session_token(oauth_config, presented)
        return login if login is not None else REJECTED

    return check


def build_app(mcp, transport: str):  # noqa: ANN001 - FastMCP instance
    """Assemble the ASGI app for a remote transport.

    Composition, not just gating: when GitHub OAuth is configured, its routes
    (metadata, registration, authorize, callback, token) are mounted at fixed
    paths ahead of the MCP transport's own catch-all, and those exact paths
    are added to the gate's open list. The MCP transport itself — matched
    only by the trailing catch-all ``Mount`` — still requires a valid bearer
    credential, static or OAuth-issued.
    """
    if transport == "sse":
        transport_app = mcp.sse_app()
    elif transport in {"http", "streamable-http"}:
        transport_app = mcp.streamable_http_app()
    else:
        raise ValueError(f"{transport!r} is not a remote transport")

    static_token = os.getenv("MCP_INBOUND_TOKEN", "").strip()
    if static_token and len(static_token) < 24:
        raise SystemExit("MCP_INBOUND_TOKEN is too short; use at least 24 characters")

    from . import github_oauth  # noqa: PLC0415 - optional, only needed here

    oauth_config = github_oauth.config_from_env()

    checkers: list[Checker] = []
    if static_token:
        checkers.append(_static_token_checker(static_token))
    if oauth_config is not None:
        checkers.append(_github_oauth_checker(oauth_config))

    if not checkers:
        raise SystemExit(
            "No inbound credential is configured for the remote transport.\n"
            "This endpoint exposes sparql_update and push_turtle to anyone who\n"
            "can reach the URL, and it carries the bridge's own token, so an\n"
            "unauthenticated caller inherits full write access.\n"
            "Set at least one of:\n"
            "  MCP_INBOUND_TOKEN=<random>   — a shared static token, or\n"
            "  GITHUB_OAUTH_CLIENT_ID=...   — GitHub-identified sessions "
            "(see holonbridge_mcp.github_oauth for the rest of that setup)\n"
            "Generate a static token with:\n"
            "  [Convert]::ToBase64String("
            "[Security.Cryptography.RandomNumberGenerator]::GetBytes(32))"
        )

    routes: list[BaseRoute] = []
    # Discovery/registration paths are exempt from the gate unconditionally,
    # not only when GitHub OAuth is configured. A client that speaks the MCP
    # Authorization spec probes these BEFORE it ever tries a credential it
    # already has — a 401 here reads as "you must authenticate to find out
    # how to authenticate," which stalls a client that only needed the plain
    # static token and never needed OAuth at all. Hit during development:
    # with no GitHub OAuth configured, these paths have no route registered
    # behind them at all, so exempting them just lets the request fall
    # through the transport Mount to a plain 404 — the correct "not
    # supported here" signal, and nothing about it is sensitive to reveal.
    open_paths = BASE_OPEN_PATHS + github_oauth.OAUTH_PATHS
    if oauth_config is not None:
        routes.extend(github_oauth.build_routes())
        log.info(
            "GitHub OAuth enabled — allowed logins: %s",
            ", ".join(sorted(oauth_config.allowed_logins)),
        )
    routes.append(Mount("/", app=transport_app))

    app = Starlette(routes=routes)
    if oauth_config is not None:
        app.state.github_oauth_config = oauth_config
        app.state.oauth_state = github_oauth.OAuthState()

    return BearerGate(app, checkers, open_paths=open_paths)


def serve(mcp, *, transport: str, host: str, port: int) -> None:  # noqa: ANN001
    """Run a remote transport under uvicorn."""
    import uvicorn

    app = build_app(mcp, transport)
    path = "/sse" if transport == "sse" else "/mcp"
    log.info("holonbridge-mcp listening on http://%s:%d%s (%s)", host, port, path, transport)
    uvicorn.run(app, host=host, port=port, log_level="info")
