"""GitHub OAuth authorization-server tests.

GitHub's own endpoints are monkeypatched at the module functions
`_exchange_github_code` / `_fetch_github_login` — the two, and only two,
places this module ever talks to the outside world. Everything else here is
exercised for real: registration, PKCE, the allowlist, one-time codes, JWTs.
"""

from __future__ import annotations

import time

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from holonbridge_mcp import github_oauth as oauth

CONFIG = oauth.GitHubOAuthConfig(
    client_id="gh-client-id",
    client_secret="gh-client-secret",
    public_url="https://bridge.example.ngrok.io",
    jwt_secret="test-jwt-signing-secret-at-least-32-bytes-long",
    allowed_logins=frozenset({"kurtcagle"}),
    token_ttl_seconds=3600,
)


def make_app(config: oauth.GitHubOAuthConfig = CONFIG) -> Starlette:
    app = Starlette(routes=oauth.build_routes())
    app.state.github_oauth_config = config
    app.state.oauth_state = oauth.OAuthState()
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(make_app(), follow_redirects=False)


def pkce_pair() -> tuple[str, str]:
    """A matching (verifier, S256 challenge) pair."""
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def register_client(client: TestClient, redirect_uri: str = "https://claude.ai/mcp/callback") -> str:
    response = client.post("/register", json={"redirect_uris": [redirect_uri]})
    assert response.status_code == 201
    return response.json()["client_id"]


# --- PKCE ---------------------------------------------------------------------


def test_pkce_verifies_a_matching_pair():
    verifier, challenge = pkce_pair()
    assert oauth.verify_pkce(verifier, challenge) is True


def test_pkce_rejects_a_wrong_verifier():
    _, challenge = pkce_pair()
    assert oauth.verify_pkce("some-other-verifier", challenge) is False


# --- JWTs -----------------------------------------------------------------------


def test_a_valid_token_verifies_and_carries_the_login():
    token, ttl = oauth.issue_session_token(CONFIG, "kurtcagle")
    assert ttl == CONFIG.token_ttl_seconds
    assert oauth.verify_session_token(CONFIG, token) == "kurtcagle"


def test_a_token_signed_with_a_different_secret_is_rejected():
    other = oauth.GitHubOAuthConfig(
        **{**CONFIG.__dict__, "jwt_secret": "a-completely-different-secret-also-32-bytes-plus"}
    )
    token, _ = oauth.issue_session_token(other, "kurtcagle")
    assert oauth.verify_session_token(CONFIG, token) is None


def test_an_expired_token_is_rejected():
    expired = oauth.GitHubOAuthConfig(**{**CONFIG.__dict__, "token_ttl_seconds": -10})
    token, _ = oauth.issue_session_token(expired, "kurtcagle")
    assert oauth.verify_session_token(CONFIG, token) is None


def test_garbage_is_rejected_not_raised():
    assert oauth.verify_session_token(CONFIG, "not.a.jwt") is None


# --- registration --------------------------------------------------------------


def test_register_requires_redirect_uris(client):
    response = client.post("/register", json={})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client_metadata"


def test_register_issues_a_public_client(client):
    response = client.post("/register", json={"redirect_uris": ["https://claude.ai/cb"]})
    body = response.json()
    assert response.status_code == 201
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == ["https://claude.ai/cb"]
    assert body["client_id"]


# --- metadata -------------------------------------------------------------------


def test_authorization_server_metadata_is_reachable_unauthenticated(client):
    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    body = response.json()
    assert body["authorization_endpoint"] == f"{CONFIG.public_url}/authorize"
    assert body["code_challenge_methods_supported"] == ["S256"]


def test_protected_resource_metadata_is_reachable_unauthenticated(client):
    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200


def test_protected_resource_metadata_is_also_served_at_the_transport_suffix(client):
    """RFC 9728 §3.1 publishes this at a path suffixed with the resource's
    own path. Confirmed against a real claude.ai connector trace: it
    requests the /sse-suffixed variant specifically — serving only the bare
    path leaves this one 404 for an actual client even though it looks
    complete against the spec text alone.
    """
    bare = client.get("/.well-known/oauth-protected-resource").json()
    suffixed = client.get("/.well-known/oauth-protected-resource/sse")
    assert suffixed.status_code == 200
    assert suffixed.json() == bare


