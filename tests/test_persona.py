"""Tests for persona.py (persona_exists/has_home), persona_state.py
(PersonaStore), and the /persona/switch, /persona/list routes.

Route tests reuse the technique test_acl.py already established: a
FusekiClient-shaped stub whose ``select`` runs real SPARQL against an
in-memory rdflib Dataset, seeded with the same shape already verified live
against urn:causalspark:holons (aimee/carlo as holon:Persona, kurt's Home
under aimee, no Home under carlo) -- not a network call, but the same
schema, same IRIs, queried with real SPARQL.

Run against a reconstructed package tree in a sandbox with no live Fuseki
reachable (2026-08-17), 20/20 passing -- this is what caught
has_home/resolve_scope_graphs passing a full Person IRI where
persona_user_graph expects a short slug, before that bug ever reached a
real request.
"""

from __future__ import annotations

import unittest

import pytest
from fastapi.testclient import TestClient

from holonbridge.acl import Animus
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.deps import require_animus
from holonbridge.persona import has_home, persona_exists
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app

HOLON = "https://w3id.org/holon/"
HOLONS_GRAPH = "urn:causalspark:holons"
KURT = "urn:causalspark:person:kurt"
NOBODY = "urn:causalspark:person:nobody"

# Mirrors what's actually live in urn:causalspark: two declared Personas,
# kurt has a Home under aimee, nothing under carlo.
FIXTURE_TURTLE = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<urn:causalspark:persona:aimee> a holon:Persona ; rdfs:label "Aimee" .
<urn:causalspark:persona:carlo> a holon:Persona ; rdfs:label "Carlo" .
"""

HOME_TURTLE = f"""
@prefix holon: <{HOLON}> .

<urn:causalspark:persona:aimee:user:kurt:home> a holon:Home ;
    holon:representsPerson <{KURT}> .
