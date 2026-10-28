"""ACL and delegation against a real Jena Fuseki (``pytest -m jena``).

Skipped unless a Jena is configured -- see ``tests/jena_support.py``. Three
parts:

1. ``DelegationAclTests`` from ``test_delegation_acl.py`` re-run with its store
   swapped for Jena. Every assertion is inherited unchanged.
2. The function tests of ``test_delegation.py`` that use its ``Store``,
   re-run the same way (the module's ``Store`` is patched to a Jena one).
3. New tests for the least-privilege paths, written to exercise SPARQL that
   rdflib and a stub would both let through: no grant means no write, roles
   never grant writes, replace is stricter than write, ``find_delegation`` for
   identities with and without delegations (PR #43), a delegation's create /
   use / revoke round trip, and the dynamic-graph refusal in
   ``authorize_query``/``authorize_update`` -- including through the real
   ``/sparql`` routes, with the real ``FusekiClient``.
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from holonbridge import delegation as dg
from holonbridge.acl import (
    Animus,
    authorize_query,
    authorize_update,
    check_read,
    check_replace,
    check_write,
    is_admin,
)
from holonbridge.deps import get_acting_as, get_client, require_animus, require_conn
from holonbridge.fuseki import FusekiClient
from holonbridge.routes import sparql as sparql_routes

from . import jena_support as js
from . import test_delegation as td
from . import test_delegation_acl as tda

pytestmark = pytest.mark.jena


# --- fixtures -------------------------------------------------------------------


@pytest.fixture(scope="session")
def jena_server(tmp_path_factory):
    if not js.configured():
        pytest.skip(js.skip_reason())
    server = js.JenaServer(tmp_path_factory.mktemp("jena")).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def jena(jena_server) -> js.JenaDataset:
    jena_server.reset()  # every test starts from an empty dataset
    return js.JenaDataset(jena_server)


@pytest.fixture(autouse=True)
def _bind_jena(request):
    """Hand the dataset to unittest-style classes, which cannot take fixtures."""
    if request.instance is not None and isinstance(request.instance, unittest.TestCase):
        request.instance.jena = request.getfixturevalue("jena")


# --- 1. DelegationAclTests, unchanged assertions, real Jena -----------------------


class TestDelegationAclOnJena(tda.DelegationAclTests):
    def setUp(self) -> None:
        self.ds = self.jena
        self.g = self.ds.get_context(tda.HOLONS)
        self.g.parse(data=tda.FIXTURE, format="turtle")
        self.q = js.query_fn_for(self.ds)
        self.now = datetime.now(timezone.utc)


# --- 2. test_delegation.py's Store-based tests, real Jena -------------------------


class _JenaStore:
    """Same surface as ``test_delegation.Store``, backed by Jena."""

    def __init__(self) -> None:
        self.ds = _JenaStore.dataset
        self.g = self.ds.graph(td.HOLONS)
        self.g.parse(
            data=f"""@prefix holon: <https://w3id.org/holon/> .
<{td.KURT}> a holon:Person . <{td.PA}> a holon:Person . <{td.OTHER}> a holon:Person .
<{td.PERSONA}> a holon:Persona .""",
            format="turtle",
        )

    dataset: js.JenaDataset

    def query_fn(self, q):
        result = self.ds.query(q)
        return result.raw

    def update(self, q):
        self.ds.update(q)

    def add(self, ttl):
        self.g.parse(data=ttl, format="turtle")


@pytest.fixture(autouse=True)
def _store_on_jena(request, monkeypatch):
    if request.function.__name__ in _REUSED:
        _JenaStore.dataset = request.getfixturevalue("jena")
        monkeypatch.setattr(td, "Store", _JenaStore)


def _uses_store(fn) -> bool:
    return "Store(" in inspect.getsource(fn)


_REUSED = {
    name: fn
    for name, fn in vars(td).items()
    if name.startswith("test_") and inspect.isfunction(fn) and _uses_store(fn)
}
globals().update(_REUSED)  # collected here, run unchanged against the patched Store


# --- 3. new least-privilege tests ------------------------------------------------

HOLON = "https://w3id.org/holon/"
HOLONS = "urn:lane:holons"
ADMIN = "urn:lane:person:root"
ALICE = "urn:lane:person:alice"
BOB = "urn:lane:person:bob"
CAROL = "urn:lane:person:carol"
EDITOR_ROLE = "urn:lane:role:editor"
PERSONA = "urn:lane:persona:p"
ALICE_HOLONS = "urn:lane:persona:p:user:alice:holons"
BOB_HOLONS = "urn:lane:persona:p:user:bob:holons"
SHARED = "urn:lane:persona:p:public:holons"
ADMIN_ROLE = "urn:lane:role:admin"  # derived by acl._admin_role from the holons graph

BASE = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{ADMIN_ROLE}> a holon:Role .
<{EDITOR_ROLE}> a holon:Role .
<{ADMIN}> a holon:Person ; holon:hasRole <{ADMIN_ROLE}> .
<{ALICE}> a holon:Person .
<{BOB}> a holon:Person .
<{CAROL}> a holon:Person ; holon:hasRole <{EDITOR_ROLE}> .
<{PERSONA}> a holon:Persona .
"""


