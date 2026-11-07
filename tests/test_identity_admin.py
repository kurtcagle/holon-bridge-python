"""Tests for the identity_admin admin routes added/extended 2026-09-18:
list_persons, grant_role/revoke_role, grant_write/revoke_write, and
deactivate_person/reactivate_person -- plus the acl.py resolve_person
change that actually makes deactivation lock a caller out.

Two layers, matching the two patterns already established elsewhere in
this suite:

- ``ResolvePersonInactiveTests`` -- pure acl.py unit tests against a real
  in-memory rdflib Dataset and a real query_fn, the same shape
  test_admin_bypass.py / test_acting_as.py used before their
  CausalSpark-specific fixtures were pulled for the data-leakage cleanup
  (see git history). No FastAPI involved.

- Everything else -- full route tests through FastAPI's TestClient, with
  FusekiClient replaced by ``GraphStoreStub``: a stand-in backed by a
  real in-memory rdflib ``Dataset`` rather than a string-matched fixture.
  ``select``/``update``/``post_graph`` all run the actual SPARQL text
  each route generates against real named graphs, so a grant made by one
  request is genuinely visible to the next -- unlike tests/test_dataset_admin.py's
  DatasetStub (which only needs to replay one fixed shape), the routes
  under test here compose (grant, then list, then revoke, then list
  again), and only a real store proves that composition works end to end.
"""

from __future__ import annotations

import unittest

import pytest
from fastapi.testclient import TestClient
from rdflib import Dataset, URIRef

from holonbridge.acl import resolve_person
from holonbridge.config import Settings
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app

TOKEN = "test-token"
HOLON = "https://w3id.org/holon/"
HOLONS_GRAPH = "urn:example:holons"

# Two Persons seeded before every route test: kurt (admin) and jane (no
# role at all yet) -- exactly enough to exercise "the only admin" guards
# without every test needing its own fixture.
FIXTURE_TURTLE = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<urn:example:role:admin> a holon:Role ; rdfs:label "Admin" .

<urn:example:person:kurt> a holon:Person ; rdfs:label "Kurt Cagle" ;
    holon:hasExternalIdentity <urn:example:person:kurt:identity:github> ;
    holon:hasRole <urn:example:role:admin> .
<urn:example:person:kurt:identity:github> a holon:GitHubIdentity ;
    holon:identifier "kurtcagle" .

<urn:example:person:jane> a holon:Person ; rdfs:label "Jane Doe" ;
    holon:hasExternalIdentity <urn:example:person:jane:identity:github> .
<urn:example:person:jane:identity:github> a holon:GitHubIdentity ;
    holon:identifier "jane-doe" .
"""


# --- pure acl.py unit tests ------------------------------------------------


def _rdflib_query_fn(graph):
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


class SoleRemainingAdminHelperTests(unittest.IsolatedAsyncioTestCase):
    """Direct unit coverage of ``_sole_remaining_admin``, rather than only
    exercising it indirectly through a route.

    Worth doing as its own thing: within a single request, "the target
    holds admin AND is not the caller AND is the only admin" is actually
    unreachable through the routes above, because reaching ``_require_admin``
    at all already requires the caller to hold admin -- so if the target
    is truly the *only* admin, the caller must *be* the target, which
    ``deactivate_person``'s separate self-refusal check catches first (see
    ``test_deactivate_refuses_the_callers_own_identity``) and
    ``revoke_role`` has no self-check to intercept it before
    (``test_revoke_role_refuses_to_remove_the_sole_admin``, which is
    exactly that self case). The "not self" branch only matters under
    concurrent requests racing each other -- the same TOCTOU shape this
    codebase already found once in write_turtle_to_graph -- so it is
    tested here at the function level instead of contorted into an HTTP
    scenario that cannot actually construct it.
    """

    def setUp(self) -> None:
        from holonbridge.conn import Conn

        ds = Dataset()
        ds.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
        self.query_fn = _rdflib_query_fn(ds)
        self.ds = ds
        self.conn = Conn(
            base_url="http://localhost:3030",
            dataset="example",
            overridden=False,
            bank_name="local",
        )

    async def test_kurt_is_sole_admin_in_the_base_fixture(self) -> None:
        from holonbridge.routes.identity_admin import _sole_remaining_admin

        self.assertTrue(
            await _sole_remaining_admin(
                self.query_fn, self.conn, exclude="urn:example:person:kurt"
            )
        )

    async def test_not_sole_once_a_second_admin_exists(self) -> None:
        from holonbridge.routes.identity_admin import _sole_remaining_admin

        self.ds.get_context(URIRef(HOLONS_GRAPH)).parse(
            data=f"""@prefix holon: <{HOLON}> .
