"""Tests for toolset.py (resolve_reachable / bind_persona_param) and the
persona-gated named-queries / named-rules routes.

Route tests use the same rdflib-backed FusekiClient stub technique as
test_persona.py / test_bridge.py — chosen deliberately here too, since
resolve_reachable's own SPARQL was rewritten specifically to be
rdflib-compatible (see toolset.py's CORRECTED note).

19/19 passing (2026-08-18), run against a reconstructed package tree, no
live Fuseki. Also run combined with test_persona.py, test_bridge.py's
holon-scoping tests, and test_conn_persona_graphs.py in one session
(50/50) to check for cross-suite state leakage, given that was a real,
previously-caught bug in this same area (see test_persona.py's own FIXED
note). This is what caught both real bugs behind this feature: the
EXISTS-in-projection rdflib incompatibility, and the short-name-vs-full-IRI
mismatch between PersonaStore's actual return shape and this module's
first-draft `persona=` argument -- neither was visible from reading the
code, both were visible within one failing assertion once these tests ran
for real.
"""

from __future__ import annotations

import unittest

import pytest
from fastapi.testclient import TestClient
from rdflib import Dataset, URIRef

from holonbridge.acl import Animus
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.deps import require_animus
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app
from holonbridge.toolset import bind_persona_param, resolve_reachable

HOLON = "https://w3id.org/holon/"
HB = "https://w3id.org/holonbridge/"
HOLONS_GRAPH = "urn:ds:holons"
QUERIES_GRAPH = "urn:ds:named-queries"
RULES_GRAPH = "urn:ds:named-rules"
KURT = "urn:ds:person:kurt"

TOOLSET_FIXTURE = f"""
@prefix holon: <{HOLON}> .

<urn:ds:toolset:carlo-core> a holon:Toolset .
<urn:ds:toolset:aimee-core> a holon:Toolset .
<urn:ds:persona:carlo> holon:hasToolset <urn:ds:toolset:carlo-core> .
<urn:ds:persona:aimee> holon:hasToolset <urn:ds:toolset:aimee-core> .

# Toolset MEMBERSHIP triples live in ground truth (holons), same graph as
# the Toolset/Persona resources themselves -- see toolset.py's own
# docstring. The query/rule's own definition lives in its registry graph
# (named-queries/named-rules); only isPartOf-a-Toolset lives here. Getting
# this wrong (originally had these triples in the registry graph instead)
# is what test_no_persona_reaches_universal_only first caught.
<urn:ds:named-query:carlo-only-query> holon:isPartOf <urn:ds:toolset:carlo-core> .
<urn:ds:named-rule:carlo-only-rule> holon:isPartOf <urn:ds:toolset:carlo-core> .
"""

QUERIES_FIXTURE = f"""
@prefix hb: <{HB}> .
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

<urn:ds:named-query:open-query> a hb:NamedQuery ;
    hb:id "open-query" ;
    rdfs:label "Open query" ;
    hb:sparql "SELECT (1 AS ?n) WHERE {{}}" .

<urn:ds:named-query:carlo-only-query> a hb:NamedQuery ;
    hb:id "carlo-only-query" ;
    rdfs:label "Carlo-only query" ;
    hb:sparql "SELECT (1 AS ?n) WHERE {{}}" .

<urn:ds:named-query:persona-aware-query> a hb:NamedQuery ;
    hb:id "persona-aware-query" ;
    rdfs:label "Persona-aware query" ;
    hb:sparql "SELECT (\\"{{{{persona}}}}\\" AS ?who) WHERE {{}}" ;
    hb:parameter <urn:ds:named-query:persona-aware-query:param:persona> .

<urn:ds:named-query:persona-aware-query:param:persona> hb:name "persona" ;
    hb:datatype "xsd:anyURI" .
"""

