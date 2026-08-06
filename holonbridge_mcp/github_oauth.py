"""A small OAuth 2.0 authorization server, backed by GitHub identity.

This exists to answer one question: *which human is this?* — not to act on
GitHub on their behalf. It never persists a GitHub access token; it reads
``login`` once at the end of the GitHub leg and discards the token
immediately. The scope requested is ``read:user``, nothing broader.

Two OAuth flows are layered here, and they must not be confused:

1. **MCP client ↔ this server.** Claude's MCP client speaks standard
   authorization-code-with-PKCE OAuth (RFC 6749 + RFC 7636), preceded by
   Dynamic Client Registration (RFC 7591). This server is the authorization
   server for *that* flow.
2. **This server ↔ GitHub.** A second, ordinary OAuth exchange, used purely
   to establish identity. From the MCP client's point of view this is
   invisible — it only ever sees flow 1.

``/authorize`` bridges the two: it receives flow 1's request, stashes it,
and redirects the browser into flow 2. ``/callback/github`` receives flow
2's result, checks the GitHub login against an allowlist, and completes
flow 1 by redirecting back to the MCP client with an authorization code of
its own.

Each flow has its own ``client_id``, and conflating them is the single most
likely setup mistake — see ``authorize`` below, which detects it explicitly.

State is in-memory with short TTLs — this is a single-process bridge, and
the whole handshake takes seconds. A restart mid-handshake drops it; a
completed session survives as a signed JWT and needs nothing further from
this process to verify.

Only ``S256`` PKCE is accepted. ``plain`` is legal per spec for constrained
clients but weaker, and every MCP client encountered so far uses S256, so
there is no compatibility reason to accept it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

log = logging.getLogger("holonbridge_mcp.github_oauth")

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_SCOPE = "read:user"

REGISTRATION_TTL = 24 * 3600 * 365  # client registrations persist for process life
PENDING_AUTH_TTL = 600  # time allowed to complete the GitHub consent screen
ISSUED_CODE_TTL = 60  # standard-length authorization code lifetime

JWT_ALGORITHM = "HS256"

#: Prefixes GitHub uses for its own OAuth App / GitHub App client IDs. Only
#: used to make a misconfiguration diagnose itself — see ``authorize``.
GITHUB_CLIENT_ID_PREFIXES = ("Ov23li", "Iv1.", "Iv23li")


class OAuthError(ValueError):
    """A request violates the OAuth flow. Carries the RFC 6749 error code."""

    def __init__(self, code: str, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


@dataclass(frozen=True)
class GitHubOAuthConfig:
    """Everything the flow needs, resolved once from the environment."""

    client_id: str
    client_secret: str
    public_url: str  # this server's own externally reachable base URL
    jwt_secret: str
    allowed_logins: frozenset[str]  # lowercased GitHub logins
    token_ttl_seconds: int = 43_200  # 12h

    @property
    def callback_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/callback/github"

    def allows(self, login: str) -> bool:
        return login.lower() in self.allowed_logins


# --- ephemeral state ------------------------------------------------------


@dataclass
class _Entry:
    value: Any
    expires_at: float


class _TtlStore:
    """A dict that forgets. Checked lazily on read — no background sweep."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def put(self, key: str, value: Any, ttl: float) -> None:
        self._entries[key] = _Entry(value, time.monotonic() + ttl)

    def peek(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            del self._entries[key]
            return None
        return entry.value

    def pop(self, key: str) -> Any | None:
        """Read once and discard — for anything single-use, like a code."""
        value = self.peek(key)
        self._entries.pop(key, None)
        return value


@dataclass
class ClientRegistration:
    client_id: str
    redirect_uris: list[str]


@dataclass
class PendingAuthorization:
    """Flow 1's request, stashed while flow 2 runs."""

    mcp_redirect_uri: str
    mcp_state: str
    code_challenge: str


@dataclass
class IssuedCode:
    """A one-time code handed to the MCP client, redeemable at /token."""

    redirect_uri: str
    code_challenge: str
    github_login: str


class OAuthState:
    """All the in-memory bookkeeping the flow needs, in one place."""

    def __init__(self) -> None:
        self.clients = _TtlStore()
        self.pending = _TtlStore()
        self.codes = _TtlStore()

    def register_client(self, redirect_uris: list[str]) -> ClientRegistration:
        client_id = secrets.token_urlsafe(16)
        registration = ClientRegistration(client_id=client_id, redirect_uris=redirect_uris)
        self.clients.put(client_id, registration, REGISTRATION_TTL)
        return registration

    def get_client(self, client_id: str) -> ClientRegistration | None:
        return self.clients.peek(client_id)


# --- PKCE and JWT -----------------------------------------------------------


def verify_pkce(verifier: str, challenge: str) -> bool:
    """S256 only: SHA-256 of the verifier, base64url, no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, challenge)


def issue_session_token(config: GitHubOAuthConfig, github_login: str) -> tuple[str, int]:
    now = int(time.time())
    payload = {
        "sub": github_login,
        "iss": config.public_url,
        "iat": now,
        "exp": now + config.token_ttl_seconds,
    }
    token = jwt.encode(payload, config.jwt_secret, algorithm=JWT_ALGORITHM)
    return token, config.token_ttl_seconds


def verify_session_token(config: GitHubOAuthConfig, token: str) -> str | None:
    """Return the GitHub login a valid token carries, or ``None``.

    Verification is fully stateless — signature and expiry only. A login
    revoked from the allowlist mid-session is not re-checked until the token
    expires; the tradeoff is a short TTL rather than a per-request lookup.
    """
    try:
        payload = jwt.decode(token, config.jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    return payload.get("sub")


# --- GitHub calls (isolated for test monkeypatching) -------------------------


async def _exchange_github_code(config: GitHubOAuthConfig, code: str) -> str:
    """Trade GitHub's code for an access token. Never stored past this call."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code": code,
                "redirect_uri": config.callback_url,
            },
            headers={"Accept": "application/json"},
        )
    if response.status_code >= 400:
        raise OAuthError("server_error", f"GitHub token exchange failed: {response.status_code}")
    body = response.json()
    token = body.get("access_token")
    if not token:
        raise OAuthError(
            "server_error", f"GitHub did not return an access token: {body.get('error', body)}"
        )
    return token


