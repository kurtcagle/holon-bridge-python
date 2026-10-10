"""Refresh tokens and persisted client registrations for the MCP OAuth layer."""

from __future__ import annotations

import sqlite3
import stat
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from holonbridge_mcp import github_oauth as oauth

REDIRECT = "https://claude.ai/cb"


def make_config(**overrides) -> oauth.GitHubOAuthConfig:
    base = dict(
        client_id="gh-client-id",
        client_secret="gh-client-secret",
        public_url="https://bridge.example.ngrok.io",
        jwt_secret="test-jwt-signing-secret-at-least-32-bytes-long",
        allowed_logins=frozenset({"kurtcagle"}),
        token_ttl_seconds=3600,
    )
    base.update(overrides)
    return oauth.GitHubOAuthConfig(**base)


def make_client(config=None, data_dir=None) -> TestClient:
    app = Starlette(routes=oauth.build_routes())
    app.state.github_oauth_config = config or make_config()
    app.state.oauth_state = oauth.OAuthState(data_dir)
    return TestClient(app, follow_redirects=False)


def authorize_fully(client: TestClient, monkeypatch, client_id: str | None = None) -> dict:
    """Run register -> authorize -> callback -> /token. Returns the token body + client_id."""
    from tests.test_github_oauth import pkce_pair  # noqa: PLC0415

    async def fake_exchange(config, code):
        return "gh-token"

    async def fake_login(token):
        return "kurtcagle"

    monkeypatch.setattr(oauth, "_exchange_github_code", fake_exchange)
    monkeypatch.setattr(oauth, "_fetch_github_login", fake_login)

    if client_id is None:
        client_id = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
    verifier, challenge = pkce_pair()
    auth = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    req_id = parse_qs(urlparse(auth.headers["location"]).query)["state"][0]
    cb = client.get("/callback/github", params={"code": "c", "state": req_id})
    code = parse_qs(urlparse(cb.headers["location"]).query)["code"][0]
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )
    assert resp.status_code == 200, resp.text
    return {**resp.json(), "client_id": client_id}


def refresh(client: TestClient, refresh_token: str, client_id: str):
    return client.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )


# --- issuing --------------------------------------------------------------------


def test_authorization_code_grant_issues_a_refresh_token(monkeypatch):
    client = make_client()
    tokens = authorize_fully(client, monkeypatch)
    assert tokens["refresh_token"]
    assert tokens["refresh_token"] != tokens["access_token"]
    assert oauth.verify_session_token(make_config(), tokens["access_token"]) == "kurtcagle"


def test_token_responses_are_not_cacheable(monkeypatch):
    client = make_client()
    tokens = authorize_fully(client, monkeypatch)
    resp = refresh(client, tokens["refresh_token"], tokens["client_id"])
    assert resp.headers["cache-control"] == "no-store"


def test_code_grant_rejects_a_client_id_other_than_the_one_it_was_issued_to(monkeypatch):
    client = make_client()
    other = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
    from tests.test_github_oauth import pkce_pair  # noqa: PLC0415

    async def fake_exchange(config, code):
        return "t"

    async def fake_login(token):
        return "kurtcagle"

    monkeypatch.setattr(oauth, "_exchange_github_code", fake_exchange)
    monkeypatch.setattr(oauth, "_fetch_github_login", fake_login)
    owner = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
    verifier, challenge = pkce_pair()
    auth = client.get(
        "/authorize",
        params={
            "response_type": "code", "client_id": owner, "redirect_uri": REDIRECT, "state": "s",
            "code_challenge": challenge, "code_challenge_method": "S256",
        },
    )
    req_id = parse_qs(urlparse(auth.headers["location"]).query)["state"][0]
    cb = client.get("/callback/github", params={"code": "c", "state": req_id})
    code = parse_qs(urlparse(cb.headers["location"]).query)["code"][0]
    resp = client.post(
        "/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
              "code_verifier": verifier, "client_id": other},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