<urn:example:person:jane> holon:hasRole <urn:example:role:admin> .""",
            format="turtle",
        )
        self.assertFalse(
            await _sole_remaining_admin(
                self.query_fn, self.conn, exclude="urn:example:person:kurt"
            )
        )
        # And symmetrically -- excluding jane, kurt remains, so jane is
        # not sole either.
        self.assertFalse(
            await _sole_remaining_admin(
                self.query_fn, self.conn, exclude="urn:example:person:jane"
            )
        )


class ResolvePersonInactiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ds = Dataset()
        ds.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
        self.ds = ds
        self.query_fn = _rdflib_query_fn(ds)

    async def test_active_person_resolves_normally(self) -> None:
        resolved = await resolve_person(
            self.query_fn, HOLONS_GRAPH, external_id="kurtcagle"
        )
        self.assertIsNotNone(resolved)
        person, label = resolved
        self.assertEqual(person, "urn:example:person:kurt")
        self.assertEqual(label, "Kurt Cagle")

    async def test_unknown_external_id_resolves_to_none(self) -> None:
        resolved = await resolve_person(
            self.query_fn, HOLONS_GRAPH, external_id="nobody"
        )
        self.assertIsNone(resolved)

    async def test_inactive_person_resolves_to_none_exactly_like_unknown(self) -> None:
        self.ds.get_context(URIRef(HOLONS_GRAPH)).parse(
            data=f"""@prefix holon: <{HOLON}> .
<urn:example:person:kurt> holon:status "inactive" .""",
            format="turtle",
        )
        resolved = await resolve_person(
            self.query_fn, HOLONS_GRAPH, external_id="kurtcagle"
        )
        self.assertIsNone(resolved)

    async def test_status_other_than_inactive_does_not_block_resolution(self) -> None:
        # Only the literal "inactive" locks anyone out -- any other value
        # (a future status this codebase hasn't invented yet) must not
        # accidentally deny access.
        self.ds.get_context(URIRef(HOLONS_GRAPH)).parse(
            data=f"""@prefix holon: <{HOLON}> .