async def _fetch_github_login(github_token: str) -> str:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            GITHUB_USER_URL,
            headers={"Authorization": f"token {github_token}", "Accept": "application/json"},
        )
    if response.status_code >= 400:
        raise OAuthError("server_error", f"GitHub user lookup failed: {response.status_code}")
    login = response.json().get("login")
    if not login:
        raise OAuthError("server_error", "GitHub user response carried no login")
    return login


# --- route handlers -----------------------------------------------------------


def _base(request: Request) -> str:
    """The public URL to advertise, overridable so a tunnel URL is used."""
    config: GitHubOAuthConfig = request.app.state.github_oauth_config
    return config.public_url.rstrip("/")


def _config(request: Request) -> GitHubOAuthConfig:
    return request.app.state.github_oauth_config


def _oauth_state(request: Request) -> OAuthState:
    return request.app.state.oauth_state


async def metadata(request: Request) -> JSONResponse:
    base = _base(request)
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "registration_endpoint": f"{base}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


async def protected_resource_metadata(request: Request) -> JSONResponse:
    base = _base(request)
    return JSONResponse({"resource": base, "authorization_servers": [base]})


async def register(request: Request) -> JSONResponse:
    """RFC 7591 Dynamic Client Registration.

    Clients are treated as public — PKCE is the security boundary, not a
    client secret, so none is issued. ``token_endpoint_auth_method: "none"``
    says so explicitly rather than leaving it to be inferred.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON is a client error
        body = {}

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "redirect_uris is required and must be a non-empty list",
            },
            status_code=400,
        )

    registration = _oauth_state(request).register_client([str(u) for u in redirect_uris])
    return JSONResponse(
        {
            "client_id": registration.client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": registration.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
    )


async def authorize(request: Request) -> Any:
    """Flow 1's entry point. Validates, then redirects into flow 2 (GitHub).

    Errors before the redirect_uri is verified are returned directly rather
    than as a redirect — redirecting an error to an unverified URI is itself
    a vulnerability (it turns this endpoint into an open redirector).
    """
    params = request.query_params
    state = _oauth_state(request)
    config = _config(request)

    if params.get("response_type") != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    client_id = params.get("client_id", "")
    registration = state.get_client(client_id)
    if registration is None:
        # The predictable trap, hit for real during setup: a bare
        # "invalid_client" teaches nobody anything. Two OAuth relationships
        # are in play — the MCP client registering with *this* server, and
        # this server asking GitHub who the user is — and each has its own
        # client_id. Putting the GitHub one into the MCP client's connector
        # config sends it here, where it was never issued and cannot be
        # recognised. Say so, rather than making the operator guess.
        looks_like_github = client_id.startswith(GITHUB_CLIENT_ID_PREFIXES)
        description = "client_id was not issued by this server. " + (
            "That value looks like a GitHub OAuth App client ID. This server "
            "is the authorization server for your MCP client, and GitHub is "
            "only used behind it to establish identity — the two have "
            "separate client IDs. Clear the client ID and secret in your MCP "
            "client's connector settings so it registers itself via Dynamic "
            "Client Registration (RFC 7591) at /register; keep "
            "GITHUB_OAUTH_CLIENT_ID in this server's environment, where it is "
            "used to talk to GitHub."
            if looks_like_github
            else "Register first at /register (RFC 7591 Dynamic Client "
            "Registration), or clear any manually configured client ID in "
            "your MCP client so it registers itself."
        )
        log.warning(
            "rejected /authorize for unregistered client_id %r%s",
            client_id,
            " (looks like a GitHub App client ID — see the error body)"
            if looks_like_github
            else "",
        )
        return JSONResponse(
            {"error": "invalid_client", "error_description": description},
            status_code=400,
        )

    redirect_uri = params.get("redirect_uri", "")
    if redirect_uri not in registration.redirect_uris:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri not registered"},
            status_code=400,
        )

    # redirect_uri is now verified; failures from here on are safe to redirect.
    mcp_state = params.get("state", "")

    def deny(code: str, description: str) -> RedirectResponse:
        query = urlencode({"error": code, "error_description": description, "state": mcp_state})
        return RedirectResponse(f"{redirect_uri}?{query}", status_code=302)

    if params.get("code_challenge_method") != "S256":
        return deny("invalid_request", "only S256 PKCE is accepted")

    code_challenge = params.get("code_challenge", "")
    if not code_challenge:
        return deny("invalid_request", "code_challenge is required")

    req_id = secrets.token_urlsafe(24)
    state.pending.put(
        req_id,
        PendingAuthorization(
            mcp_redirect_uri=redirect_uri, mcp_state=mcp_state, code_challenge=code_challenge
        ),
        PENDING_AUTH_TTL,
    )

    github_query = urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.callback_url,
            "scope": GITHUB_SCOPE,
            "state": req_id,
            "allow_signup": "false",
        }
    )
    return RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{github_query}", status_code=302)


async def github_callback(request: Request) -> Any:
    """Flow 2's return leg. Completes flow 1 with the bridge's own code."""
    params = request.query_params
    state = _oauth_state(request)
    config = _config(request)

    req_id = params.get("state", "")
    pending: PendingAuthorization | None = state.pending.pop(req_id)
    if pending is None:
        # No verified redirect_uri to send this to — this is the one case
        # where a direct error page, not a redirect, is the honest answer.
        return PlainTextResponse(
            "Authorization request expired or was not recognized. "
            "Please retry the connection from your MCP client.",
            status_code=400,
        )

    def deny(description: str) -> RedirectResponse:
        query = urlencode(
            {"error": "access_denied", "error_description": description, "state": pending.mcp_state}
        )
        return RedirectResponse(f"{pending.mcp_redirect_uri}?{query}", status_code=302)

    if params.get("error"):
        return deny(f"GitHub denied the request: {params.get('error')}")

    code = params.get("code", "")
    if not code:
        return deny("GitHub did not return an authorization code")

    try:
        github_token = await _exchange_github_code(config, code)
        login = await _fetch_github_login(github_token)
    except OAuthError as exc:
        log.warning("GitHub identity check failed: %s", exc.description)
        return deny("could not verify GitHub identity")
    # github_token deliberately goes out of scope here and is never stored —
    # its only purpose was to answer "who is this."

    if not config.allows(login):
        log.info("GitHub login %r authenticated but is not on the allowlist", login)
        return deny(f"GitHub account {login!r} is not authorized for this bridge")

    issued = secrets.token_urlsafe(24)
    state.codes.put(
        issued,
        IssuedCode(
            redirect_uri=pending.mcp_redirect_uri,
            code_challenge=pending.code_challenge,
            github_login=login,
        ),
        ISSUED_CODE_TTL,
    )
    query = urlencode({"code": issued, "state": pending.mcp_state})
    return RedirectResponse(f"{pending.mcp_redirect_uri}?{query}", status_code=302)


async def token(request: Request) -> JSONResponse:
    """Flow 1's final leg. Verifies PKCE, issues a signed session token."""
    state = _oauth_state(request)
    config = _config(request)
    form = await request.form()

    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = str(form.get("code", ""))
    issued: IssuedCode | None = state.codes.pop(code)
    if issued is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if str(form.get("redirect_uri", "")) != issued.redirect_uri:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    verifier = str(form.get("code_verifier", ""))
    if not verifier or not verify_pkce(verifier, issued.code_challenge):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    access_token, ttl = issue_session_token(config, issued.github_login)
    return JSONResponse({"access_token": access_token, "token_type": "Bearer", "expires_in": ttl})


