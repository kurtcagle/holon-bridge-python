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

The handshake state (pending requests, one-time codes) is in-memory with
short TTLs — the whole handshake takes seconds, and a restart mid-handshake
just drops it. A completed session survives as a signed JWT and needs nothing
further from this process to verify.

Two things are durable, in a small SQLite file under ``MCP_DATA_DIR`` when
that is set (in memory, lost on restart, when it is not): client registrations
(RFC 7591), so a restart does not force every client to re-register, and
refresh tokens, so a client can renew its short-lived access token without a
person re-authorising. Refresh tokens are stored only as SHA-256 hashes,
rotate on every use, and a replayed one revokes its whole family. The
allowlist is re-checked at every refresh, which is what bounds revocation to
one access-token lifetime without a per-request lookup.

Only ``S256`` PKCE is accepted. ``plain`` is legal per spec for constrained
clients but weaker, and every MCP client encountered so far uses S256, so
there is no compatibility reason to accept it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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

REGISTRATION_TTL = 24 * 3600 * 365  # client registrations: long-lived, one year
PENDING_AUTH_TTL = 600  # time allowed to complete the GitHub consent screen
ISSUED_CODE_TTL = 60  # standard-length authorization code lifetime

JWT_ALGORITHM = "HS256"

DEFAULT_REFRESH_TTL_SECONDS = 30 * 24 * 3600  # 30 days, sliding (renewed at each use)
DB_FILENAME = "oauth.sqlite3"

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
    refresh_ttl_seconds: int = DEFAULT_REFRESH_TTL_SECONDS
    data_dir: str | None = None  # where registrations/refresh tokens persist; None = in memory

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
    client_id: str = ""


@dataclass
class IssuedCode:
    """A one-time code handed to the MCP client, redeemable at /token."""

    redirect_uri: str
    code_challenge: str
    github_login: str
    client_id: str = ""


def hash_refresh_token(raw: str) -> str:
    """SHA-256 of an opaque refresh token. The raw value is never stored.

    A fast hash is right here: the token is 256 bits of randomness, so there
    is nothing to brute-force and no need for a slow password hash.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id     TEXT PRIMARY KEY,
    redirect_uris TEXT NOT NULL,
    expires_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash   TEXT PRIMARY KEY,
    family_id    TEXT NOT NULL,
    client_id    TEXT NOT NULL,
    github_login TEXT NOT NULL,
    expires_at   REAL NOT NULL,
    used_at      REAL,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS refresh_tokens_family ON refresh_tokens (family_id);
"""


