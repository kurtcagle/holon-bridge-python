"""The owner tier: one level above admin.

Once a dataset has an owner, only an owner may grant or revoke ``admin`` or
``owner``; an admin keeps every other administrative power. A dataset with no
owner behaves exactly as before for ``admin`` (admins manage admins) until an
owner is established from the operator side, and ``owner`` itself is never
grantable by a non-owner.

Fixture: olivia (owner), adam (admin), jane (no role), plus a second dataset
state without an owner for the legacy cases. Callers authenticate with the
shared token and a header, as in the identity-admin tests.
"""

from __future__ import annotations

import unittest

import pytest
from fastapi.testclient import TestClient
from rdflib import Dataset, URIRef

from holonbridge.acl import is_admin, is_owner, owner_exists
from holonbridge.bootstrap import ensure_admin_person
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app

from .test_identity_admin import GraphStoreStub, _rdflib_query_fn

TOKEN = "test-token"
HOLON = "https://w3id.org/holon/"
HOLONS = "urn:example:holons"

PEOPLE = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<urn:example:role:admin> a holon:Role . <urn:example:role:owner> a holon:Role .

<urn:example:person:adam> a holon:Person ; rdfs:label "Adam" ;
    holon:hasExternalIdentity <urn:example:person:adam:identity:github> ;
    holon:hasRole <urn:example:role:admin> .
<urn:example:person:adam:identity:github> a holon:GitHubIdentity ; holon:identifier "adam-gh" .

<urn:example:person:jane> a holon:Person ; rdfs:label "Jane" ;
    holon:hasExternalIdentity <urn:example:person:jane:identity:github> .
<urn:example:person:jane:identity:github> a holon:GitHubIdentity ; holon:identifier "jane-gh" .
"""
OWNER = f"""
@prefix holon: <{HOLON}> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<urn:example:person:olivia> a holon:Person ; rdfs:label "Olivia" ;
    holon:hasExternalIdentity <urn:example:person:olivia:identity:github> ;
    holon:hasRole <urn:example:role:owner> .
