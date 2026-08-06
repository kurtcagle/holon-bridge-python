"""Tests for the shared .env loader.

Each test resets the module-level "already loaded" guard and clears any env
vars the loader might touch, since the whole point of that guard is to make
loading a no-op on the second call within one process — which is exactly
what would make these tests interfere with each other if left alone.
"""

from __future__ import annotations

import os

import pytest

from holonbridge import envfile


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    envfile.reset_for_testing()
    monkeypatch.delenv("HOLONBRIDGE_ENV_FILE", raising=False)
    monkeypatch.delenv("SOME_TEST_VAR", raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    envfile.reset_for_testing()


def write_env(path, **pairs) -> None:
    path.write_text("\n".join(f"{k}={v}" for k, v in pairs.items()) + "\n")


def test_no_file_anywhere_is_silent(tmp_path):
    assert envfile.load_shared_env() is None


def test_cwd_dotenv_is_found_and_loaded(tmp_path, monkeypatch):
    write_env(tmp_path / ".env", SOME_TEST_VAR="from-cwd")
    used = envfile.load_shared_env()
    assert used == tmp_path / ".env"
    assert os.getenv("SOME_TEST_VAR") == "from-cwd"


def test_explicit_path_overrides_cwd_discovery(tmp_path, monkeypatch):
    write_env(tmp_path / ".env", SOME_TEST_VAR="from-cwd")
    elsewhere = tmp_path / "elsewhere.env"
    write_env(elsewhere, SOME_TEST_VAR="from-explicit-path")
    monkeypatch.setenv("HOLONBRIDGE_ENV_FILE", str(elsewhere))

    used = envfile.load_shared_env()
    assert used == elsewhere
    assert os.getenv("SOME_TEST_VAR") == "from-explicit-path"


def test_explicit_path_works_regardless_of_cwd(tmp_path, monkeypatch):
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    target = other_dir / "shared.env"
    write_env(target, SOME_TEST_VAR="found-me")
    monkeypatch.setenv("HOLONBRIDGE_ENV_FILE", str(target))
    # cwd (tmp_path, set by the autouse fixture) has no .env of its own —
    # the explicit path must not depend on it.

    assert envfile.load_shared_env() == target
    assert os.getenv("SOME_TEST_VAR") == "found-me"


def test_a_missing_explicit_path_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLONBRIDGE_ENV_FILE", str(tmp_path / "nope.env"))
    with pytest.raises(SystemExit, match="does not exist"):
        envfile.load_shared_env()


def test_a_real_environment_variable_beats_the_file(tmp_path, monkeypatch):
    write_env(tmp_path / ".env", SOME_TEST_VAR="from-file")
    monkeypatch.setenv("SOME_TEST_VAR", "from-real-shell")

    envfile.load_shared_env()
    assert os.getenv("SOME_TEST_VAR") == "from-real-shell"


def test_loading_is_idempotent_within_one_process(tmp_path, monkeypatch):
    write_env(tmp_path / ".env", SOME_TEST_VAR="from-file")
    first = envfile.load_shared_env()
    assert first is not None

    # A second call is a no-op — even if the file changes underneath it —
    # which is what lets both entry points call it unconditionally without
    # re-reading the file on every access.
    write_env(tmp_path / ".env", SOME_TEST_VAR="changed-after-first-load")
    second = envfile.load_shared_env()
    assert second is None
    assert os.getenv("SOME_TEST_VAR") == "from-file"


def test_both_entry_points_see_the_same_shared_file(tmp_path, monkeypatch):
    """The property the user actually asked for: one file, both processes."""
    write_env(tmp_path / ".env", BEARER_TOKEN="shared-value")

    envfile.load_shared_env()
    assert os.getenv("BEARER_TOKEN") == "shared-value"

    # holonbridge_mcp/server.py reads BEARER_TOKEN as BEARER at import time;
    # re-import is not meaningful here (Python caches modules), so this
    # exercises the same os.getenv() call that module makes, proving the
    # value a fresh process would see is the same either way.
    import importlib

    import holonbridge_mcp.server as mcp_server

    importlib.reload(mcp_server)
    assert mcp_server.BEARER == "shared-value"
