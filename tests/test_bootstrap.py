"""Tests for holonbridge/bootstrap.py -- the AnimusDep chicken-and-egg
bypass, plus (CHANGED 2026-09-18) its comma-separated ``--dataset`` list.

No test file existed for this module before now. Covers:

- ``_parse_datasets`` -- the comma-splitting itself, pure and synchronous.
- ``person_exists``/``ensure_admin_person`` -- the actual bypass logic,
  against a fake ``FusekiClient`` rather than a live Fuseki (same
  dependency-injection shape every other test in this suite uses).
- ``_bootstrap_one`` -- one dataset's worth of the new batch loop,
  including its failure isolation (a connect-time error on one dataset
  reported and skipped, not raised past the caller).

Deliberately does NOT exercise ``_run``/``main`` end to end: both call
``get_settings()`` directly, which is ``@lru_cache``-wrapped process-global
state built from real environment variables -- exactly the kind of thing
worth keeping out of a unit test. ``_bootstrap_one`` already takes
``settings`` as an explicit argument for this reason, so the batch-loop
behaviour ``_run`` adds on top (aggregating several ``_bootstrap_one``
results, computing the overall exit code) is exercised directly against
that function instead, without ever touching the real environment or a
real ``~/.holonbridge/config.json``.
"""

from __future__ import annotations

import unittest

from holonbridge.bootstrap import (
    _bootstrap_one,
    _parse_datasets,
    ensure_admin_person,
    person_exists,
)
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiError

HOLON = "https://w3id.org/holon/"


class ParseDatasetsTests(unittest.TestCase):
    def test_none_means_the_banks_own_default(self) -> None:
        self.assertEqual(_parse_datasets(None), [None])

    def test_single_name_is_a_one_item_list(self) -> None:
        self.assertEqual(_parse_datasets("hcg"), ["hcg"])

    def test_comma_separated_splits_into_several(self) -> None:
        self.assertEqual(
            _parse_datasets("hcg,crm,datavid,ggsc"),
            ["hcg", "crm", "datavid", "ggsc"],
        )

    def test_whitespace_around_names_is_trimmed(self) -> None:
        self.assertEqual(_parse_datasets("hcg, crm , datavid"), ["hcg", "crm", "datavid"])

    def test_stray_commas_do_not_produce_empty_names(self) -> None:
        self.assertEqual(_parse_datasets("hcg,,crm,"), ["hcg", "crm"])

    def test_an_all_comma_value_falls_back_to_the_default(self) -> None:
        # Degenerate input (",", ",,,") -- nothing real survives the
        # filter, so this behaves like --dataset was never passed at all,
        # rather than silently bootstrapping zero datasets.
        self.assertEqual(_parse_datasets(",,,"), [None])


# --- fake FusekiClient, dependency-injected the same way every other
# test in this suite stubs the backend -------------------------------------


class FakeFusekiClient:
    """Tracks, per dataset, which external ids already have a Person --
    just enough state for person_exists/ensure_admin_person to make real
    decisions against, without a live Fuseki. ``unreachable`` simulates a
    connect-time failure (the server down, wrong host) distinct from a
    write-time ``FusekiError`` -- the two failure points ``_bootstrap_one``
    handles differently (see its own docstring).
    """

    def __init__(self) -> None:
        self.existing: dict[str, set[str]] = {}
        self.writes: list[tuple[str, str]] = []
        self.unreachable: set[str] = set()

    async def select(self, conn: Conn, query: str) -> dict:
        if conn.dataset in self.unreachable:
            raise FusekiError(502, "connection refused", endpoint=conn.query_endpoint)
        known = self.existing.get(conn.dataset, set())
        found = any(f'"{eid}"' in query for eid in known)
        return {"boolean": found}

    async def post_graph(self, conn: Conn, graph_iri: str, turtle: str) -> None:
        if conn.dataset in self.unreachable:
            raise FusekiError(502, "connection refused", endpoint=graph_iri)
        self.writes.append((conn.dataset, turtle))
        self.existing.setdefault(conn.dataset, set()).add("kurtcagle")

    async def aclose(self) -> None:
        return None


def _conn(dataset: str = "example") -> Conn:
    return Conn(
        base_url="http://localhost:3030",
        dataset=dataset,
        overridden=False,
        bank_name="local",
    )


