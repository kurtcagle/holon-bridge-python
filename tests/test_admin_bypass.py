"""Tests for the admin-role bypass added 2026-08-15: a person holding the
reserved admin Role short-circuits check_read/check_write/check_invoke
unconditionally, including past a deniedTo override -- deliberately not
another entry in the ordinary most-specific-wins precedence chain.
"""

from __future__ import annotations

import unittest

from holonbridge.acl import check_invoke, check_read, check_write, is_admin

HOLON = "https://w3id.org/holon/"
HOLONS_GRAPH = "urn:causalspark:holons"

FIXTURE_TURTLE = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<urn:causalspark:persona:aimee> a holon:Persona ; rdfs:label "Aimee" .
<urn:causalspark:query:some-query> a holon:NamedQuery ; rdfs:label "some-query" .

<urn:causalspark:role:admin> a holon:Role ; rdfs:label "Admin" .

<urn:causalspark:person:kurt> a holon:Person ; rdfs:label "Kurt Cagle" ;
    holon:hasRole <urn:causalspark:role:admin> .

<urn:causalspark:person:nobody> a holon:Person ; rdfs:label "Nobody" .

# Aimee explicitly denies kurt individually -- the admin bypass must win
# over this anyway, since admin sits outside the ordinary precedence chain.
<urn:causalspark:persona:aimee> holon:deniedTo <urn:causalspark:person:kurt> .
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


class AdminBypassTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from rdflib import Dataset, URIRef

        ds = Dataset()
        ds.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
        self.query_fn = make_query_fn(ds)

    async def test_is_admin_true_for_admin_role_holder(self) -> None:
        self.assertTrue(
            await is_admin(self.query_fn, HOLONS_GRAPH, person="urn:causalspark:person:kurt")
        )

    async def test_is_admin_false_for_everyone_else(self) -> None:
        self.assertFalse(
            await is_admin(self.query_fn, HOLONS_GRAPH, person="urn:causalspark:person:nobody")
        )

    async def test_admin_bypasses_deniedTo_on_read(self) -> None:
        # Kurt has no ReadGrant for aimee at all in this fixture, AND aimee
        # explicitly denies him -- both would ordinarily refuse this. Admin
        # must still win.
        decision = await check_read(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            persona="urn:causalspark:persona:aimee",
        )
        self.assertTrue(decision.allowed)
        self.assertIn("admin", decision.reason)

    async def test_admin_bypasses_invoke_with_no_grant(self) -> None:
        decision = await check_invoke(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            named_query="urn:causalspark:query:some-query",
        )
        self.assertTrue(decision.allowed)

    async def test_admin_bypasses_write_with_no_grantsWrite(self) -> None:
        decision = await check_write(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            target="urn:causalspark:persona:aimee",
        )
        self.assertTrue(decision.allowed)

    async def test_non_admin_still_refused_on_all_three(self) -> None:
        # Regression guard: the bypass must not have loosened anything for
        # a person who doesn't hold the admin role.
        read = await check_read(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:nobody",
            persona="urn:causalspark:persona:aimee",
        )
        invoke = await check_invoke(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:nobody",
            named_query="urn:causalspark:query:some-query",
        )
        write = await check_write(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:nobody",
            target="urn:causalspark:persona:aimee",
        )
        self.assertFalse(read.allowed)
        self.assertFalse(invoke.allowed)
        self.assertFalse(write.allowed)


class AdminRoleIriTests(unittest.TestCase):
    def test_derives_from_holons_graph(self) -> None:
        from holonbridge.acl import _admin_role

        self.assertEqual(_admin_role("urn:causalspark:holons"), "urn:causalspark:role:admin")

    def test_bank_scoped_variant(self) -> None:
        from holonbridge.acl import _admin_role

        self.assertEqual(
            _admin_role("urn:local:causalspark:holons"), "urn:local:causalspark:role:admin"
        )

    def test_rejects_non_holons_graph(self) -> None:
        from holonbridge.acl import _admin_role

        with self.assertRaises(ValueError):
            _admin_role("urn:causalspark:ontology")


if __name__ == "__main__":
    unittest.main()