<urn:example:person:olivia:identity:github> a holon:GitHubIdentity ; holon:identifier "olivia-gh" .
"""


def as_(login: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "X-Holon-Animus-Id": login, "X-Holon-Animus-Type": "GitHubIdentity"}


def build(tmp_path, *, with_owner: bool):
    stub = GraphStoreStub()
    g = stub.ds.get_context(URIRef(HOLONS))
    g.parse(data=PEOPLE, format="turtle")
    if with_owner:
        g.parse(data=OWNER, format="turtle")
    app = create_app(Settings(bearer_token=TOKEN, fuseki_dataset="example"))
    cm = TestClient(app)
    c = cm.__enter__()
    app.state.fuseki = stub
    app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
    return c, cm, stub


@pytest.fixture
def owned(tmp_path):
    c, cm, _ = build(tmp_path, with_owner=True)
    yield c
    cm.__exit__(None, None, None)


@pytest.fixture
def legacy(tmp_path):
    c, cm, _ = build(tmp_path, with_owner=False)
    yield c
    cm.__exit__(None, None, None)


def grant(c, who, person, role):
    return c.post("/admin/role/grant", json={"person": person, "role": role}, headers=as_(who))


def revoke(c, who, person, role):
    return c.post("/admin/role/revoke", json={"person": person, "role": role}, headers=as_(who))


def roles_of(c, person_slug: str, *, viewer: str = "olivia-gh") -> set[str]:
    listing = c.get("/admin/persons", headers=as_(viewer)).json()
    p = next(p for p in listing["persons"] if p["person"].endswith(f":{person_slug}"))
    return {r["role"].rsplit(":", 1)[-1] for r in p["roles"]}


# --- owner implies admin ----------------------------------------------------------------


def test_an_owner_passes_every_admin_check(owned):
    assert owned.get("/admin/persons", headers=as_("olivia-gh")).status_code == 200


class OwnerPredicateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ds = Dataset()
        ds.get_context(URIRef(HOLONS)).parse(data=PEOPLE + OWNER.split("\n", 2)[2], format="turtle")
        self.q = _rdflib_query_fn(ds)

    async def test_owner_is_admin_and_owner_admin_is_not_owner(self) -> None:
        self.assertTrue(await is_admin(self.q, HOLONS, person="urn:example:person:olivia"))
        self.assertTrue(await is_owner(self.q, HOLONS, person="urn:example:person:olivia"))
        self.assertTrue(await is_admin(self.q, HOLONS, person="urn:example:person:adam"))
        self.assertFalse(await is_owner(self.q, HOLONS, person="urn:example:person:adam"))
        self.assertFalse(await is_admin(self.q, HOLONS, person="urn:example:person:jane"))
        self.assertTrue(await owner_exists(self.q, HOLONS))


# --- an admin cannot manage admins or owners once there is an owner -----------------------


def test_an_admin_cannot_grant_admin_when_an_owner_exists(owned):
    r = grant(owned, "adam-gh", "jane", "admin")
    assert r.status_code == 403
    assert "owner" in r.json()["detail"]
    assert "admin" not in roles_of(owned, "jane")


def test_an_admin_cannot_grant_owner_to_themselves_or_anyone(owned):
    assert grant(owned, "adam-gh", "adam", "owner").status_code == 403
    assert grant(owned, "adam-gh", "jane", "owner").status_code == 403
    assert "owner" not in roles_of(owned, "adam")


def test_an_admin_cannot_create_a_person_with_a_privileged_role(owned):
    for role in ("admin", "owner"):
        r = owned.post(
            "/admin/person",
            json={"slug": "eve", "full_name": "Eve", "external_id": "eve-gh", "role": role},
            headers=as_("adam-gh"),
        )
        assert r.status_code == 403, role


def test_an_admin_cannot_revoke_an_owner_or_another_admin(owned):
    assert revoke(owned, "adam-gh", "olivia", "owner").status_code == 403
    grant(owned, "olivia-gh", "jane", "admin")
    assert revoke(owned, "adam-gh", "jane", "admin").status_code == 403
    assert "admin" in roles_of(owned, "jane")


def test_an_admin_cannot_deactivate_an_owner_or_an_admin(owned):
    assert owned.post("/admin/person/deactivate", json={"person": "olivia"}, headers=as_("adam-gh")).status_code == 403
    grant(owned, "olivia-gh", "jane", "admin")
    assert owned.post("/admin/person/deactivate", json={"person": "jane"}, headers=as_("adam-gh")).status_code == 403


def test_an_admin_keeps_every_other_power(owned):
    assert grant(owned, "adam-gh", "jane", "editor").status_code == 200
    assert "editor" in roles_of(owned, "jane")
    assert revoke(owned, "adam-gh", "jane", "editor").json()["revoked"] is True
    assert owned.post("/admin/person/deactivate", json={"person": "jane"}, headers=as_("adam-gh")).status_code == 200


def test_an_admin_cannot_climb_by_acting_as_the_owner(owned):
    """The gate looks at the real caller, as _require_admin does."""
    assert owned.post("/admin/act-as", json={"target": "olivia"}, headers=as_("adam-gh")).status_code == 200

    r = grant(owned, "adam-gh", "adam", "owner")

    assert r.status_code == 403
    assert "owner" not in roles_of(owned, "adam")


# --- what an owner can do -----------------------------------------------------------------


def test_an_owner_creates_and_removes_admins(owned):
    assert grant(owned, "olivia-gh", "jane", "admin").status_code == 200
    assert "admin" in roles_of(owned, "jane")
    assert revoke(owned, "olivia-gh", "jane", "admin").json()["revoked"] is True
    assert "admin" not in roles_of(owned, "jane")


def test_an_owner_can_create_a_person_as_admin(owned):
    r = owned.post(
        "/admin/person",
        json={"slug": "eve", "full_name": "Eve", "external_id": "eve-gh", "role": "admin"},
        headers=as_("olivia-gh"),
    )
    assert r.status_code == 200


def test_an_owner_can_deactivate_an_admin(owned):
    assert owned.post("/admin/person/deactivate", json={"person": "adam"}, headers=as_("olivia-gh")).status_code == 200


# --- ownership moves, it is never lost ----------------------------------------------------


def test_the_sole_owner_cannot_revoke_their_own_ownership(owned):
    r = revoke(owned, "olivia-gh", "olivia", "owner")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "only owner" in r.json()["note"]
    assert "owner" in roles_of(owned, "olivia")


def test_ownership_transfers_by_granting_then_revoking(owned):
    assert grant(owned, "olivia-gh", "jane", "owner").status_code == 200
    assert revoke(owned, "olivia-gh", "olivia", "owner").json()["revoked"] is True
    assert roles_of(owned, "olivia", viewer="jane-gh") == set()
    assert owned.get("/admin/persons", headers=as_("jane-gh")).status_code == 200


def test_an_owner_who_also_holds_admin_can_shed_just_the_admin_role(owned):
    grant(owned, "olivia-gh", "olivia", "admin")
    r = revoke(owned, "olivia-gh", "olivia", "admin")
    assert r.json()["revoked"] is True
    assert roles_of(owned, "olivia") == {"owner"}


def test_the_last_admin_capable_person_cannot_be_removed(owned):
    revoke(owned, "olivia-gh", "adam", "admin")
    r = revoke(owned, "olivia-gh", "olivia", "owner")
    assert r.json()["ok"] is False


# --- datasets that predate the owner role ------------------------------------------------


def test_with_no_owner_admins_still_manage_admins(legacy):
    assert grant(legacy, "adam-gh", "jane", "admin").status_code == 200


def test_with_no_owner_an_admin_still_cannot_mint_one(legacy):
    r = grant(legacy, "adam-gh", "adam", "owner")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_the_operator_can_give_a_legacy_dataset_its_first_owner():
    stub = GraphStoreStub()
    stub.ds.get_context(URIRef(HOLONS)).parse(data=PEOPLE, format="turtle")
    conn = Conn(base_url="http://x", dataset="example", overridden=False, bank_name="local")

    plain = await ensure_admin_person(
        stub, conn, slug="adam", full_name="Adam", external_id="adam-gh", role="owner"
    )
    assert plain["created"] is False and not plain.get("granted")  # default: never touches an existing Person

    granted = await ensure_admin_person(
        stub, conn, slug="adam", full_name="Adam", external_id="adam-gh", role="owner", grant_existing=True
    )
    assert granted["granted"] is True
    assert granted["person"] == "urn:example:person:adam"
    q = _rdflib_query_fn(stub.ds)
    assert await owner_exists(q, HOLONS)
