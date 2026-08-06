"""Tests for the opt-in urn:{bank}:{dataset}:{role} graph naming convention.

The bank rename (2209fb9) changed which server and dataset a bank points
at, but never touched how graph IRIs within that dataset are built --
Conn.graph() went on returning urn:{dataset}:{role} regardless of which
bank was active, discovered live against `worldtest` on 2026-07-29 after
its graphs had already been hand-rewritten to urn:holongraph:worldtest:*.

The fix here is deliberately conservative: a dataset only gets the bank
segment if it appears in that bank's own `bankScopedDatasets` list. The
one property every test in this file ultimately serves is the same
promise made to Kurt before this shipped -- a bank with an empty (or
absent) `bankScopedDatasets` list must be byte-for-byte identical to the
pre-fix behaviour, for every dataset, always. Getting that wrong here
would have meant silently breaking `chloe`, `bridgerton`, and `storme` the
moment this shipped, since none of them have actually been migrated.
"""

from __future__ import annotations

import json

import pytest

from holonbridge.config import Bank, BankStore, Settings
from holonbridge.conn import Conn, resolve_conn


# --- Bank.from_json / as_public ------------------------------------------------


def test_bank_scoped_datasets_defaults_to_empty():
    bank = Bank.from_json("local", {"url": "http://x", "dataset": "ds"})
    assert bank.bank_scoped_datasets == frozenset()


def test_bank_scoped_datasets_is_read_from_config():
    bank = Bank.from_json(
        "holongraph",
        {"url": "http://x", "dataset": "ds", "bankScopedDatasets": ["worldtest"]},
    )
    assert bank.bank_scoped_datasets == frozenset({"worldtest"})


def test_bank_scoped_datasets_rejects_a_non_list():
    """A single string is a natural typo (bankScopedDatasets: "worldtest")
    that would otherwise iterate character-by-character and silently scope
    every one-letter "dataset" -- fail loudly instead."""
    with pytest.raises(ValueError, match="bankScopedDatasets must be a list"):
        Bank.from_json(
            "holongraph",
            {"url": "http://x", "dataset": "ds", "bankScopedDatasets": "worldtest"},
        )


def test_as_public_reports_bank_scoped_datasets_sorted():
    bank = Bank.from_json(
        "holongraph",
        {"url": "http://x", "dataset": "ds", "bankScopedDatasets": ["storme", "worldtest"]},
    )
    assert bank.as_public()["bankScopedDatasets"] == ["storme", "worldtest"]


def test_as_public_omits_no_secrets_alongside_the_new_field():
    bank = Bank.from_json(
        "holongraph",
        {
            "url": "http://x",
            "dataset": "ds",
            "bankScopedDatasets": ["worldtest"],
            "auth": {"type": "bearer", "token": "SECRET"},
        },
    )
    assert "SECRET" not in json.dumps(bank.as_public())


# --- Conn.graph() / Conn.scoped() ----------------------------------------------


def _conn(dataset: str, bank_scoped_datasets: frozenset[str] = frozenset()) -> Conn:
    return Conn(
        base_url="http://x",
        dataset=dataset,
        overridden=False,
        bank_name="holongraph",
        bank_scoped_datasets=bank_scoped_datasets,
    )


def test_an_unscoped_dataset_gets_the_old_two_segment_name():
    conn = _conn("chloe")
    assert conn.graph("holons") == "urn:chloe:holons"


def test_a_scoped_dataset_gets_the_three_segment_name():
    conn = _conn("worldtest", frozenset({"worldtest"}))
    assert conn.graph("holons") == "urn:holongraph:worldtest:holons"


def test_scoping_one_dataset_does_not_scope_another_on_the_same_bank():
    """The regression this whole feature exists to prevent: adding
    worldtest to a bank's list must never touch chloe, bridgerton, or
    storme, even though they share the same bank_name."""
    conn = _conn("chloe", frozenset({"worldtest"}))
    assert conn.graph("holons") == "urn:chloe:holons"


def test_scoped_helper_follows_the_same_rule():
    unscoped = _conn("bridgerton")
    scoped = _conn("worldtest", frozenset({"worldtest"}))
    assert unscoped.scoped("pipelines", "my-pipeline") == "urn:bridgerton:pipeline:my-pipeline"
    assert scoped.scoped("pipelines", "my-pipeline") == "urn:holongraph:worldtest:pipeline:my-pipeline"


def test_an_unknown_role_still_raises_regardless_of_scoping():
    conn = _conn("worldtest", frozenset({"worldtest"}))
    with pytest.raises(ValueError, match="unknown graph role"):
        conn.graph("not-a-real-role")


def test_describe_reflects_scoping_through_graph():
    """describe() (what get_endpoint reports) builds every entry through
    graph() -- this is the exact field that surfaced the original bug live,
    so it gets its own explicit check rather than relying on graph()'s
    tests to imply it."""
    conn = _conn("worldtest", frozenset({"worldtest"}))
    graphs = conn.describe()["graphs"]
    assert graphs["holons"] == "urn:holongraph:worldtest:holons"
    assert graphs["shacl"] == "urn:holongraph:worldtest:shacl"


# --- resolve_conn(): the actual wiring a request goes through -----------------


def _store(tmp_path, payload) -> BankStore:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return BankStore(Settings(config_path=path))


def test_resolve_conn_carries_bank_scoped_datasets_through(tmp_path):
    store = _store(
        tmp_path,
        {
            "default": "holongraph",
            "banks": {
                "holongraph": {
                    "url": "http://localhost:3030",
                    "dataset": "ds",
                    "bankScopedDatasets": ["worldtest"],
                }
            },
        },
    )
    settings = Settings(config_path=tmp_path / "config.json")

    scoped_conn = resolve_conn(settings=settings, banks=store, bank_name="holongraph", override="worldtest")
    assert scoped_conn.graph("holons") == "urn:holongraph:worldtest:holons"

    unscoped_conn = resolve_conn(settings=settings, banks=store, bank_name="holongraph", override="chloe")
    assert unscoped_conn.graph("holons") == "urn:chloe:holons"


def test_a_bank_with_no_bankscoped_key_is_a_pure_no_op(tmp_path):
    """The exact promise made before this shipped: a bank that never
    mentions bankScopedDatasets must resolve identically to the pre-fix
    bridge, for every dataset."""
    store = _store(
        tmp_path,
        {
            "default": "local",
            "banks": {"local": {"url": "http://localhost:3030", "dataset": "ds"}},
        },
    )
    settings = Settings(config_path=tmp_path / "config.json")

    for dataset in ("chloe", "bridgerton", "storme", "worldtest", "ds", "admin"):
        conn = resolve_conn(settings=settings, banks=store, bank_name="local", override=dataset)
        assert conn.graph("holons") == f"urn:{dataset}:holons"
        assert conn.graph("shacl") == f"urn:{dataset}:shacl"