# --- metadata -------------------------------------------------------------------


def test_metadata_and_registration_advertise_refresh_token():
    client = make_client()
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert "refresh_token" in meta["grant_types_supported"]
    assert "authorization_code" in meta["grant_types_supported"]
    reg = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()
    assert "refresh_token" in reg["grant_types"]


# --- rotation -------------------------------------------------------------------


def test_refresh_rotates_and_the_old_token_then_fails(monkeypatch):
    client = make_client()
    first = authorize_fully(client, monkeypatch)
    resp = refresh(client, first["refresh_token"], first["client_id"])
    assert resp.status_code == 200
    second = resp.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert oauth.verify_session_token(make_config(), second["access_token"]) == "kurtcagle"
    assert second["expires_in"] == 3600

    replay = refresh(client, first["refresh_token"], first["client_id"])
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_the_chain_keeps_working_across_several_rotations(monkeypatch):
    client = make_client()
    tokens = authorize_fully(client, monkeypatch)
    for _ in range(4):
        resp = refresh(client, tokens["refresh_token"], tokens["client_id"])
        assert resp.status_code == 200
        tokens = {**resp.json(), "client_id": tokens["client_id"]}


def test_reuse_of_a_used_refresh_token_revokes_the_whole_family(monkeypatch):
    client = make_client()
    first = authorize_fully(client, monkeypatch)
    second = refresh(client, first["refresh_token"], first["client_id"]).json()

    # An attacker (or a buggy client) replays the first token...
    assert refresh(client, first["refresh_token"], first["client_id"]).status_code == 400
    # ...which kills the legitimate newest token too.
    after = refresh(client, second["refresh_token"], first["client_id"])
    assert after.status_code == 400
    assert after.json()["error"] == "invalid_grant"


def test_revoking_one_family_leaves_other_families_alone(monkeypatch):
    client = make_client()
    a = authorize_fully(client, monkeypatch)
    b = authorize_fully(client, monkeypatch, client_id=a["client_id"])
    refresh(client, a["refresh_token"], a["client_id"])
    refresh(client, a["refresh_token"], a["client_id"])  # replay: family A revoked
    assert refresh(client, b["refresh_token"], b["client_id"]).status_code == 200


# --- refusals -------------------------------------------------------------------