# --- /authorize -----------------------------------------------------------------


def test_authorize_rejects_an_unregistered_client(client):
    verifier, challenge = pkce_pair()
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "never-registered",
            "redirect_uri": "https://claude.ai/cb",
            "state": "mcp-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_client"


def test_authorize_rejects_an_unregistered_redirect_uri(client):
    client_id = register_client(client, "https://claude.ai/cb")
    _, challenge = pkce_pair()
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://evil.example/steal",
            "state": "mcp-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    # An unverified redirect_uri must never be redirected to — that would
    # make this endpoint an open redirector.
    assert response.status_code == 400
    assert "redirect" in response.json()["error_description"]


def test_authorize_redirects_a_valid_request_to_github(client):
    client_id = register_client(client, "https://claude.ai/cb")
    _, challenge = pkce_pair()
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/cb",
            "state": "mcp-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(oauth.GITHUB_AUTHORIZE_URL)
    assert "client_id=gh-client-id" in location
    assert "scope=read%3Auser" in location


def test_authorize_denies_a_non_s256_challenge_by_redirecting_with_an_error(client):
    client_id = register_client(client, "https://claude.ai/cb")
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/cb",
            "state": "mcp-state",
            "code_challenge": "whatever",
            "code_challenge_method": "plain",
        },
    )
    # redirect_uri IS verified by this point, so an error redirect is safe
    # and correct here — unlike the unregistered-client case above.
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://claude.ai/cb?")
    assert "error=invalid_request" in response.headers["location"]


def test_authorize_rejects_a_non_code_response_type(client):
    response = client.get("/authorize", params={"response_type": "token"})
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_response_type"


# --- /callback/github -----------------------------------------------------------


async def _fake_exchange(config, code):
    assert code == "github-code-123"
    return "gh-access-token"


async def _fake_fetch_allowed(token):
    assert token == "gh-access-token"
    return "kurtcagle"


async def _fake_fetch_denied(token):
    return "someone-else"


def _authorize_then_capture_state(client: TestClient) -> tuple[str, str]:
    """Drive /authorize and pull the req_id GitHub would echo back as state."""
    client_id = register_client(client, "https://claude.ai/cb")
    verifier, challenge = pkce_pair()
    authorize = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/cb",
            "state": "mcp-original-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    from urllib.parse import parse_qs, urlparse

    req_id = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    return req_id, verifier


