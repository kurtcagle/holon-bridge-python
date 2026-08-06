"""Tests for the bank override mechanism and the legacy config-key fallback.

A *bank* is a named backend connection — a server URL, a default dataset, and
optional credentials — previously called a "profile". Two distinct things are
covered here:

1. The MCP-layer bank override, which mirrors the dataset override exactly:
   persisted to disk so a process restart restores the choice rather than
   silently reverting. The failure mode is the same one the dataset override
   was written for, and is worse for banks: a call against the wrong bank
   still succeeds and still returns data, it is simply the wrong store.

2. ``BankStore`` reading ``config.json``, including the backward-compatible
   fallback to the old ``profiles`` key. Existing configs must keep working
   across the rename without being edited.

The resolution logic is a pure function of the environment and the filesystem,
so it is tested directly with the state-file path monkeypatched to ``tmp_path``
rather than through a full module reload — the same approach as
``test_dataset_persistence``.
"""

from __future__ import annotations

import json

import holonbridge_mcp.server as server
from holonbridge.config import BankStore, Settings


# --- MCP bank override --------------------------------------------------------


def _use_tmp_state_file(monkeypatch, tmp_path):
    path = tmp_path / "mcp-bank-override"
    monkeypatch.setattr(server, "_BANK_STATE_FILE", path)
    monkeypatch.delenv("HOLONBRIDGE_BANK", raising=False)
    return path


def test_with_nothing_set_the_bank_override_is_empty(monkeypatch, tmp_path):
    _use_tmp_state_file(monkeypatch, tmp_path)
    bank, source = server._resolve_initial_bank()
    assert bank == ""
    assert source == "none"


def test_a_persisted_bank_is_restored(monkeypatch, tmp_path):
    """The core behaviour: what a process restart does instead of reverting."""
    path = _use_tmp_state_file(monkeypatch, tmp_path)
    path.write_text("ggsc", encoding="utf-8")

    bank, source = server._resolve_initial_bank()
    assert bank == "ggsc"
    assert source == "persisted"


def test_an_explicit_env_var_beats_a_persisted_bank(monkeypatch, tmp_path):
    """A real env var is a deliberate pin and must win over a stale file.

    This is the precedence that makes persistence safe to add: an operator who
    sets HOLONBRIDGE_BANK gets what they asked for, not what some earlier
    session happened to leave behind.
    """
    path = _use_tmp_state_file(monkeypatch, tmp_path)
    path.write_text("ggsc", encoding="utf-8")
    monkeypatch.setenv("HOLONBRIDGE_BANK", "bridgerton")

    bank, source = server._resolve_initial_bank()
    assert bank == "bridgerton"
    assert source == "env"


def test_persisting_then_clearing_removes_the_file(monkeypatch, tmp_path):
    """Clearing must not leave a value to be restored on the next restart."""
    path = _use_tmp_state_file(monkeypatch, tmp_path)

    server._persist_bank("ggsc")
    assert path.read_text(encoding="utf-8") == "ggsc"

    server._persist_bank("")
    assert not path.exists()
    assert server._resolve_initial_bank() == ("", "none")


def test_a_persist_failure_does_not_raise(monkeypatch, tmp_path):
    """Best-effort by design: a disk problem must not break the live switch."""
    path = tmp_path / "unwritable" / "mcp-bank-override"
    monkeypatch.setattr(server, "_BANK_STATE_FILE", path)

    def boom(*args, **kwargs):
        raise OSError("no")

    monkeypatch.setattr(type(path), "mkdir", boom)
    server._persist_bank("ggsc")  # must not raise
    assert not path.exists()


# --- ?bank= parameter merging -------------------------------------------------


def test_no_override_leaves_params_untouched(monkeypatch):
    monkeypatch.setattr(server, "_bank_override", "")
    assert server._with_bank(None) is None
    assert server._with_bank({"limit": 5}) == {"limit": 5}


def test_the_override_is_applied_as_a_query_param(monkeypatch):
    monkeypatch.setattr(server, "_bank_override", "ggsc")
    assert server._with_bank(None) == {"bank": "ggsc"}


def test_caller_params_are_preserved_alongside_the_bank(monkeypatch):
    """Regression guard: replacing rather than merging would silently drop
    every other query parameter on the call."""
    monkeypatch.setattr(server, "_bank_override", "ggsc")
    assert server._with_bank({"limit": 5}) == {"limit": 5, "bank": "ggsc"}


def test_an_explicit_caller_bank_wins_over_the_session_default(monkeypatch):
    monkeypatch.setattr(server, "_bank_override", "ggsc")
    assert server._with_bank({"bank": "chosen"}) == {"bank": "chosen"}


def test_bank_and_dataset_overrides_are_independent(monkeypatch):
    """They travel by different channels — bank as a query param, dataset as a
    header — and neither switch may disturb the other."""
    monkeypatch.setattr(server, "_bank_override", "ggsc")
    monkeypatch.setattr(server, "_dataset_override", "storme")

    assert server._with_bank(None) == {"bank": "ggsc"}
    assert server._headers()["X-Dataset-Override"] == "storme"


# --- BankStore config loading -------------------------------------------------


def _store(tmp_path, payload) -> BankStore:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return BankStore(Settings(config_path=path))


def test_banks_key_is_read(tmp_path):
    store = _store(
        tmp_path,
        {"default": "ggsc", "banks": {"ggsc": {"url": "https://x", "dataset": "ggsc"}}},
    )
    assert store.active.name == "ggsc"
    assert store.get("ggsc").dataset == "ggsc"


def test_the_legacy_profiles_key_still_works(tmp_path, caplog):
    """Existing configs must survive the rename un-edited — and say so once."""
    store = _store(
        tmp_path,
        {"default": "ggsc", "profiles": {"ggsc": {"url": "https://x", "dataset": "ggsc"}}},
    )
    assert store.active.name == "ggsc"
    assert any("legacy 'profiles' key" in r.getMessage() for r in caplog.records)


def test_banks_wins_when_both_keys_are_present(tmp_path):
    """An operator mid-migration should get the new key, not a merge or a
    coin toss."""
    store = _store(
        tmp_path,
        {
            "default": "new",
            "banks": {"new": {"url": "https://new", "dataset": "n"}},
            "profiles": {"old": {"url": "https://old", "dataset": "o"}},
        },
    )
    assert store.active.name == "new"
    assert [b["name"] for b in store.list()] == ["local", "new"]


def test_a_local_bank_always_exists(tmp_path):
    """The bridge must start cleanly with no config file present."""
    store = BankStore(Settings(config_path=tmp_path / "absent.json"))
    assert store.active.name == "local"


def test_the_public_view_never_leaks_a_token(tmp_path):
    store = _store(
        tmp_path,
        {
            "default": "ggsc",
            "banks": {
                "ggsc": {
                    "url": "https://x",
                    "dataset": "ggsc",
                    "auth": {"type": "bearer", "token": "SECRET"},
                }
            },
        },
    )
    public = store.get("ggsc").as_public()
    assert public["authenticated"] is True
    assert "SECRET" not in json.dumps(public)