def persona_of(graph: str):
    prefix = "urn:lane:persona:"
    return prefix + graph[len(prefix):].partition(":")[0] if graph.startswith(prefix) else None


@pytest.fixture
def store(jena) -> js.JenaDataset:
    jena.get_context(HOLONS).parse(data=BASE, format="turtle")
    return jena


@pytest.fixture
def q(store):
    return js.query_fn_for(store)


def add(store, ttl: str) -> None:
    store.get_context(HOLONS).parse(data=f"@prefix holon: <{HOLON}> .\n{ttl}", format="turtle")


async def test_an_identity_with_no_grant_cannot_write(q):
    for person in (ALICE, BOB, CAROL):
        decision = await check_write(q, HOLONS, person=person, target=ALICE_HOLONS)
        assert not decision.allowed, person
        assert "no explicit grantsWrite" in decision.reason


async def test_a_grant_to_one_person_does_not_reach_another_or_another_graph(store, q):
    add(store, f"<{ALICE_HOLONS}> holon:grantsWrite <{ALICE}> .")
    assert (await check_write(q, HOLONS, person=ALICE, target=ALICE_HOLONS)).allowed
    assert not (await check_write(q, HOLONS, person=BOB, target=ALICE_HOLONS)).allowed
    assert not (await check_write(q, HOLONS, person=ALICE, target=BOB_HOLONS)).allowed


async def test_explicit_grantswrite_is_required_roles_never_grant_writes(store, q):
    # CAROL holds a non-admin role, the persona has a ReadGrant-style link, and the
    # *role* is named as the grant's object: none of that is a grant to CAROL.
    add(store, f"<{ALICE_HOLONS}> holon:grantsWrite <{EDITOR_ROLE}> .")
    add(store, f"<{PERSONA}> holon:grantsRead <{CAROL}> .")
    decision = await check_write(q, HOLONS, person=CAROL, target=ALICE_HOLONS)
    assert not decision.allowed
    assert "never role-based" in decision.reason
    add(store, f"<{ALICE_HOLONS}> holon:grantsWrite <{CAROL}> .")
    allowed = await check_write(q, HOLONS, person=CAROL, target=ALICE_HOLONS)
    assert allowed.allowed and allowed.reason == "explicit grantsWrite"


async def test_only_the_admin_role_bypasses_and_only_for_that_dataset(store, q):
    assert (await check_write(q, HOLONS, person=ADMIN, target=ALICE_HOLONS)).allowed
    assert await is_admin(q, HOLONS, person=ADMIN)
    assert not await is_admin(q, HOLONS, person=CAROL)  # a role, but not the admin one


async def test_replace_is_stricter_than_write(store, q):
    add(store, f"<{ALICE_HOLONS}> holon:grantsWrite <{ALICE}> .")
    assert (await check_write(q, HOLONS, person=ALICE, target=ALICE_HOLONS)).allowed
    denied = await check_replace(q, HOLONS, person=ALICE, target=ALICE_HOLONS)
    assert not denied.allowed and "replace is stricter" in denied.reason
    add(store, f"<{ALICE_HOLONS}> holon:grantsReplace <{ALICE}> .")
    assert (await check_replace(q, HOLONS, person=ALICE, target=ALICE_HOLONS)).allowed