# --- wiring -------------------------------------------------------------------

#: Every path this module serves. Used by the caller to keep these open while
#: gating everything else — see holonbridge_mcp.remote.
OAUTH_PATHS: tuple[str, ...] = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    # RFC 9728 §3.1 publishes protected-resource metadata at a path suffixed
    # with the resource's own path. Confirmed live against claude.ai's actual
    # connector: it requests the /sse-suffixed variant specifically, not just
    # the bare one — the bare one alone leaves this 401 for real clients even
    # though it looks complete against the spec text.
    "/.well-known/oauth-protected-resource/sse",
    "/.well-known/oauth-protected-resource/mcp",
    "/register",
    "/authorize",
    "/callback/github",
    "/token",
)


def build_routes() -> list[Route]:
    return [
        Route("/.well-known/oauth-authorization-server", metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
        # Same handler, same content — the suffix is only where the client
        # looked, per RFC 9728 §3.1, not a different resource description.
        Route(
            "/.well-known/oauth-protected-resource/sse",
            protected_resource_metadata,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-protected-resource/mcp",
            protected_resource_metadata,
            methods=["GET"],
        ),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET"]),
        Route("/callback/github", github_callback, methods=["GET"]),
        Route("/token", token, methods=["POST"]),
    ]


def config_from_env() -> GitHubOAuthConfig | None:
    """Build the config from the environment, or ``None`` if unconfigured.

    Presence of ``GITHUB_OAUTH_CLIENT_ID`` is what turns this on. Once it is
    set, every other variable is required — a half-configured OAuth layer
    (a client ID with no allowlist, say) is worse than none, so this fails
    loudly rather than guessing at a permissive default.
    """
    import os

    client_id = os.getenv("GITHUB_OAUTH_CLIENT_ID", "").strip()
    if not client_id:
        return None

    required = {
        "GITHUB_OAUTH_CLIENT_SECRET": os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "").strip(),
        "MCP_PUBLIC_URL": os.getenv("MCP_PUBLIC_URL", "").strip(),
        "MCP_JWT_SECRET": os.getenv("MCP_JWT_SECRET", "").strip(),
        "MCP_ALLOWED_GITHUB_LOGINS": os.getenv("MCP_ALLOWED_GITHUB_LOGINS", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(
            "GITHUB_OAUTH_CLIENT_ID is set, which enables GitHub OAuth, but "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not.\n"
            "A partially configured OAuth layer is worse than none — fix the "
            "environment or unset GITHUB_OAUTH_CLIENT_ID to disable it."
        )

    allowed = frozenset(
        login.strip().lower()
        for login in required["MCP_ALLOWED_GITHUB_LOGINS"].split(",")
        if login.strip()
    )
    if not allowed:
        raise SystemExit(
            "MCP_ALLOWED_GITHUB_LOGINS is set but contains no usable logins. "
            "An OAuth layer with an empty allowlist authenticates identity "
            "but authorizes no one — which usually means everyone falls "
            "through to invalid_grant. List at least one GitHub login."
        )

    ttl = int(os.getenv("MCP_JWT_TTL_SECONDS", "43200"))

    return GitHubOAuthConfig(
        client_id=client_id,
        client_secret=required["GITHUB_OAUTH_CLIENT_SECRET"],
        public_url=required["MCP_PUBLIC_URL"],
        jwt_secret=required["MCP_JWT_SECRET"],
        allowed_logins=allowed,
        token_ttl_seconds=ttl,
    )
