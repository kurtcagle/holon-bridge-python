"""Tests for the persisted dataset-override mechanism in holonbridge_mcp.

Motivated by a real, repeated failure: a chosen dataset survives fine within
one session, but an MCP process restart used to fall back silently to
whatever ``HOLONBRIDGE_DATASET`` (or nothing) said at import time — so a
write immediately after a restart could land in the wrong dataset with no
error and no warning. These tests cover the pure logic
(``_resolve_initial_dataset``, ``_persist_dataset``, ``_load_persisted_dataset``)
directly, monkeypatching the module's state-file path to an isolated
``tmp_path`` rather than exercising this through a full module reload —
the resolution logic is a pure function of the environment and the
filesystem, and testing it that way is both simpler and more direct than
reimporting the module for every case.
"""

from __future__ import annotations

import holonbridge_mcp.server as server


def _use_tmp_state_file(monkeypatch, tmp_path):
    path = tmp_path / "mcp-dataset-override"
    monkeypatch.setattr(server, "_DATASET_STATE_FILE", path)
    monkeypatch.delenv("HOLONBRIDGE_DATASET", raising=False)
    return path


def test_with_nothing_set_the_override_is_empty(monkeypatch, tmp_path):
    _use_tmp_state_file(monkeypatch, tmp_path)
    dataset, source = server._resolve_initial_dataset()
    assert dataset == ""
    assert source == "none"


def test_a_persisted_value_is_restored(monkeypatch, tmp_path):
    """The core fix: this is what a process restart now does instead of
    silently reverting to the environment default.
    """
    path = _use_tmp_state_file(monkeypatch, tmp_path)
    path.write_text("storme", encoding="utf-8")

    dataset, source = server._resolve_initial_dataset()
    assert dataset == "storme"
    assert source == "persisted"


def test_an_explicit_env_var_wins_over_a_persisted_value(monkeypatch, tmp_path):
    """Matches every other precedent in this codebase: a real environment
    variable is a deliberate pin and always wins over a stored default.
    """
    path = _use_tmp_state_file(monkeypatch, tmp_path)
    path.write_text("storme", encoding="utf-8")
    monkeypatch.setenv("HOLONBRIDGE_DATASET", "bridgerton")

    dataset, source = server._resolve_initial_dataset()
    assert dataset == "bridgerton"
    assert source == "env"


def test_persist_then_load_round_trips(monkeypatch, tmp_path):
    _use_tmp_state_file(monkeypatch, tmp_path)
    server._persist_dataset("bridgerton")
    assert server._load_persisted_dataset() == "bridgerton"


def test_persisting_an_empty_name_clears_the_file(monkeypatch, tmp_path):
    path = _use_tmp_state_file(monkeypatch, tmp_path)
    server._persist_dataset("storme")
    assert path.is_file()

    server._persist_dataset("")
    assert not path.exists()
    assert server._load_persisted_dataset() == ""


def test_a_missing_persisted_file_reads_as_empty_not_an_error(monkeypatch, tmp_path):
    _use_tmp_state_file(monkeypatch, tmp_path)
    assert server._load_persisted_dataset() == ""


def test_persisting_creates_parent_directories(monkeypatch, tmp_path):
    """The default location is ~/.holonbridge/mcp-dataset-override, and that
    directory does not necessarily exist yet on a fresh machine.
    """
    nested = tmp_path / "does" / "not" / "exist" / "mcp-dataset-override"
    monkeypatch.setattr(server, "_DATASET_STATE_FILE", nested)

    server._persist_dataset("storme")
    assert nested.is_file()
    assert nested.read_text(encoding="utf-8") == "storme"


def test_a_failed_persist_does_not_raise(monkeypatch, tmp_path):
    """Best-effort: this session's switch must not be undone by a disk
    problem, only the ability to survive the *next* restart.
    """
    # A path whose parent is a file, not a directory, cannot be created.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    unwritable = blocker / "mcp-dataset-override"
    monkeypatch.setattr(server, "_DATASET_STATE_FILE", unwritable)

    server._persist_dataset("storme")  # must not raise