async def test_grantsreplace_alone_does_not_imply_write(store, q):
    add(store, f"<{ALICE_HOLONS}> holon:grantsReplace <{ALICE}> .")
    assert not (await check_write(q, HOLONS, person=ALICE, target=ALICE_HOLONS)).allowed


async def test_scratch_namespace_needs_its_own_namespace_grant(store, q):
    scratch = "urn:lane:scratch:abc123"
    assert not (await check_write(q, HOLONS, person=ALICE, target=scratch)).allowed
    add(store, f"<urn:lane:scratch> holon:grantsWrite <{ALICE}> .")
    assert (await check_write(q, HOLONS, person=ALICE, target=scratch)).allowed
    assert not (await check_replace(q, HOLONS, person=ALICE, target=scratch)).allowed


async def test_check_read_needs_a_persona_readgrant_roles_alone_do_not_suffice(store, q):
    assert not (await check_read(q, HOLONS, person=CAROL, persona=PERSONA)).allowed


# --- find_delegation: the PR #43 case -------------------------------------------------


@pytest.mark.parametrize("permission", ["read", "write"])
@pytest.mark.parametrize("kwargs", [
    {},
    {"target": ALICE_HOLONS},
    {"delegator": ALICE},
    {"target": ALICE_HOLONS, "delegator": ALICE},
])
async def test_find_delegation_for_an_identity_with_no_delegations_does_not_crash(
    store, q, permission, kwargs
):
    # The common case, and the one that crashed Jena with ARQInternalErrorException
    # ("Peek iterator is already empty"): the dataset has no Delegation rows at all.
    assert await dg.find_delegation(q, HOLONS, delegate=BOB, permission=permission, **kwargs) == []


async def test_find_delegation_against_an_absent_holons_graph_returns_nothing(jena):
    q = js.query_fn_for(jena)  # nothing loaded at all
    assert await dg.find_delegation(
        q, HOLONS, delegate=BOB, permission="write", target=ALICE_HOLONS) == []


async def test_find_delegation_with_other_peoples_delegations_present(store, q):
    now = datetime.now(timezone.utc)
    store.get_context(HOLONS).parse(
        data=dg.build_delegation(
            delegation_iri="urn:lane:delegation:a", delegator=ALICE, delegate=CAROL,
            persona=PERSONA, graphs=[ALICE_HOLONS], permissions=["read", "write"],
            valid_from=now - timedelta(hours=1), valid_until=now + timedelta(days=1)),
        format="turtle")
    assert await dg.find_delegation(q, HOLONS, delegate=BOB, permission="write",
                                    target=ALICE_HOLONS) == []
    found = await dg.find_delegation(q, HOLONS, delegate=CAROL, permission="write",
                                     target=ALICE_HOLONS)
    assert [m.delegation for m in found] == ["urn:lane:delegation:a"]


async def test_delegation_create_use_revoke_round_trip(store, q):
    now = datetime.now(timezone.utc)
    iri = "urn:lane:delegation:rt"
    assert not (await check_write(q, HOLONS, person=BOB, target=ALICE_HOLONS)).allowed

    store.get_context(HOLONS).parse(
        data=dg.build_delegation(
            delegation_iri=iri, delegator=ALICE, delegate=BOB, persona=PERSONA,
            graphs=[ALICE_HOLONS], permissions=["read", "write"],
            valid_from=now - timedelta(minutes=5), valid_until=now + timedelta(days=1)),
        format="turtle")
    use = await check_write(q, HOLONS, person=BOB, target=ALICE_HOLONS)
    assert use.allowed and use.matched_grant == iri
    assert not (await check_replace(q, HOLONS, person=BOB, target=ALICE_HOLONS)).allowed
    rows = await dg.list_delegations(q, HOLONS)
    assert {r["delegation"]: r["status"] for r in rows}[iri] == "active"

    store.update(dg.revoke_update(HOLONS, iri, now))
    assert not (await check_write(q, HOLONS, person=BOB, target=ALICE_HOLONS)).allowed
    rows = await dg.list_delegations(q, HOLONS)
    assert {r["delegation"]: r["status"] for r in rows}[iri] == "revoked"