class PersonExistsAndEnsureAdminPersonTests(unittest.IsolatedAsyncioTestCase):
    async def test_person_exists_false_on_an_empty_dataset(self) -> None:
        client = FakeFusekiClient()
        self.assertFalse(await person_exists(client, _conn(), external_id="kurtcagle"))

    async def test_ensure_admin_person_creates_on_a_fresh_dataset(self) -> None:
        client = FakeFusekiClient()
        result = await ensure_admin_person(
            client,
            _conn(),
            slug="kurt",
            full_name="Kurt Cagle",
            external_id="kurtcagle",
        )
        self.assertEqual(
            result,
            {
                "ok": True,
                "created": True,
                "person": "urn:example:person:kurt",
                "identity": "urn:example:person:kurt:identity:github",
                "role": "urn:example:role:owner",  # the default: the first identity owns the dataset
            },
        )
        self.assertEqual(len(client.writes), 1)

    async def test_an_explicit_admin_role_is_still_honoured(self) -> None:
        client = FakeFusekiClient()
        result = await ensure_admin_person(
            client, _conn(), slug="kurt", full_name="Kurt Cagle", external_id="kurtcagle", role="admin"
        )
        self.assertEqual(result["role"], "urn:example:role:admin")

    async def test_ensure_admin_person_is_a_no_op_when_identity_already_exists(self) -> None:
        client = FakeFusekiClient()
        client.existing["example"] = {"kurtcagle"}
        result = await ensure_admin_person(
            client,
            _conn(),
            slug="kurt",
            full_name="Kurt Cagle",
            external_id="kurtcagle",
        )
        self.assertEqual(
            result, {"ok": True, "created": False, "reason": "external_id already present"}
        )
        self.assertEqual(client.writes, [])  # never touched -- see module docstring

    async def test_no_role_when_role_is_none(self) -> None:
        client = FakeFusekiClient()
        result = await ensure_admin_person(
            client,
            _conn(),
            slug="jane",
            full_name="Jane Doe",
            external_id="jane-doe",
            role=None,
        )
        self.assertIsNone(result["role"])
        self.assertNotIn("holon:hasRole", client.writes[0][1])


class BootstrapOneTests(unittest.IsolatedAsyncioTestCase):
    """Direct coverage of the new per-dataset batch unit, including the
    failure-isolation the module docstring describes: one dataset in a
    ``--dataset a,b,c`` run failing must not raise past the caller, only
    report and return False, so the batch loop can continue to the rest.
    """

    def setUp(self) -> None:
        from pathlib import Path

        # A path that doesn't exist -- BankStore then has nothing to read
        # and synthesises just the "local" bank from Settings' own
        # fuseki_url/fuseki_dataset defaults, same as a fresh install with
        # no ~/.holonbridge/config.json yet.
        self.settings = Settings(config_path=Path("/nonexistent/holonbridge-test-config.json"))

        class _Args:
            slug = "kurt"
            name = "Kurt Cagle"
            external_id = "kurtcagle"
            external_id_type = "GitHubIdentity"
            role = "admin"

        self.args = _Args()

    async def test_succeeds_against_a_fresh_dataset(self) -> None:
        client = FakeFusekiClient()
        ok = await _bootstrap_one(
            client, bank_name="local", dataset="hcg", settings=self.settings, args=self.args
        )
        self.assertTrue(ok)
        self.assertEqual(client.writes[0][0], "hcg")

    async def test_unknown_bank_is_reported_and_returns_false_not_raised(self) -> None:
        client = FakeFusekiClient()
        ok = await _bootstrap_one(
            client,
            bank_name="does-not-exist",
            dataset="hcg",
            settings=self.settings,
            args=self.args,
        )
        self.assertFalse(ok)
        self.assertEqual(client.writes, [])

    async def test_an_unreachable_dataset_is_reported_and_returns_false_not_raised(self) -> None:
        client = FakeFusekiClient()
        client.unreachable.add("crm")
        ok = await _bootstrap_one(
            client, bank_name="local", dataset="crm", settings=self.settings, args=self.args
        )
        self.assertFalse(ok)

    async def test_a_batch_isolates_one_failing_dataset_from_the_rest(self) -> None:
        # The shape _run's own loop produces: one FakeFusekiClient shared
        # across the whole batch (matching _run's single client, reused
        # via one aclose() at the end), one dataset down, the other two up.
        client = FakeFusekiClient()
        client.unreachable.add("crm")

        results = [
            await _bootstrap_one(
                client, bank_name="local", dataset=name, settings=self.settings, args=self.args
            )
            for name in ("hcg", "crm", "datavid")
        ]

        self.assertEqual(results, [True, False, True])
        self.assertFalse(all(results))  # the batch-level exit code _run computes
        self.assertEqual({d for d, _ in client.writes}, {"hcg", "datavid"})


if __name__ == "__main__":
    unittest.main()