"""


def make_query_fn(dataset):
    async def query_fn(sparql: str) -> dict:
        result = dataset.query(sparql, initNs={}, initBindings={})
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


def make_dataset():
    from rdflib import Dataset, URIRef

    ds = Dataset()
    ds.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
    ds.get_context(URIRef("urn:causalspark:persona:aimee:user:kurt:holons")).parse(
        data=HOME_TURTLE, format="turtle"
    )
    return ds


class PersonaExistsHasHomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.query_fn = make_query_fn(make_dataset())
        self.conn = Conn(
            base_url="http://x", dataset="causalspark", overridden=False, bank_name="local"
        )

    async def test_persona_exists_true(self) -> None:
        self.assertTrue(await persona_exists(self.query_fn, self.conn, persona="aimee"))

    async def test_persona_exists_false_for_unknown_name(self) -> None:
        self.assertFalse(await persona_exists(self.query_fn, self.conn, persona="nope"))

    async def test_has_home_true_for_member(self) -> None:
        # person_id is the FULL Person IRI, same as Animus.person -- the
        # exact shape that used to raise ValueError before person_slug.
        self.assertTrue(
            await has_home(self.query_fn, self.conn, persona="aimee", person_id=KURT)
        )

    async def test_has_home_false_for_non_member(self) -> None:
        # carlo exists (persona_exists would be True) but kurt has no Home
        # graph seeded for it.
        self.assertFalse(
            await has_home(self.query_fn, self.conn, persona="carlo", person_id=KURT)
        )

    async def test_has_home_false_for_unknown_person(self) -> None:
        self.assertFalse(
            await has_home(self.query_fn, self.conn, persona="aimee", person_id=NOBODY)
        )


class PersonaStoreTests(unittest.TestCase):
    def test_unset_reports_none(self) -> None:
        store = PersonaStore(path=self._tmp("a"))
        self.assertEqual(store.get(person_id=KURT, dataset="causalspark"), (None, "none"))

    def test_set_then_get_reports_explicit(self) -> None:
        store = PersonaStore(path=self._tmp("b"))
        store.set(person_id=KURT, dataset="causalspark", persona="aimee")
        self.assertEqual(
            store.get(person_id=KURT, dataset="causalspark"), ("aimee", "explicit")
        )

    def test_persists_across_instances_as_persisted(self) -> None:
        path = self._tmp("c")
        PersonaStore(path=path).set(person_id=KURT, dataset="causalspark", persona="aimee")
        reloaded = PersonaStore(path=path)
        self.assertEqual(
            reloaded.get(person_id=KURT, dataset="causalspark"), ("aimee", "persisted")
        )

    def test_clearing_empty_string_removes_the_entry(self) -> None:
        store = PersonaStore(path=self._tmp("d"))
        store.set(person_id=KURT, dataset="causalspark", persona="aimee")
        store.set(person_id=KURT, dataset="causalspark", persona="")
        self.assertEqual(store.get(person_id=KURT, dataset="causalspark"), (None, "none"))

    def test_one_persons_entry_never_touches_another(self) -> None:
        store = PersonaStore(path=self._tmp("e"))
        store.set(person_id=KURT, dataset="causalspark", persona="aimee")
        store.set(person_id=NOBODY, dataset="causalspark", persona="carlo")
        self.assertEqual(
            store.get(person_id=KURT, dataset="causalspark"), ("aimee", "explicit")
        )
        self.assertEqual(
            store.get(person_id=NOBODY, dataset="causalspark"), ("carlo", "explicit")
        )

    def test_one_dataset_never_touches_another_for_the_same_person(self) -> None:
        store = PersonaStore(path=self._tmp("f"))
        store.set(person_id=KURT, dataset="causalspark", persona="aimee")
        store.set(person_id=KURT, dataset="chloe", persona="carlo")
        self.assertEqual(
            store.get(person_id=KURT, dataset="causalspark"), ("aimee", "explicit")
        )
        self.assertEqual(store.get(person_id=KURT, dataset="chloe"), ("carlo", "explicit"))

    def test_env_default_only_applies_with_no_stored_entry(self) -> None:
        import os

        store = PersonaStore(path=self._tmp("g"))
        os.environ["HOLONBRIDGE_PERSONA"] = "aimee"
        try:
            self.assertEqual(
                store.get(person_id=KURT, dataset="causalspark"), ("aimee", "env")
            )
            store.set(person_id=KURT, dataset="causalspark", persona="carlo")
            # persisted choice wins over env default -- the precedence
            # inversion relative to switch_dataset/switch_bank.
            self.assertEqual(
                store.get(person_id=KURT, dataset="causalspark"), ("carlo", "explicit")
            )
        finally:
            del os.environ["HOLONBRIDGE_PERSONA"]

    @staticmethod
    def _tmp(name: str):
        import tempfile
        from pathlib import Path

        return Path(tempfile.gettempdir()) / f"test-persona-state-{name}.json"


# --- route tests ------------------------------------------------------------


class RdflibFuseki:
    """FusekiClient-shaped stub whose select() runs real SPARQL against an
    in-memory rdflib Dataset -- same technique as test_acl.py's
    make_query_fn, wrapped in FusekiClient's method signature so it can
    back a real TestClient route test."""

    def __init__(self, dataset) -> None:
        self._ds = dataset

    async def select(self, conn, query, *, default_graph=None):
        return await make_query_fn(self._ds)(query)

    async def construct(self, conn, query, *, default_graph=None):
        return ""

    async def ping(self, conn) -> bool:
        return True

    async def aclose(self) -> None:
        return None


TOKEN = "test-token"


@pytest.fixture
def app_client():
    settings = Settings(bearer_token=TOKEN, fuseki_dataset="causalspark")
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.fuseki = RdflibFuseki(make_dataset())
        yield app, client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def as_kurt(app) -> None:
    app.dependency_overrides[require_animus] = lambda: Animus(
        external_id="kurtcagle",
        external_id_type="GitHubIdentity",
        person=KURT,
        person_label="Kurt Cagle",
    )


def test_switch_requires_identity(app_client):
    _, client = app_client
    response = client.post("/persona/switch", json={"name": "aimee"}, headers=auth())
    assert response.status_code == 401


def test_switch_to_unknown_name_is_not_found(app_client):
    app, client = app_client
    as_kurt(app)
    body = client.post("/persona/switch", json={"name": "nope"}, headers=auth()).json()
    assert body["ok"] is False
    assert "not_found" in body["note"]


def test_switch_to_persona_with_no_home_is_refused(app_client):
    app, client = app_client
    as_kurt(app)
    body = client.post("/persona/switch", json={"name": "carlo"}, headers=auth()).json()
    assert body["ok"] is False
    assert "refused" in body["note"]


def test_switch_to_persona_with_home_succeeds(app_client):
    app, client = app_client
    as_kurt(app)
    body = client.post("/persona/switch", json={"name": "aimee"}, headers=auth()).json()
    assert body == {"ok": True, "persona": "aimee"}


def test_whoami_reports_the_switch(app_client):
    app, client = app_client
    as_kurt(app)
    client.post("/persona/switch", json={"name": "aimee"}, headers=auth())
    body = client.get("/whoami", headers=auth()).json()
    assert body["persona"] == "aimee"
    assert body["personaSource"] == "explicit"


def test_get_endpoint_reports_the_switch(app_client):
    app, client = app_client
    as_kurt(app)
    client.post("/persona/switch", json={"name": "aimee"}, headers=auth())
    body = client.get("/endpoint", headers=auth()).json()
    assert body["personaOverride"] == "aimee"
    assert body["personaOverrideSource"] == "explicit"


def test_clearing_falls_back_to_none(app_client):
    app, client = app_client
    as_kurt(app)
    client.post("/persona/switch", json={"name": "aimee"}, headers=auth())
    clear = client.post("/persona/switch", json={"name": ""}, headers=auth()).json()
    assert clear == {
        "ok": True,
        "cleared": True,
        "persona": None,
        "note": "override cleared; ground truth only",
    }
    body = client.get("/whoami", headers=auth()).json()
    assert body["persona"] is None
    assert body["personaSource"] == "none"


def test_list_personas_reports_membership(app_client):
    app, client = app_client
    as_kurt(app)
    body = client.get("/persona/list", headers=auth()).json()
    by_name = {p["name"]: p for p in body["personas"]}
    assert by_name["aimee"]["member"] is True
    assert by_name["carlo"]["member"] is False


if __name__ == "__main__":
    unittest.main()