async def test_revoke_all_update_on_real_jena(store, q):
    now = datetime.now(timezone.utc)
    for n, (by, to) in enumerate([(ALICE, BOB), (BOB, ALICE), (ALICE, CAROL)]):
        store.get_context(HOLONS).parse(
            data=dg.build_delegation(
                delegation_iri=f"urn:lane:delegation:{n}", delegator=by, delegate=to,
                persona=PERSONA, graphs=[f"urn:lane:persona:p:user:{by.rsplit(':', 1)[-1]}:holons"],
                permissions=["read"], valid_from=now - timedelta(hours=1),
                valid_until=now + timedelta(days=1)),
            format="turtle")
    store.update(dg.revoke_all_update(HOLONS, BOB, now))
    status = {r["delegation"]: r["status"] for r in await dg.list_delegations(q, HOLONS)}
    assert status["urn:lane:delegation:0"] == "revoked"
    assert status["urn:lane:delegation:1"] == "revoked"
    assert status["urn:lane:delegation:2"] == "active"  # does not involve BOB


# --- authorize_query / authorize_update ---------------------------------------------


async def read(q, sparql, person=BOB):
    return await authorize_query(q, HOLONS, person=person, sparql_text=sparql,
                                 persona_of_graph=persona_of)


async def test_a_query_over_a_graph_variable_is_refused_to_non_admins(store, q):
    add(store, f"<{PERSONA}> holon:grantsRead <{BOB}> .")
    for text in (
        "SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }",
        f"SELECT * WHERE {{ GRAPH <{SHARED}> {{ ?s ?p ?o }} GRAPH ?g {{ ?a ?b ?c }} }}",
        "SELECT * FROM NAMED <urn:lane:persona:p:user:alice:holons> WHERE { GRAPH ?g { ?s ?p ?o } }",
    ):
        decision = await read(q, text)
        assert not decision.allowed, text
        assert "does not name" in decision.reason or "no ReadGrant" in decision.reason
    assert (await read(q, "SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }", person=ADMIN)).allowed


async def test_a_query_over_someone_elses_private_graph_is_refused_without_a_delegation(store, q):
    decision = await read(q, f"SELECT * WHERE {{ GRAPH <{ALICE_HOLONS}> {{ ?s ?p ?o }} }}")
    assert not decision.allowed and "no ReadGrant" in decision.reason


async def test_unparseable_query_is_refused(q):
    assert not (await read(q, "SELEKT nonsense {")).allowed


async def test_dynamic_updates_are_refused_even_to_admins(store, q):
    for text in (
        "INSERT { GRAPH ?g { <urn:x:a> <urn:x:b> <urn:x:c> } } WHERE { VALUES ?g { <urn:x:g> } }",
        "DROP ALL",
        "CLEAR NAMED",
    ):
        for person in (ALICE, ADMIN):
            decision = await authorize_update(q, HOLONS, person=person, sparql_text=text)
            assert not decision.allowed, (person, text)


async def test_update_needs_write_per_graph_and_replace_for_wholesale_ops(store, q):
    add(store, f"<{ALICE_HOLONS}> holon:grantsWrite <{ALICE}> .")
    insert = (f"INSERT DATA {{ GRAPH <{ALICE_HOLONS}> {{ <urn:x:a> <urn:x:b> <urn:x:c> }} }}")
    assert (await authorize_update(q, HOLONS, person=ALICE, sparql_text=insert)).allowed
    assert not (await authorize_update(q, HOLONS, person=BOB, sparql_text=insert)).allowed
    drop = f"DROP GRAPH <{ALICE_HOLONS}>"
    refused = await authorize_update(q, HOLONS, person=ALICE, sparql_text=drop)
    assert not refused.allowed and "grantsReplace" in refused.reason
    add(store, f"<{ALICE_HOLONS}> holon:grantsReplace <{ALICE}> .")
    assert (await authorize_update(q, HOLONS, person=ALICE, sparql_text=drop)).allowed


# --- through the real /sparql routes, real FusekiClient, real Jena -------------------