class _Db:
    """Client registrations and refresh tokens, in SQLite.

    With a directory the file persists (mode 0600, directory 0700); with
    ``None`` it is an in-memory database, so behaviour without configuration
    is the old one: everything is lost on restart. SQLite's own locking makes a
    second process on the same file safe; the rotation below is a single
    ``BEGIN IMMEDIATE`` transaction so two racing refreshes cannot both win.
    """

    def __init__(self, data_dir: str | os.PathLike[str] | None) -> None:
        self._lock = threading.Lock()
        if data_dir is None:
            target = ":memory:"
        else:
            directory = Path(data_dir)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / DB_FILENAME
            # Create the file 0600 before SQLite does, never briefly wider.
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
            os.chmod(path, 0o600)
            target = str(path)
        self._conn = sqlite3.connect(
            target, check_same_thread=False, isolation_level=None, timeout=5.0
        )
        self._conn.executescript(_SCHEMA)

    # -- clients

    def put_client(self, client_id: str, redirect_uris: list[str], ttl: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO clients VALUES (?, ?, ?)",
                (client_id, json.dumps(redirect_uris), time.time() + ttl),
            )

    def get_client(self, client_id: str) -> ClientRegistration | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT redirect_uris, expires_at FROM clients WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None or row[1] < time.time():
            return None
        return ClientRegistration(client_id=client_id, redirect_uris=json.loads(row[0]))

    # -- refresh tokens

    def put_refresh(
        self, raw: str, *, family_id: str, client_id: str, github_login: str, ttl: float
    ) -> None:
        now = time.time()
        with self._lock:
            # Expired rows are useless for reuse detection too; drop them here.
            self._conn.execute("DELETE FROM refresh_tokens WHERE expires_at < ?", (now,))
            self._conn.execute(
                "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, ?, NULL, 0)",
                (hash_refresh_token(raw), family_id, client_id, github_login, now + ttl),
            )

    def revoke_family(self, family_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE refresh_tokens SET revoked = 1 WHERE family_id = ?", (family_id,)
            )

    def rotate_refresh(
        self,
        raw: str,
        *,
        client_id: str,
        is_allowed: Any,
        new_raw: str,
        ttl: float,
    ) -> str:
        """Atomically redeem ``raw`` and store its successor; return the login.

        Raises :class:`OAuthError` (``invalid_grant``) for anything wrong with
        the token. A replay of an already-used or revoked token revokes the
        whole family before raising.
        """
        digest = hash_refresh_token(raw)
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT family_id, client_id, github_login, expires_at, used_at, revoked "
                    "FROM refresh_tokens WHERE token_hash = ?",
                    (digest,),
                ).fetchone()
                if row is None:
                    raise OAuthError("invalid_grant", "unknown refresh token")
                family_id, token_client, login, expires_at, used_at, revoked = row
                if token_client != client_id:
                    # client_id is public, so this is not proof of theft and does
                    # not revoke; it just must not work.
                    raise OAuthError("invalid_grant", "refresh token was issued to another client")
                if used_at is not None or revoked:
                    self._conn.execute(
                        "UPDATE refresh_tokens SET revoked = 1 WHERE family_id = ?", (family_id,)
                    )
                    self._conn.execute("COMMIT")
                    log.warning(
                        "refresh token reuse for %r (client %r): family revoked", login, client_id
                    )
                    raise OAuthError("invalid_grant", "refresh token already used")
                if expires_at < now:
                    raise OAuthError("invalid_grant", "refresh token expired")
                if not is_allowed(login):
                    self._conn.execute(
                        "UPDATE refresh_tokens SET revoked = 1 WHERE family_id = ?", (family_id,)
                    )
                    self._conn.execute("COMMIT")
                    log.info("refresh refused: %r is no longer on the allowlist", login)
                    raise OAuthError("invalid_grant", "login is no longer authorized")
                self._conn.execute(
                    "UPDATE refresh_tokens SET used_at = ? WHERE token_hash = ?", (now, digest)
                )
                self._conn.execute(
                    "INSERT INTO refresh_tokens VALUES (?, ?, ?, ?, ?, NULL, 0)",
                    (hash_refresh_token(new_raw), family_id, client_id, login, now + ttl),
                )
                self._conn.execute("COMMIT")
                return login
            except OAuthError:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise


class OAuthState:
    """The bookkeeping the flow needs.

    ``pending`` and ``codes`` are ephemeral, in memory. Client registrations and
    refresh tokens live in :class:`_Db`: durable when ``data_dir`` is given.
    """

    def __init__(self, data_dir: str | os.PathLike[str] | None = None) -> None:
        self.db = _Db(data_dir)
        self.pending = _TtlStore()
        self.codes = _TtlStore()

    def register_client(self, redirect_uris: list[str]) -> ClientRegistration:
        client_id = secrets.token_urlsafe(16)
        self.db.put_client(client_id, redirect_uris, REGISTRATION_TTL)
        return ClientRegistration(client_id=client_id, redirect_uris=redirect_uris)

    def get_client(self, client_id: str) -> ClientRegistration | None:
        return self.db.get_client(client_id)

    def issue_refresh_token(
        self, config: GitHubOAuthConfig, *, client_id: str, github_login: str
    ) -> str:
        """Start a new refresh-token family (a fresh authorization)."""
        raw = secrets.token_urlsafe(32)
        self.db.put_refresh(
            raw,
            family_id=secrets.token_urlsafe(12),
            client_id=client_id,
            github_login=github_login,
            ttl=config.refresh_ttl_seconds,
        )
        return raw

    def redeem_refresh_token(
        self, config: GitHubOAuthConfig, raw: str, client_id: str
    ) -> tuple[str, str]:
        """Rotate a refresh token. Returns ``(github_login, new_refresh_token)``."""
        new_raw = secrets.token_urlsafe(32)
        login = self.db.rotate_refresh(
            raw,
            client_id=client_id,
            is_allowed=config.allows,
            new_raw=new_raw,
            ttl=config.refresh_ttl_seconds,
        )
        return login, new_raw


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
    The allowlist *is* re-checked each time a refresh token is redeemed, so
    revocation takes effect within one access-token lifetime.
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
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728 protected-resource metadata.

    ``resource`` must be the resource identifier the well-known URL was derived
    from (RFC 9728 §3.3): for ``/.well-known/oauth-protected-resource/mcp`` that
    is ``<public>/mcp``, not the bare origin. A client that validates this --
    a new Streamable HTTP connector may -- rejects the bare origin there.

    The bare and ``/sse``-suffixed variants keep returning the bare origin
    exactly as before: a connector registered against ``/sse`` works with it
    today, and changing what it sees would risk breaking something that works.
    """
    base = _base(request)
    resource = f"{base}/mcp" if request.url.path.rstrip("/").endswith("/mcp") else base
    return JSONResponse({"resource": resource, "authorization_servers": [base]})


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
            "grant_types": ["authorization_code", "refresh_token"],
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
            mcp_redirect_uri=redirect_uri,
            mcp_state=mcp_state,
            code_challenge=code_challenge,
            client_id=client_id,
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
            client_id=pending.client_id,
        ),
        ISSUED_CODE_TTL,
    )
    query = urlencode({"code": issued, "state": pending.mcp_state})
    return RedirectResponse(f"{pending.mcp_redirect_uri}?{query}", status_code=302)


def _token_response(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    # RFC 6749 §5.1: token responses must not be cached.
    return JSONResponse(
        body, status_code=status_code, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
    )


def _grant_error(code: str, description: str | None = None) -> JSONResponse:
    body = {"error": code}
    if description:
        body["error_description"] = description
    return _token_response(body, 400)


async def token(request: Request) -> JSONResponse:
    """Flow 1's final leg: the authorization-code and refresh-token grants."""
    state = _oauth_state(request)
    config = _config(request)
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "refresh_token":
        return _refresh_grant(state, config, form)
    if grant_type != "authorization_code":
        return _grant_error("unsupported_grant_type")

    code = str(form.get("code", ""))
    issued: IssuedCode | None = state.codes.pop(code)
    if issued is None:
        return _grant_error("invalid_grant")

    if str(form.get("redirect_uri", "")) != issued.redirect_uri:
        return _grant_error("invalid_grant")

    # Public clients may send client_id; when they do it must be the one the
    # code was issued to. Not required, so existing clients keep working.
    presented = str(form.get("client_id", ""))
    if presented and presented != issued.client_id:
        return _grant_error("invalid_grant")

    verifier = str(form.get("code_verifier", ""))
    if not verifier or not verify_pkce(verifier, issued.code_challenge):
        return _grant_error("invalid_grant")

    access_token, ttl = issue_session_token(config, issued.github_login)
    refresh_token = state.issue_refresh_token(
        config, client_id=issued.client_id, github_login=issued.github_login
    )
    return _token_response(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "refresh_token": refresh_token,
        }
    )


def _refresh_grant(state: OAuthState, config: GitHubOAuthConfig, form: Any) -> JSONResponse:
    """RFC 6749 §6, with rotation and family revocation (OAuth 2.1)."""
    raw = str(form.get("refresh_token", ""))
    client_id = str(form.get("client_id", ""))
    if not raw or not client_id:
        return _grant_error("invalid_request", "refresh_token and client_id are required")
    if state.get_client(client_id) is None:
        return _grant_error("invalid_client", "client_id was not issued by this server")
    try:
        login, new_refresh = state.redeem_refresh_token(config, raw, client_id)
    except OAuthError as exc:
        return _grant_error(exc.code, exc.description)
    access_token, ttl = issue_session_token(config, login)
    return _token_response(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "refresh_token": new_refresh,
        }
    )


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
        # The suffix is where the client looked, per RFC 9728 §3.1. The handler
        # answers with the matching resource identifier for /mcp and the bare
        # origin otherwise -- see protected_resource_metadata.
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
    refresh_ttl = int(os.getenv("MCP_REFRESH_TTL_SECONDS", str(DEFAULT_REFRESH_TTL_SECONDS)))
    if refresh_ttl <= 0:
        raise SystemExit("MCP_REFRESH_TTL_SECONDS must be a positive number of seconds.")
    data_dir = os.getenv("MCP_DATA_DIR", "").strip() or None

    return GitHubOAuthConfig(
        client_id=client_id,
        client_secret=required["GITHUB_OAUTH_CLIENT_SECRET"],
        public_url=required["MCP_PUBLIC_URL"],
        jwt_secret=required["MCP_JWT_SECRET"],
        allowed_logins=allowed,
        token_ttl_seconds=ttl,
        refresh_ttl_seconds=refresh_ttl,
        data_dir=data_dir,
    )
