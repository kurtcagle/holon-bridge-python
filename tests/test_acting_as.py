"""Tests for the admin act_as impersonation override (acting_as.py) and
build_animus_as (acl.py), added 2026-09-01. See acting_as.py's module
docstring for why swapping Animus.person alone is sufficient to route a
request through the real (non-bypassed) grant-check code, without a
separate "bypass off" flag.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from holonbridge.acl import build_animus_as
from holonbridge.acting_as import ActingAsStore

HOLON = "https://w3id.org/holon/"
HOLONS_GRAPH = "urn:causalspark:holons"

FIXTURE_TURTLE = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<urn:causalspark:person:ctownley-cs> a holon:Person ; rdfs:label "Caroline Townley" ;
    holon:memberOfTeam <urn:causalspark:team:founders> .
"""


def make_query_fn(graph):
    async def query_fn(sparql: str) -> dict:
        result = graph.query(sparql, initNs={}, initBindings={})
        if result.type == "ASK":
            return {"boolean": bool(result.askAnswer)}
        bindings = []
        for row in result:
            binding = {}
            for var in result.vars:
                val = row[var]
                if val is None:
                    continue
                binding[str(var)] = {
                    "type": "uri" if str(val).startswith(("http", "urn")) else "literal",
                    "value": str(val),
                }
            bindings.append(binding)
        return {"results": {"bindings": bindings}}

    return query_fn


class BuildAnimusAsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from rdflib import Dataset, URIRef

        ds = Dataset()
        ds.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
        self.query_fn = make_query_fn(ds)

    async def test_resolves_label_and_teams_for_known_person(self) -> None:
        animus = await build_animus_as(
            self.query_fn, HOLONS_GRAPH, person="urn:causalspark:person:ctownley-cs"
        )
        self.assertEqual(animus.person, "urn:causalspark:person:ctownley-cs")
        self.assertEqual(animus.person_label, "Caroline Townley")
        self.assertIn("urn:causalspark:team:founders", animus.teams)

    async def test_marks_external_id_as_impersonation_not_a_real_login(self) -> None:
        animus = await build_animus_as(
            self.query_fn, HOLONS_GRAPH, person="urn:causalspark:person:ctownley-cs"
        )
        self.assertEqual(animus.external_id_type, "ActingAs")
        self.assertIn("urn:causalspark:person:ctownley-cs", animus.external_id)

    async def test_unknown_person_gets_no_label_not_an_error(self) -> None:
        # No holon:Person exists at this IRI in the fixture -- build_animus_as
        # itself doesn't check existence (the /admin/act-as route does,
        # once, before ever calling this); it should degrade to no label,
        # not raise.
        animus = await build_animus_as(
            self.query_fn, HOLONS_GRAPH, person="urn:causalspark:person:nobody"
        )
        self.assertEqual(animus.person, "urn:causalspark:person:nobody")
        self.assertIsNone(animus.person_label)
        self.assertEqual(animus.teams, frozenset())


class ActingAsStoreTests(unittest.TestCase):
    def test_set_then_get_returns_target(self) -> None:
        store = ActingAsStore()
        store.set(
            real_person="urn:causalspark:person:kurt",
            target_person="urn:causalspark:person:ctownley-cs",
        )
        self.assertEqual(
            store.get(real_person="urn:causalspark:person:kurt"),
            "urn:causalspark:person:ctownley-cs",
        )

    def test_get_with_no_override_returns_none(self) -> None:
        store = ActingAsStore()
        self.assertIsNone(store.get(real_person="urn:causalspark:person:kurt"))

    def test_clear_removes_the_override(self) -> None:
        store = ActingAsStore()
        store.set(
            real_person="urn:causalspark:person:kurt",
            target_person="urn:causalspark:person:ctownley-cs",
        )
        store.clear(real_person="urn:causalspark:person:kurt")
        self.assertIsNone(store.get(real_person="urn:causalspark:person:kurt"))

    def test_clear_with_no_override_is_a_safe_no_op(self) -> None:
        store = ActingAsStore()
        store.clear(real_person="urn:causalspark:person:kurt")  # must not raise

    def test_two_real_people_do_not_collide(self) -> None:
        store = ActingAsStore()
        store.set(
            real_person="urn:causalspark:person:kurt",
            target_person="urn:causalspark:person:ctownley-cs",
        )
        store.set(
            real_person="urn:causalspark:person:thomas",
            target_person="urn:causalspark:person:pawel",
        )
        self.assertEqual(
            store.get(real_person="urn:causalspark:person:kurt"),
            "urn:causalspark:person:ctownley-cs",
        )
        self.assertEqual(
            store.get(real_person="urn:causalspark:person:thomas"),
            "urn:causalspark:person:pawel",
        )

    def test_entry_past_ttl_expires_and_is_dropped(self) -> None:
        store = ActingAsStore(ttl_seconds=1)
        store.set(
            real_person="urn:causalspark:person:kurt",
            target_person="urn:causalspark:person:ctownley-cs",
        )
        # Reach into the entry and backdate it, rather than sleeping in a
        # test -- same shape as how other state-file tests in this suite
        # avoid real delays.
        entry = store._by_real_person["urn:causalspark:person:kurt"]
        store._by_real_person["urn:causalspark:person:kurt"] = type(entry)(
            target_person=entry.target_person,
            since=datetime.now(timezone.utc) - timedelta(seconds=2),
        )
        self.assertIsNone(store.get(real_person="urn:causalspark:person:kurt"))

    def test_set_renews_the_ttl_clock(self) -> None:
        store = ActingAsStore(ttl_seconds=1)
        store.set(
            real_person="urn:causalspark:person:kurt",
            target_person="urn:causalspark:person:ctownley-cs",
        )
        entry = store._by_real_person["urn:causalspark:person:kurt"]
        store._by_real_person["urn:causalspark:person:kurt"] = type(entry)(
            target_person=entry.target_person,
            since=datetime.now(timezone.utc) - timedelta(seconds=2),
        )
        # Renew before it's read again.
        store.set(
            real_person="urn:causalspark:person:kurt",
            target_person="urn:causalspark:person:ctownley-cs",
        )
        self.assertEqual(
            store.get(real_person="urn:causalspark:person:kurt"),
            "urn:causalspark:person:ctownley-cs",
        )


if __name__ == "__main__":
    unittest.main()