def _app(store: js.JenaDataset, person: str) -> TestClient:
    conn = js.conn_for(store)
    # The holons graph and the persona scheme the routes use come from the Conn.
    conn_holons = conn.holons_graph
    assert conn_holons  # derived from the dataset name
    app = FastAPI()
    app.include_router(sparql_routes.router)
    client = FusekiClient()

    async def animus() -> Animus:
        return Animus("x", "GitHubIdentity", person=person, real_person=person)

    app.dependency_overrides[require_conn] = lambda: conn
    app.dependency_overrides[get_client] = lambda: client
    app.dependency_overrides[require_animus] = animus
    app.dependency_overrides[get_acting_as] = lambda: type(
        "A", (), {"get": lambda *a, **k: None, "clear": lambda *a, **k: None,
                  "set": lambda *a, **k: None})()
    return TestClient(app)


@pytest.fixture
def routes(jena):
    """A route client factory whose holons graph is the dataset's real one."""
    conn = js.conn_for(jena)
    jena.get_context(conn.holons_graph).parse(
        data=BASE.replace("urn:lane:role:admin", f"{conn.holons_graph[:-len(':holons')]}:role:admin"),
        format="turtle")
    return jena, conn


def test_route_refuses_graph_variable_select_for_a_non_admin_and_runs_it_for_admin(routes):
    jena, conn = routes
    admin_role = conn.holons_graph[: -len(":holons")] + ":role:admin"
    non_admin = _app(jena, ALICE)
    refused = non_admin.post("/sparql/select", json={"query": "SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }"})
    assert refused.status_code == 403, refused.text
    jena.get_context(conn.holons_graph).parse(
        data=f"@prefix holon: <{HOLON}> . <{ADMIN}> holon:hasRole <{admin_role}> .", format="turtle")
    ok = _app(jena, ADMIN).post("/sparql/select", json={"query": "SELECT * WHERE { GRAPH ?g { ?s ?p ?o } } LIMIT 1"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["results"]["bindings"]


def test_route_update_without_a_grant_is_403_and_writes_nothing(routes):
    jena, _ = routes
    target = "urn:lane:persona:p:user:alice:holons"
    text = f"INSERT DATA {{ GRAPH <{target}> {{ <urn:x:a> <urn:x:b> <urn:x:c> }} }}"
    response = _app(jena, BOB).post("/sparql/update", json={"update": text})
    assert response.status_code == 403
    assert not jena.query(f"ASK {{ GRAPH <{target}> {{ ?s ?p ?o }} }}").askAnswer


def test_route_update_with_an_explicit_grant_reaches_jena(routes):
    jena, conn = routes
    target = "urn:lane:persona:p:user:alice:holons"
    jena.get_context(conn.holons_graph).parse(
        data=f"@prefix holon: <{HOLON}> . <{target}> holon:grantsWrite <{ALICE}> .", format="turtle")
    text = f"INSERT DATA {{ GRAPH <{target}> {{ <urn:x:a> <urn:x:b> <urn:x:c> }} }}"
    response = _app(jena, ALICE).post("/sparql/update", json={"update": text})
    assert response.status_code == 200, response.text
    assert jena.query(f"ASK {{ GRAPH <{target}> {{ <urn:x:a> <urn:x:b> <urn:x:c> }} }}").askAnswer


async def test_a_query_naming_no_graph_is_cleared_by_the_acl_but_sees_only_the_default_graph(
    store, q
):
    # Pins current behaviour, which differs from "refused": authorize_query treats a
    # query with no graph reference as touching nothing. Safe only because this
    # dataset's default graph is not the union of the named graphs (Fuseki's
    # default). On a dataset with unionDefaultGraph this same query would read
    # every graph -- the ACL could not tell.
    decision = await read(q, "SELECT * WHERE { ?s ?p ?o }")
    assert decision.allowed
    assert store.query("SELECT * WHERE { ?s ?p ?o }").raw["results"]["bindings"] == []
    assert store.query(
        f"SELECT * WHERE {{ GRAPH <{HOLONS}> {{ ?s ?p ?o }} }}"
    ).raw["results"]["bindings"]  # the data is there; the bare query just cannot see it