RULES_FIXTURE = f"""
@prefix hb: <{HB}> .
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<urn:ds:named-rule:open-rule> a hb:NamedRule ;
    hb:id "open-rule" ;
    rdfs:label "Open rule" ;
    hb:construct "CONSTRUCT {{ <urn:ds:x> <urn:ds:y> <urn:ds:z> }} WHERE {{}}" ;
    hb:targetGraph "urn:ds:scratch" .

<urn:ds:named-rule:carlo-only-rule> a hb:NamedRule ;
    hb:id "carlo-only-rule" ;
    rdfs:label "Carlo-only rule" ;
    hb:construct "CONSTRUCT {{ <urn:ds:x> <urn:ds:y> <urn:ds:z> }} WHERE {{}}" ;
    hb:targetGraph "urn:ds:scratch" .
"""


def make_query_fn(dataset):
    async def query_fn(sparql: str) -> dict:
        result = dataset.query(sparql, initNs={}, initBindings={})
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


def make_dataset():
    ds = Dataset()
    ds.get_context(URIRef(HOLONS_GRAPH)).parse(data=TOOLSET_FIXTURE, format="turtle")
    ds.get_context(URIRef(QUERIES_GRAPH)).parse(data=QUERIES_FIXTURE, format="turtle")
    ds.get_context(URIRef(RULES_GRAPH)).parse(data=RULES_FIXTURE, format="turtle")
    return ds


class ResolveReachableTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.query_fn = make_query_fn(make_dataset())
        self.conn = Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")
        self.candidates = [
            "urn:ds:named-query:open-query",
            "urn:ds:named-query:carlo-only-query",
            "urn:ds:named-query:persona-aware-query",
        ]

    async def test_carlo_reaches_universal_and_his_own(self) -> None:
        # persona is the SHORT name, matching PersonaStore.get()'s real
        # return shape -- resolve_reachable converts internally.
        reachable = await resolve_reachable(
            self.query_fn, self.conn, persona="carlo", candidate_iris=self.candidates
        )
        self.assertEqual(
            reachable,
            {
                "urn:ds:named-query:open-query",
                "urn:ds:named-query:carlo-only-query",
                "urn:ds:named-query:persona-aware-query",
            },
        )

    async def test_aimee_does_not_reach_carlos_restricted_query(self) -> None:
        reachable = await resolve_reachable(
            self.query_fn, self.conn, persona="aimee", candidate_iris=self.candidates
        )
        self.assertNotIn("urn:ds:named-query:carlo-only-query", reachable)
        self.assertIn("urn:ds:named-query:open-query", reachable)

    async def test_no_persona_reaches_universal_only(self) -> None:
        reachable = await resolve_reachable(
            self.query_fn, self.conn, persona=None, candidate_iris=self.candidates
        )
        self.assertEqual(
            reachable, {"urn:ds:named-query:open-query", "urn:ds:named-query:persona-aware-query"}
        )


class BindPersonaParamTests(unittest.TestCase):
    def test_injects_when_declared_and_active(self) -> None:
        result = bind_persona_param({}, persona_iri="urn:ds:persona:carlo", declares_persona=True)
        self.assertEqual(result, {"persona": "urn:ds:persona:carlo"})

    def test_untouched_when_not_declared(self) -> None:
        result = bind_persona_param(
            {"foo": "bar"}, persona_iri="urn:ds:persona:carlo", declares_persona=False
        )
        self.assertEqual(result, {"foo": "bar"})

    def test_overrides_caller_supplied_value(self) -> None:
        result = bind_persona_param(
            {"persona": "urn:ds:persona:someone-else"},
            persona_iri="urn:ds:persona:carlo",
            declares_persona=True,
        )
        self.assertEqual(result["persona"], "urn:ds:persona:carlo")

    def test_removed_when_declared_but_no_active_persona(self) -> None:
        result = bind_persona_param({"persona": "whatever"}, persona_iri=None, declares_persona=True)
        self.assertNotIn("persona", result)


# --- route tests --------------------------------------------------------------


class RdflibFuseki:
    def __init__(self, dataset) -> None:
        self._ds = dataset

    async def select(self, conn, query, *, default_graph=None):
        return await make_query_fn(self._ds)(query)

    async def construct(self, conn, query, *, default_graph=None):
        return ""

    async def update(self, conn, update):
        return None

    async def ping(self, conn) -> bool:
        return True

    async def aclose(self) -> None:
        return None


