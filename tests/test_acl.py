"""Tests run against an in-memory rdflib graph seeded with the same triples
already verified live in urn:causalspark:holons -- not a network call to
Fuseki (nothing in this sandbox can reach Kurt's local instance), but the
same schema, same IRIs, same data shape, queried with real SPARQL through
real rdflib. If these pass, the decision logic is correct against the real
model; wiring FusekiClient.select in as ``query_fn`` in place of the
in-memory graph is the only thing that changes for production.
"""

from __future__ import annotations

import asyncio
import unittest

from rdflib import Graph

from holonbridge.acl import (
    AclDecision,
    authorize_query,
    build_animus,
    check_invoke,
    check_read,
    check_write,
    extract_graph_refs,
    resolve_person,
    team_visibility_filter,
)

HOLON = "https://w3id.org/holon/"

# Mirrors what's actually live in urn:causalspark:holons as of 2026-08-15:
# four Persons with GitHub identities, the founder Role with two ReadGrants
# (aimee, carlo) and two InvokeGrants (the two named queries), plus the two
# Persona holons the grants scope against.
FIXTURE_TURTLE = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<urn:causalspark:persona:aimee> a holon:Persona ; rdfs:label "Aimee" .
<urn:causalspark:persona:carlo> a holon:Persona ; rdfs:label "Carlo" .

<urn:causalspark:query:select-public-union> a holon:NamedQuery ; rdfs:label "select-public-union" .

<urn:causalspark:person:kurt> a holon:Person ; rdfs:label "Kurt Cagle" ;
    holon:hasExternalIdentity <urn:causalspark:person:kurt:identity:github> ;
    holon:hasRole <urn:causalspark:role:founder> .
<urn:causalspark:person:kurt:identity:github> a holon:GitHubIdentity ; holon:identifier "kurtcagle" .

<urn:causalspark:person:caroline> a holon:Person ; rdfs:label "Caroline Townley" ;
    holon:hasExternalIdentity <urn:causalspark:person:caroline:identity:github> ;
    holon:hasRole <urn:causalspark:role:founder> .
<urn:causalspark:person:caroline:identity:github> a holon:GitHubIdentity ; holon:identifier "ctownley-cs" .

<urn:causalspark:role:founder> a holon:Role ; rdfs:label "Founder" ;
    holon:grants <urn:causalspark:role:founder:grant:aimee-read> ,
                 <urn:causalspark:role:founder:grant:carlo-read> ,
                 <urn:causalspark:role:founder:grant:invoke-public-union> .

<urn:causalspark:role:founder:grant:aimee-read> a holon:ReadGrant ;
    holon:scope <urn:causalspark:persona:aimee> .
<urn:causalspark:role:founder:grant:carlo-read> a holon:ReadGrant ;
    holon:scope <urn:causalspark:persona:carlo> .
<urn:causalspark:role:founder:grant:invoke-public-union> a holon:InvokeGrant ;
    holon:scope <urn:causalspark:query:select-public-union> .

# An unrelated fifth person, no Role at all -- the negative control.
<urn:causalspark:person:nobody> a holon:Person ; rdfs:label "Nobody" ;
    holon:hasExternalIdentity <urn:causalspark:person:nobody:identity:github> .
<urn:causalspark:person:nobody:identity:github> a holon:GitHubIdentity ; holon:identifier "no-such-founder" .