def test_callback_completes_the_loop_for_an_allowed_login(client, monkeypatch):
    monkeypatch.setattr(oauth, "_exchange_github_code", _fake_exchange)
    monkeypatch.setattr(oauth, "_fetch_github_login", _fake_fetch_allowed)

    req_id, _ = _authorize_then_capture_state(client)
    response = client.get(
        "/callback/github", params={"code": "github-code-123", "state": req_id}
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://claude.ai/cb?")
    assert "state=mcp-original-state" in location
    assert "code=" in location
    assert "error" not in location


def test_callback_denies_a_login_not_on_the_allowlist(client, monkeypatch):
    monkeypatch.setattr(oauth, "_exchange_github_code", _fake_exchange)
    monkeypatch.setattr(oauth, "_fetch_github_login", _fake_fetch_denied)

    req_id, _ = _authorize_then_capture_state(client)
    response = client.get(
        "/callback/github", params={"code": "github-code-123", "state": req_id}
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://claude.ai/cb?")
    assert "error=access_denied" in location
    assert "state=mcp-original-state" in location


def test_callback_with_an_unknown_state_gets_a_direct_error_not_a_redirect(client):
    # No verified redirect_uri is known here, so redirecting would be unsafe —
    # this is the one legitimate direct-error case in the whole flow.
    response = client.get(
        "/callback/github", params={"code": "whatever", "state": "never-issued"}
    )
    assert response.status_code == 400
    assert "location" not in response.headers


def test_callback_state_is_single_use(client, monkeypatch):
    monkeypatch.setattr(oauth, "_exchange_github_code", _fake_exchange)
    monkeypatch.setattr(oauth, "_fetch_github_login", _fake_fetch_allowed)

    req_id, _ = _authorize_then_capture_state(client)
    first = client.get("/callback/github", params={"code": "github-code-123", "state": req_id})
    assert first.status_code == 302

    second = client.get("/callback/github", params={"code": "github-code-123", "state": req_id})
    assert second.status_code == 400  # the state was already consumed


def test_callback_propagates_github_denial(client):
    req_id, _ = _authorize_then_capture_state(client)
    response = client.get(
        "/callback/github", params={"error": "access_denied", "state": req_id}
    )
    assert response.status_code == 302
    assert "error=access_denied" in response.headers["location"]


# --- /token ---------------------------------------------------------------------


def _get_full_code(client: TestClient, monkeypatch) -> tuple[str, str, str]:
    """Drive the whole flow up to an issued code. Returns (code, verifier, redirect_uri)."""
    monkeypatch.setattr(oauth, "_exchange_github_code", _fake_exchange)
    monkeypatch.setattr(oauth, "_fetch_github_login", _fake_fetch_allowed)

    client_id = register_client(client, "https://claude.ai/cb")
    verifier, challenge = pkce_pair()
    authorize = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/cb",
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    from urllib.parse import parse_qs, urlparse

    req_id = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    callback = client.get(
        "/callback/github", params={"code": "github-code-123", "state": req_id}
    )
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    return code, verifier, "https://claude.ai/cb"


def test_token_exchange_succeeds_with_the_right_verifier(client, monkeypatch):
    code, verifier, redirect_uri = _get_full_code(client, monkeypatch)
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert oauth.verify_session_token(CONFIG, body["access_token"]) == "kurtcagle"


def test_token_exchange_rejects_a_wrong_verifier(client, monkeypatch):
    code, _, redirect_uri = _get_full_code(client, monkeypatch)
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": "wrong-verifier-entirely",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_the_code_is_single_use(client, monkeypatch):
    code, verifier, redirect_uri = _get_full_code(client, monkeypatch)
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    first = client.post("/token", data=body)
    assert first.status_code == 200
    second = client.post("/token", data=body)
    assert second.status_code == 400


def test_token_rejects_a_mismatched_redirect_uri(client, monkeypatch):
    code, verifier, _ = _get_full_code(client, monkeypatch)
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://not-the-right-one.example/cb",
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 400


def test_token_rejects_the_wrong_grant_type(client):
    response = client.post("/token", data={"grant_type": "client_credentials"})
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


def test_a_denied_login_never_reaches_token_exchange(client, monkeypatch):
    monkeypatch.setattr(oauth, "_exchange_github_code", _fake_exchange)
    monkeypatch.setattr(oauth, "_fetch_github_login", _fake_fetch_denied)

    client_id = register_client(client, "https://claude.ai/cb")
    verifier, challenge = pkce_pair()
    authorize = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/cb",
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    from urllib.parse import parse_qs, urlparse

    req_id = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    client.get("/callback/github", params={"code": "github-code-123", "state": req_id})

    # the login was denied, so no code was ever issued — any token attempt fails
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "anything-at-all",
            "redirect_uri": "https://claude.ai/cb",
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 400


# --- config_from_env --------------------------------------------------------------


def test_unset_client_id_means_oauth_is_off(monkeypatch):
    assert oauth.config_from_env() is None


def test_partial_config_refuses_to_start(monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "abc")
    with pytest.raises(SystemExit, match="GITHUB_OAUTH_CLIENT_SECRET"):
        oauth.config_from_env()


def test_empty_allowlist_refuses_to_start(monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "abc")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "def")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://x.example")
    monkeypatch.setenv("MCP_JWT_SECRET", "secret")
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_LOGINS", "  ,  ,")
    with pytest.raises(SystemExit, match="empty allowlist"):
        oauth.config_from_env()


def test_full_config_loads_and_lowercases_logins(monkeypatch):
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "abc")
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_SECRET", "def")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://x.example")
    monkeypatch.setenv("MCP_JWT_SECRET", "secret")
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_LOGINS", "KurtCagle, someoneElse")
    config = oauth.config_from_env()
    assert config is not None
    assert config.allowed_logins == frozenset({"kurtcagle", "someoneelse"})
    assert config.allows("kurtcagle") and config.allows("KURTCAGLE")