def test_refresh_fails_for_a_login_removed_from_the_allowlist(monkeypatch):
    client = make_client()
    tokens = authorize_fully(client, monkeypatch)
    # Same app, allowlist now empty of this login.
    client.app.state.github_oauth_config = make_config(allowed_logins=frozenset({"someone-else"}))
    resp = refresh(client, tokens["refresh_token"], tokens["client_id"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"

    # Re-adding the login does not resurrect the revoked family.
    client.app.state.github_oauth_config = make_config()
    assert refresh(client, tokens["refresh_token"], tokens["client_id"]).status_code == 400


def test_refresh_with_the_wrong_client_id_is_rejected_and_does_not_burn_the_token(monkeypatch):
    client = make_client()
    tokens = authorize_fully(client, monkeypatch)
    other = client.post("/register", json={"redirect_uris": [REDIRECT]}).json()["client_id"]
    resp = refresh(client, tokens["refresh_token"], other)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
    assert refresh(client, tokens["refresh_token"], tokens["client_id"]).status_code == 200


def test_refresh_with_an_unregistered_client_id_is_invalid_client(monkeypatch):
    client = make_client()
    tokens = authorize_fully(client, monkeypatch)
    resp = refresh(client, tokens["refresh_token"], "never-registered")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client"


def test_unknown_refresh_token_is_invalid_grant(monkeypatch):
    client = make_client()
    tokens = authorize_fully(client, monkeypatch)
    resp = refresh(client, "not-a-real-token", tokens["client_id"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_expired_refresh_token_is_invalid_grant(monkeypatch):
    client = make_client(make_config(refresh_ttl_seconds=-1))
    tokens = authorize_fully(client, monkeypatch)
    resp = refresh(client, tokens["refresh_token"], tokens["client_id"])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_missing_parameters_are_invalid_request():
    client = make_client()
    resp = client.post("/token", data={"grant_type": "refresh_token"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


# --- persistence ----------------------------------------------------------------


def test_registration_survives_a_new_state_on_the_same_data_dir(tmp_path):
    first = oauth.OAuthState(tmp_path)
    reg = first.register_client([REDIRECT])
    second = oauth.OAuthState(tmp_path)
    found = second.get_client(reg.client_id)
    assert found is not None
    assert found.redirect_uris == [REDIRECT]


def test_registration_without_a_data_dir_is_lost_with_the_state():
    first = oauth.OAuthState()
    reg = first.register_client([REDIRECT])
    assert first.get_client(reg.client_id) is not None
    assert oauth.OAuthState().get_client(reg.client_id) is None


def test_refresh_token_survives_a_restart_on_the_same_data_dir(tmp_path, monkeypatch):
    client = make_client(data_dir=tmp_path)
    tokens = authorize_fully(client, monkeypatch)
    restarted = make_client(data_dir=tmp_path)
    resp = refresh(restarted, tokens["refresh_token"], tokens["client_id"])
    assert resp.status_code == 200


def test_unknown_client_id_is_still_unknown_with_a_data_dir(tmp_path):
    assert oauth.OAuthState(tmp_path).get_client("nope") is None


def test_database_file_is_private_and_holds_no_raw_refresh_token(tmp_path, monkeypatch):
    client = make_client(data_dir=tmp_path)
    tokens = authorize_fully(client, monkeypatch)
    rotated = refresh(client, tokens["refresh_token"], tokens["client_id"]).json()

    db_path = tmp_path / oauth.DB_FILENAME
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    blob = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    for raw in (tokens["refresh_token"], rotated["refresh_token"]):
        assert raw.encode() not in blob
    conn = sqlite3.connect(db_path)
    hashes = {r[0] for r in conn.execute("SELECT token_hash FROM refresh_tokens")}
    conn.close()
    assert oauth.hash_refresh_token(tokens["refresh_token"]) in hashes
    assert oauth.hash_refresh_token(rotated["refresh_token"]) in hashes


def test_a_new_data_dir_is_created_private(tmp_path):
    target = tmp_path / "nested" / "oauth"
    oauth.OAuthState(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


# --- config ---------------------------------------------------------------------


def _env(monkeypatch, **extra):
    for k, v in {
        "GITHUB_OAUTH_CLIENT_ID": "id", "GITHUB_OAUTH_CLIENT_SECRET": "s",
        "MCP_PUBLIC_URL": "https://x.example", "MCP_JWT_SECRET": "j" * 40,
        "MCP_ALLOWED_GITHUB_LOGINS": "kurtcagle", **extra,
    }.items():
        monkeypatch.setenv(k, v)


def test_config_defaults(monkeypatch):
    _env(monkeypatch)
    monkeypatch.delenv("MCP_REFRESH_TTL_SECONDS", raising=False)
    monkeypatch.delenv("MCP_DATA_DIR", raising=False)
    cfg = oauth.config_from_env()
    assert cfg.refresh_ttl_seconds == 30 * 24 * 3600
    assert cfg.data_dir is None


def test_config_reads_refresh_ttl_and_data_dir(monkeypatch, tmp_path):
    _env(monkeypatch, MCP_REFRESH_TTL_SECONDS="600", MCP_DATA_DIR=str(tmp_path))
    cfg = oauth.config_from_env()
    assert cfg.refresh_ttl_seconds == 600
    assert cfg.data_dir == str(tmp_path)


def test_config_rejects_a_non_positive_refresh_ttl(monkeypatch):
    _env(monkeypatch, MCP_REFRESH_TTL_SECONDS="0")
    with pytest.raises(SystemExit):
        oauth.config_from_env()