<urn:example:person:jane> holon:status "pending" .""",
            format="turtle",
        )
        resolved = await resolve_person(
            self.query_fn, HOLONS_GRAPH, external_id="jane-doe"
        )
        self.assertIsNotNone(resolved)


# --- route-level tests through FastAPI ------------------------------------


class GraphStoreStub:
    """A FusekiClient stand-in backed by a real in-memory rdflib Dataset.

    Unlike a string-matched stub, ``select``/``update``/``post_graph``
    here run the actual SPARQL each route generates against real named
    graphs -- a grant made by one HTTP call is genuinely visible to the
    next, which is exactly what the tests below need to prove.
    """

    def __init__(self) -> None:
        self.ds = Dataset()

    async def select(self, conn, query, *, default_graph=None):
        result = self.ds.query(query)
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

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        result = self.ds.query(query)
        return result.serialize(format="turtle")

    async def update(self, conn, update):
        self.ds.update(update)

    async def get_graph(self, conn, graph_iri):
        return self.ds.get_context(URIRef(graph_iri)).serialize(format="turtle")

    async def get_graph_ntriples(self, conn, graph_iri):
        graph = self.ds.default_context if graph_iri is None else self.ds.get_context(URIRef(graph_iri))
        return graph.serialize(format="nt")

    async def post_graph(self, conn, graph_iri, turtle):
        self.ds.get_context(URIRef(graph_iri)).parse(data=turtle, format="turtle")

    async def put_graph(self, conn, graph_iri, turtle):
        self.ds.remove_context(self.ds.get_context(URIRef(graph_iri)))
        self.ds.get_context(URIRef(graph_iri)).parse(data=turtle, format="turtle")

    async def drop_graph(self, conn, graph_iri):
        self.ds.remove_context(self.ds.get_context(URIRef(graph_iri)))

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        return "[] a <http://www.w3.org/ns/shacl#ValidationReport> ; <http://www.w3.org/ns/shacl#conforms> true ."

    async def list_datasets(self, conn):
        return []

    async def ping(self, conn):
        return True

    async def aclose(self):
        return None


@pytest.fixture
def stub() -> GraphStoreStub:
    store = GraphStoreStub()
    store.ds.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
    return store


@pytest.fixture
def client(stub: GraphStoreStub, tmp_path):
    settings = Settings(bearer_token=TOKEN, fuseki_dataset="example")
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        # Isolated per test, same fix test_bridge.py/test_persona.py applied --
        # otherwise app.state.personas defaults to the real
        # ~/.holonbridge/persona-state.json, shared across every test.
        app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
        yield test_client


def admin_auth() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Holon-Animus-Id": "kurtcagle",
        "X-Holon-Animus-Type": "GitHubIdentity",
    }


def jane_auth() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Holon-Animus-Id": "jane-doe",
        "X-Holon-Animus-Type": "GitHubIdentity",
    }


# --- list_persons -----------------------------------------------------------


def test_list_persons_requires_admin(client):
    response = client.get("/admin/persons", headers=jane_auth())
    assert response.status_code == 403


def test_list_persons_returns_the_roster(client):
    body = client.get("/admin/persons", headers=admin_auth()).json()
    assert body["count"] == 2
    by_slug = {p["person"].rsplit(":", 1)[-1]: p for p in body["persons"]}
    assert by_slug["kurt"]["status"] == "active"
    assert by_slug["kurt"]["roles"][0]["role"] == "urn:example:role:admin"
    assert by_slug["kurt"]["external_identities"][0]["identifier"] == "kurtcagle"
    assert by_slug["jane"]["roles"] == []
    assert by_slug["jane"]["write_grants"] == []


# --- grant_role / revoke_role ------------------------------------------------


def test_grant_role_requires_an_existing_person(client):
    response = client.post(
        "/admin/role/grant",
        json={"person": "jane-doe", "role": "editor"},
        headers=admin_auth(),
    )
    assert response.status_code == 404


def test_grant_role_against_the_fixtures_actual_person_slug(client):
    response = client.post(
        "/admin/role/grant",
        json={"person": "urn:example:person:jane", "role": "editor"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "person": "urn:example:person:jane", "role": "urn:example:role:editor"}

    listing = client.get("/admin/persons", headers=admin_auth()).json()
    jane = next(p for p in listing["persons"] if p["person"] == "urn:example:person:jane")
    assert {r["role"] for r in jane["roles"]} == {"urn:example:role:editor"}


def test_grant_role_is_idempotent(client):
    for _ in range(2):
        response = client.post(
            "/admin/role/grant",
            json={"person": "urn:example:person:jane", "role": "editor"},
            headers=admin_auth(),
        )
        assert response.status_code == 200
    listing = client.get("/admin/persons", headers=admin_auth()).json()
    jane = next(p for p in listing["persons"] if p["person"] == "urn:example:person:jane")
    assert len(jane["roles"]) == 1


def test_revoke_role_removes_it(client):
    client.post(
        "/admin/role/grant",
        json={"person": "urn:example:person:jane", "role": "editor"},
        headers=admin_auth(),
    )
    response = client.post(
        "/admin/role/revoke",
        json={"person": "urn:example:person:jane", "role": "editor"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    assert response.json()["revoked"] is True

    listing = client.get("/admin/persons", headers=admin_auth()).json()
    jane = next(p for p in listing["persons"] if p["person"] == "urn:example:person:jane")
    assert jane["roles"] == []


def test_revoke_role_not_held_is_an_ok_false_not_an_error(client):
    response = client.post(
        "/admin/role/revoke",
        json={"person": "urn:example:person:jane", "role": "editor"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "not_found" in body["note"]


def test_revoke_role_refuses_to_remove_the_sole_admin(client):
    response = client.post(
        "/admin/role/revoke",
        json={"person": "urn:example:person:kurt", "role": "admin"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "refused" in body["note"]

    # Still holds it -- the guard actually blocked the write.
    listing = client.get("/admin/persons", headers=admin_auth()).json()
    kurt = next(p for p in listing["persons"] if p["person"] == "urn:example:person:kurt")
    assert {r["role"] for r in kurt["roles"]} == {"urn:example:role:admin"}


def test_revoke_role_allows_removing_admin_when_a_second_admin_exists(client):
    client.post(
        "/admin/role/grant",
        json={"person": "urn:example:person:jane", "role": "admin"},
        headers=admin_auth(),
    )
    response = client.post(
        "/admin/role/revoke",
        json={"person": "urn:example:person:kurt", "role": "admin"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    assert response.json()["revoked"] is True


# --- grant_write / revoke_write ----------------------------------------------


def test_write_grant_round_trips_through_acl_check_write(client):
    from holonbridge.acl import check_write

    target = "urn:example:persona:assistant"
    grant = client.post(
        "/admin/write/grant",
        json={"person": "urn:example:person:jane", "target": target, "mode": "write"},
        headers=admin_auth(),
    )
    assert grant.status_code == 200
    assert grant.json() == {
        "ok": True,
        "person": "urn:example:person:jane",
        "target": target,
        "mode": "write",
    }

    graph = client.app.state.fuseki.ds  # type: ignore[attr-defined]

    async def query_fn(q: str) -> dict:
        result = graph.query(q)
        return {"boolean": bool(result.askAnswer)}

    import asyncio

    decision = asyncio.run(
        check_write(query_fn, HOLONS_GRAPH, person="urn:example:person:jane", target=target)
    )
    assert decision.allowed is True

    revoke = client.post(
        "/admin/write/revoke",
        json={"person": "urn:example:person:jane", "target": target, "mode": "write"},
        headers=admin_auth(),
    )
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] is True

    decision_after = asyncio.run(
        check_write(query_fn, HOLONS_GRAPH, person="urn:example:person:jane", target=target)
    )
    assert decision_after.allowed is False


def test_write_and_replace_are_independent_grants(client):
    target = "urn:example:persona:assistant"
    client.post(
        "/admin/write/grant",
        json={"person": "urn:example:person:jane", "target": target, "mode": "write"},
        headers=admin_auth(),
    )
    revoke_replace = client.post(
        "/admin/write/revoke",
        json={"person": "urn:example:person:jane", "target": target, "mode": "replace"},
        headers=admin_auth(),
    )
    assert revoke_replace.json()["ok"] is False  # never granted under "replace"

    listing = client.get("/admin/persons", headers=admin_auth()).json()
    jane = next(p for p in listing["persons"] if p["person"] == "urn:example:person:jane")
    assert jane["write_grants"] == [{"target": target, "mode": "write"}]


def test_write_grant_rejects_an_invalid_mode(client):
    response = client.post(
        "/admin/write/grant",
        json={"person": "urn:example:person:jane", "target": "urn:example:x", "mode": "delete"},
        headers=admin_auth(),
    )
    assert response.status_code == 422


# --- deactivate_person / reactivate_person -----------------------------------


def test_deactivate_strips_roles_and_grants_and_sets_status(client):
    client.post(
        "/admin/role/grant",
        json={"person": "urn:example:person:jane", "role": "editor"},
        headers=admin_auth(),
    )
    client.post(
        "/admin/write/grant",
        json={"person": "urn:example:person:jane", "target": "urn:example:x", "mode": "write"},
        headers=admin_auth(),
    )

    response = client.post(
        "/admin/person/deactivate",
        json={"person": "urn:example:person:jane"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["roles_revoked"] == ["urn:example:role:editor"]
    assert body["write_grants_revoked"] == [{"target": "urn:example:x", "mode": "write"}]

    listing = client.get("/admin/persons", headers=admin_auth()).json()
    jane = next(p for p in listing["persons"] if p["person"] == "urn:example:person:jane")
    assert jane["status"] == "inactive"
    assert jane["roles"] == []
    assert jane["write_grants"] == []
    # The Person itself, and their identity, survive.
    assert jane["external_identities"][0]["identifier"] == "jane-doe"


def test_deactivated_person_gets_401_on_their_next_request(client):
    client.post(
        "/admin/person/deactivate",
        json={"person": "urn:example:person:jane"},
        headers=admin_auth(),
    )
    response = client.get("/whoami", headers=jane_auth())
    assert response.status_code == 401


def test_deactivate_refuses_the_callers_own_identity(client):
    response = client.post(
        "/admin/person/deactivate",
        json={"person": "urn:example:person:kurt"},
        headers=admin_auth(),
    )
    assert response.status_code == 400


def test_deactivate_allows_removing_a_non_sole_admin(client):
    # With two admins, deactivating one that is NOT the caller must
    # succeed -- confirms the sole-admin guard is scoped to "no other
    # admin remains," not "any admin at all."
    client.post(
        "/admin/role/grant",
        json={"person": "urn:example:person:jane", "role": "admin"},
        headers=admin_auth(),
    )
    response = client.post(
        "/admin/person/deactivate",
        json={"person": "urn:example:person:jane"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_reactivate_restores_resolution_but_not_roles(client):
    client.post(
        "/admin/role/grant",
        json={"person": "urn:example:person:jane", "role": "editor"},
        headers=admin_auth(),
    )
    client.post(
        "/admin/person/deactivate",
        json={"person": "urn:example:person:jane"},
        headers=admin_auth(),
    )
    assert client.get("/whoami", headers=jane_auth()).status_code == 401

    response = client.post(
        "/admin/person/reactivate",
        json={"person": "urn:example:person:jane"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    assert response.json()["reactivated"] is True

    # Identity resolves again...
    assert client.get("/whoami", headers=jane_auth()).status_code == 200
    # ...but the Role from before deactivation was not reimplied.
    listing = client.get("/admin/persons", headers=admin_auth()).json()
    jane = next(p for p in listing["persons"] if p["person"] == "urn:example:person:jane")
    assert jane["roles"] == []
    assert jane["status"] == "active"


def test_reactivate_never_deactivated_person_is_a_safe_no_op(client):
    response = client.post(
        "/admin/person/reactivate",
        json={"person": "urn:example:person:jane"},
        headers=admin_auth(),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


if __name__ == "__main__":
    unittest.main()