TOKEN = "test-token"


@pytest.fixture
def app_client(tmp_path):
    settings = Settings(bearer_token=TOKEN, fuseki_dataset="ds")
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.fuseki = RdflibFuseki(make_dataset())
        # Isolated per test -- same reasoning as test_persona.py's and
        # test_bridge.py's matching fixtures.
        app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
        yield app, client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def as_(app, persona_iri: str) -> None:
    app.dependency_overrides[require_animus] = lambda: Animus(
        external_id="kurtcagle",
        external_id_type="GitHubIdentity",
        person=KURT,
        person_label="Kurt Cagle",
    )
    personas: PersonaStore = app.state.personas
    if persona_iri:
        name = persona_iri.rsplit(":", 1)[-1]
        personas.set(person_id=KURT, dataset="ds", persona=name)


def test_list_named_queries_requires_identity(app_client):
    _, client = app_client
    response = client.get("/named-queries", headers=auth())
    assert response.status_code == 401


def test_list_named_queries_filters_by_toolset(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:carlo")
    body = client.get("/named-queries", headers=auth()).json()
    ids = {q["id"] for q in body["queries"]}
    assert ids == {"open-query", "carlo-only-query", "persona-aware-query"}


def test_list_named_queries_excludes_restricted_for_other_persona(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:aimee")
    body = client.get("/named-queries", headers=auth()).json()
    ids = {q["id"] for q in body["queries"]}
    assert "carlo-only-query" not in ids
    assert "open-query" in ids


def test_get_restricted_query_404s_for_wrong_persona(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:aimee")
    response = client.get("/named-query/carlo-only-query", headers=auth())
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_named_query"


def test_get_restricted_query_succeeds_for_right_persona(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:carlo")
    response = client.get("/named-query/carlo-only-query", headers=auth())
    assert response.status_code == 200
    assert response.json()["id"] == "carlo-only-query"


def test_run_restricted_query_404s_for_wrong_persona(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:aimee")
    response = client.post("/named-query/carlo-only-query/run", json={"dry_run": True}, headers=auth())
    assert response.status_code == 404


def test_persona_param_bound_into_dry_run_sparql(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:carlo")
    body = client.post(
        "/named-query/persona-aware-query/run", json={"dry_run": True}, headers=auth()
    ).json()
    assert "<urn:ds:persona:carlo>" in body["sparql"]


def test_persona_param_not_forced_on_undeclared_query(app_client):
    """open-query does not declare `persona` -- confirms bind_persona_param's
    declares_persona gate actually prevents injection into it (this would
    have no visible effect either way for a placeholder query with no
    {{persona}} token, but matters directly for an hquery:-vocabulary
    query, which would otherwise hard-error on any undeclared supplied
    parameter)."""
    app, client = app_client
    as_(app, "urn:ds:persona:carlo")
    response = client.post("/named-query/open-query/run", json={"dry_run": True}, headers=auth())
    assert response.status_code == 200


def test_named_rules_list_filters_by_toolset(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:aimee")
    body = client.get("/named-rules", headers=auth()).json()
    ids = {r["id"] for r in body["rules"]}
    assert ids == {"open-rule"}


def test_run_restricted_rule_404s_for_wrong_persona(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:aimee")
    response = client.post("/named-rule/carlo-only-rule/run", json={"dry_run": True}, headers=auth())
    assert response.status_code == 404


def test_run_restricted_rule_succeeds_for_right_persona(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:carlo")
    response = client.post("/named-rule/carlo-only-rule/run", json={"dry_run": True}, headers=auth())
    assert response.status_code == 200


def test_run_all_rules_skips_unreachable(app_client):
    app, client = app_client
    as_(app, "urn:ds:persona:aimee")
    body = client.post("/named-rules/run", json={}, headers=auth()).json()
    ran_ids = {r["ruleId"] for r in body["results"]}
    assert "carlo-only-rule" not in ran_ids
    assert body["skipped"] >= 1


if __name__ == "__main__":
    unittest.main()