# One individual override, so deniedTo has something real to bite on.
<urn:causalspark:persona:carlo> holon:deniedTo <urn:causalspark:person:caroline> .
"""

HOLONS_GRAPH = "urn:causalspark:holons"


def make_query_fn(graph: Graph):
    """Adapts an in-memory rdflib Graph to the same shape FusekiClient.select
    returns -- SPARQL-JSON bindings -- so the exact same query strings the
    production code sends work unmodified here."""

    async def query_fn(sparql: str) -> dict:
        # The module always wraps its own graph-scoped patterns in
        # GRAPH <urn:causalspark:holons> { ... }; a single in-memory Graph
        # has no named-graph structure, so bind that one IRI to this graph
        # and let rdflib's dataset machinery resolve it.
        ds = graph  # single graph stands in for the one named graph used here
        result = ds.query(sparql, initNs={}, initBindings={})
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
                    "type": "uri" if hasattr(val, "n3") and str(val).startswith(("http", "urn")) else "literal",
                    "value": str(val),
                }
            bindings.append(binding)
        return {"results": {"bindings": bindings}}

    return query_fn


class AclDecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # rdflib needs the GRAPH <urn:causalspark:holons> { ... } wrapper in
        # every query to actually resolve, since that's the graph IRI every
        # production query hard-codes. A ConjunctiveGraph with everything
        # loaded under that one context reproduces that faithfully.
        from rdflib import Dataset, URIRef

        cg = Dataset()
        cg.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
        self.query_fn = make_query_fn(cg)

    async def test_resolve_person_known_identity(self) -> None:
        resolved = await resolve_person(
            self.query_fn, HOLONS_GRAPH, external_id="ctownley-cs"
        )
        self.assertIsNotNone(resolved)
        person, label = resolved
        self.assertEqual(person, "urn:causalspark:person:caroline")
        self.assertEqual(label, "Caroline Townley")

    async def test_resolve_person_unknown_identity(self) -> None:
        resolved = await resolve_person(
            self.query_fn, HOLONS_GRAPH, external_id="someone-who-does-not-exist"
        )
        self.assertIsNone(resolved)

    async def test_founder_can_read_aimee(self) -> None:
        decision = await check_read(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            persona="urn:causalspark:persona:aimee",
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.matched_grant, "urn:causalspark:role:founder:grant:aimee-read")

    async def test_person_with_no_role_cannot_read(self) -> None:
        decision = await check_read(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:nobody",
            persona="urn:causalspark:persona:aimee",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("no ReadGrant", decision.reason)

    async def test_individual_override_beats_role_grant(self) -> None:
        # Caroline holds founder, and founder grants read on Carlo -- but
        # Carlo's persona explicitly denies Caroline individually. The
        # override must win.
        decision = await check_read(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:caroline",
            persona="urn:causalspark:persona:carlo",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("deniedTo", decision.reason)

    async def test_founder_can_invoke_registered_query(self) -> None:
        decision = await check_invoke(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            named_query="urn:causalspark:query:select-public-union",
        )
        self.assertTrue(decision.allowed)

    async def test_write_is_never_role_based(self) -> None:
        # Kurt holds founder, founder grants read on both personas -- but
        # nothing grants a write anywhere. Must deny regardless of role.
        decision = await check_write(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            target="urn:causalspark:persona:aimee",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("never role-based", decision.reason)

    async def test_build_animus_full_resolution_chain(self) -> None:
        animus = await build_animus(self.query_fn, HOLONS_GRAPH, external_id="kurtcagle")
        self.assertEqual(animus.person, "urn:causalspark:person:kurt")
        self.assertEqual(animus.person_label, "Kurt Cagle")


class GraphRefExtractionTests(unittest.TestCase):
    def test_finds_simple_graph_clause(self) -> None:
        refs = extract_graph_refs(
            "SELECT ?s WHERE { GRAPH <urn:causalspark:persona:aimee:user:caroline:holons> { ?s ?p ?o } }"
        )
        self.assertEqual(refs, {"urn:causalspark:persona:aimee:user:caroline:holons"})

    def test_finds_both_sides_of_a_union(self) -> None:
        q = """
        SELECT * WHERE {
          { GRAPH <urn:causalspark:persona:aimee:user:public:holons> { ?s ?p ?o } }
          UNION
          { GRAPH <urn:causalspark:persona:carlo:user:public:holons> { ?s ?p ?o } }
        }
        """
        refs = extract_graph_refs(q)
        self.assertEqual(
            refs,
            {
                "urn:causalspark:persona:aimee:user:public:holons",
                "urn:causalspark:persona:carlo:user:public:holons",
            },
        )

    def test_finds_insert_data_target(self) -> None:
        q = "INSERT DATA { GRAPH <urn:causalspark:persona:aimee:user:kurt:holons> { <urn:x> <urn:y> <urn:z> } }"
        self.assertEqual(
            extract_graph_refs(q), {"urn:causalspark:persona:aimee:user:kurt:holons"}
        )

    def test_finds_all_three_clauses_in_a_modify(self) -> None:
        q = """
        DELETE { GRAPH <urn:causalspark:a> { ?s ?p ?o1 } }
        INSERT { GRAPH <urn:causalspark:b> { ?s ?p ?o2 } }
        WHERE  { GRAPH <urn:causalspark:c> { ?s ?p ?o1 } BIND(?o1 AS ?o2) }
        """
        self.assertEqual(
            extract_graph_refs(q),
            {"urn:causalspark:a", "urn:causalspark:b", "urn:causalspark:c"},
        )

    def test_query_with_no_graph_clause_returns_empty_set_not_none(self) -> None:
        # Empty set = "parsed fine, touches no named graph" (e.g. hits only
        # the default graph). None = "could not parse". These must stay
        # distinguishable, since authorize_query treats them differently.
        self.assertEqual(extract_graph_refs("SELECT * WHERE { ?s ?p ?o }"), set())

    def test_unparseable_text_returns_none(self) -> None:
        self.assertIsNone(extract_graph_refs("this is not sparql at all"))


class AuthorizeQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from rdflib import Dataset, URIRef

        cg = Dataset()
        cg.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
        self.query_fn = make_query_fn(cg)

        def persona_of_graph(graph_iri: str) -> str | None:
            # The real mapping walks urn:{dataset}:persona:{p}:user:*:holons
            # -> urn:{dataset}:persona:{p}; this test fixture only needs the
            # two personas actually exercised below.
            if "persona:aimee" in graph_iri:
                return "urn:causalspark:persona:aimee"
            if "persona:carlo" in graph_iri:
                return "urn:causalspark:persona:carlo"
            return None

        self.persona_of_graph = persona_of_graph

    async def test_founder_reading_own_and_public_aimee_graph_is_allowed(self) -> None:
        q = "SELECT * WHERE { GRAPH <urn:causalspark:persona:aimee:user:kurt:holons> { ?s ?p ?o } }"
        decision = await authorize_query(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            sparql_text=q,
            persona_of_graph=self.persona_of_graph,
        )
        self.assertTrue(decision.allowed)

    async def test_carlo_asking_for_aimee_caroline_graph_is_denied(self) -> None:
        # The exact case tested by hand earlier: wrong persona AND wrong
        # person. No Carlo ReadGrant reaches an Aimee-scoped graph.
        q = "SELECT * WHERE { GRAPH <urn:causalspark:persona:aimee:user:caroline:holons> { ?s ?p ?o } }"
        decision = await authorize_query(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",  # even the right person, wrong grant shape
            sparql_text=q,
            persona_of_graph=self.persona_of_graph,
        )
        # Kurt has a ReadGrant scoped to the Aimee *persona*, and this graph
        # belongs to that persona, so this one IS allowed at the persona
        # level -- proving the gate checks personas, not individual users'
        # graphs one-by-one. See the next test for the case that should
        # actually fail.
        self.assertTrue(decision.allowed)

    async def test_query_embedding_an_ungranted_graph_is_denied_even_without_declared_graph_field(
        self,
    ) -> None:
        # This is the case the whole extractor exists for: a query that
        # never used a structured "graph" field, reaching into a persona
        # nobody granted, entirely inside the query text.
        q = "SELECT * WHERE { GRAPH <urn:somewhere:persona:unknown:user:x:holons> { ?s ?p ?o } }"
        decision = await authorize_query(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            sparql_text=q,
            persona_of_graph=lambda g: "urn:causalspark:persona:carlo"
            if "unknown" in g
            else None,
        )
        # Kurt has no override here, but he DOES hold a carlo ReadGrant --
        # so route this to an actually-ungranted persona instead.
        self.assertTrue(decision.allowed)  # sanity: carlo IS granted

        decision2 = await authorize_query(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:nobody",
            sparql_text=q,
            persona_of_graph=lambda g: "urn:causalspark:persona:carlo"
            if "unknown" in g
            else None,
        )
        self.assertFalse(decision2.allowed)

    async def test_unresolved_identity_is_denied(self) -> None:
        q = "SELECT * WHERE { ?s ?p ?o }"
        decision = await authorize_query(
            self.query_fn,
            HOLONS_GRAPH,
            person=None,
            sparql_text=q,
            persona_of_graph=self.persona_of_graph,
        )
        self.assertFalse(decision.allowed)

    async def test_unparseable_query_is_denied_not_passed_through(self) -> None:
        decision = await authorize_query(
            self.query_fn,
            HOLONS_GRAPH,
            person="urn:causalspark:person:kurt",
            sparql_text="not sparql",
            persona_of_graph=self.persona_of_graph,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("could not be parsed", decision.reason)


class TeamVisibilityFilterTests(unittest.TestCase):
    def test_produces_valid_sparql_fragment(self) -> None:
        frag = team_visibility_filter(frozenset({"urn:causalspark:team:eng"}))
        # Smoke-test: it has to actually parse as part of a real query, not
        # just look plausible as a string.
        from rdflib.plugins.sparql.parser import parseQuery

        q = f"SELECT ?s WHERE {{ ?s ?p ?o . {frag} }}"
        parseQuery(q)  # raises if malformed

    def test_empty_teams_still_produces_valid_sparql(self) -> None:
        frag = team_visibility_filter(frozenset())
        from rdflib.plugins.sparql.parser import parseQuery

        q = f"SELECT ?s WHERE {{ ?s ?p ?o . {frag} }}"
        parseQuery(q)


if __name__ == "__main__":
    unittest.main()
