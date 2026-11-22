"""Per-fluent write grants: a Person may write named fluents without holding the three graph grants.

Runs the REAL fluent and fluent-grant routes with the write function replaced by a recorder, so every
refusal also proves nothing ran.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from rdflib import URIRef

from holonbridge import acl, audit
from holonbridge import fluent_grants as fg
from holonbridge.routes import fluent as fluent_routes
from holonbridge.routes import fluent_grants as grant_routes

from .test_fluent_identity import (  # noqa: F401  (fixtures and helpers reused as-is)
    ADMIN, EVENTS, FLUENT, HOLONS, JANE, JOHN, SCENE, SEQUENCES, Recorder, Store, _reset_audit,
    audit_entries, conn, grant, recorder, store, update, who,
)
from .test_fluent_identity import api as _api_unused  # noqa: F401
from fastapi.testclient import TestClient as _TC  # noqa: F401
from holonbridge.acl import Animus
from holonbridge.config import Settings
from holonbridge.deps import get_client, require_animus, require_conn

PROC = "urn:example:proc:campaign:spring:"
F1, F2 = PROC + "position", PROC + "channel:email"
OTHER = "urn:example:proc:campaign:autumn:position"


class GStore(Store):
    async def update(self, conn, sparql):
        self.ds.update(sparql)


@pytest.fixture
def store():
    return GStore()


@pytest.fixture
def api(store, conn, who, recorder, monkeypatch):
    async def many(client, conn, **kw):
        recorder.updates.append({"many": [o.fluent for o in kw["ops"]]})
        raise RuntimeError("stop")  # route maps nothing; the recorder entry is what we assert

    monkeypatch.setattr(fluent_routes, "update_fluents", many)
    app = FastAPI()
    app.state.fuseki = store
    app.state.settings = Settings()
    app.add_middleware(audit.AuditMiddleware)
    app.include_router(fluent_routes.router)
    app.include_router(grant_routes.router)

    def _conn(request: Request):
        request.state.conn = conn
        return conn

    async def _animus(request: Request) -> Animus:
        a = Animus("x", "GitHubIdentity", person=who["person"], real_person=who["real"], acting_as=who["acting_as"])
        request.state.animus = a
        return a

    app.dependency_overrides[require_conn] = _conn
    app.dependency_overrides[get_client] = lambda: store
    app.dependency_overrides[require_animus] = _animus
    return TestClient(app, raise_server_exceptions=False)


def stored_grant(store, *, person=JANE, fluents=(), prefixes=(), until=None, revoked=False, slug="g1"):
    now = datetime.now(timezone.utc)
    iri = fg.grant_iri(HOLONS, slug)
    ttl = fg.build_grant(iri=iri, grantee=person, granted_by=ADMIN, fluents=list(fluents), prefixes=list(prefixes),
                         granted_at=now, valid_until=until)
    store.ds.get_context(URIRef(HOLONS)).parse(data=ttl, format="turtle")
    if revoked:
        store.ds.update(fg.revoke_update(HOLONS, iri, now))
    return iri


def test_exact_fluent_grant_allows_the_write_without_graph_grants(api, recorder, store):
    stored_grant(store, fluents=[F1])
    r = update(api, fluent=F1)
    assert r.status_code == 200, r.text
    assert r.json()["assertedBy"] == JANE
    assert [u["fluent"] for u in recorder.updates] == [F1]


def test_prefix_grant_covers_the_process_and_not_a_sibling(api, recorder, store):
    stored_grant(store, prefixes=[PROC])
    assert update(api, fluent=F2).status_code == 200
    r = update(api, fluent=OTHER)
    assert r.status_code == 403 and len(recorder.updates) == 1


def test_a_fluent_grant_gives_no_graph_write(store):
    stored_grant(store, prefixes=[PROC])

    async def q(query):
        return await store.select(None, query)

    import asyncio
    for g in (EVENTS, SCENE, SEQUENCES):
        d = asyncio.run(acl.check_write(q, HOLONS, person=JANE, target=g))
        assert not d.allowed


def test_someone_elses_grant_does_not_apply(api, recorder, store):
    stored_grant(store, person=JOHN, fluents=[F1])
    assert update(api, fluent=F1).status_code == 403 and recorder.updates == []


def test_expired_and_revoked_grants_are_refused(api, recorder, store):
    stored_grant(store, fluents=[F1], until=datetime.now(timezone.utc) - timedelta(minutes=1), slug="old")
    stored_grant(store, fluents=[F1], revoked=True, slug="rev")
    assert update(api, fluent=F1).status_code == 403 and recorder.updates == []


def test_update_many_needs_every_fluent_covered(api, recorder, store):
    stored_grant(store, prefixes=[PROC])
    body = lambda *fl: {"operations": [{"fluent": f, "operation": "Set", "value": 1} for f in fl]}  # noqa: E731
    r = api.post("/fluent/update-many", json=body(F1, OTHER))
    assert r.status_code == 403 and recorder.updates == []
    api.post("/fluent/update-many", json=body(F1, F2))
    assert recorder.updates == [{"many": [F1, F2]}]


def test_graph_grants_still_work_unchanged(api, recorder, store):
    grant(store, EVENTS, SCENE, SEQUENCES)
    assert update(api, fluent=OTHER).status_code == 200


def test_the_audit_record_names_the_fluent_grant(api, store):
    iri = stored_grant(store, fluents=[F1])
    update(api, fluent=F1)
    assert any(e["outcome"] and iri for e in audit_entries(store))


# --- the admin routes ---------------------------------------------------------------------


def test_only_an_admin_can_create_and_acting_as_does_not_count(api, who, store):
    body = {"grantee": JANE, "prefixes": [PROC]}
    assert api.post("/fluent-grant", json=body).status_code == 403
    who.update(person=ADMIN, real=JANE, acting_as=True)  # a non-admin acting as the admin
    assert api.post("/fluent-grant", json=body).status_code == 403
    assert not list(store.ds.get_context(URIRef(HOLONS)).subjects(None, URIRef(fg.HOLON + "FluentGrant")))


def test_admin_creates_then_revokes_and_the_participant_follows(api, who, recorder, store):
    who.update(person=ADMIN, real=ADMIN)
    r = api.post("/fluent-grant", json={"grantee": JANE, "prefixes": [PROC], "lifetime_days": 7})
    assert r.status_code == 200, r.text
    grant_iri = r.json()["grant"]
    who.update(person=JANE, real=JANE)
    assert update(api, fluent=F1).status_code == 200
    who.update(person=ADMIN, real=ADMIN)
    assert api.get("/fluent-grant", params={"grantee": JANE}).json()["grants"][0]["grant"] == grant_iri
    assert api.post("/fluent-grant/revoke", json={"grant": grant_iri}).json()["revoked"] is True
    who.update(person=JANE, real=JANE)
    assert update(api, fluent=F1).status_code == 403
    assert len(recorder.updates) == 1


@pytest.mark.parametrize("body", [
    {"grantee": JANE},                                  # nothing named
    {"grantee": JANE, "prefixes": ["urn:x:"]},          # too broad
    {"grantee": JANE, "fluents": ["urn:a> . <urn:b"]},  # would change the Turtle's structure
    {"grantee": "urn:example:person:nobody", "fluents": [F1]},  # no such person (404)
])
def test_bad_requests_write_nothing(api, who, store, body):
    who.update(person=ADMIN, real=ADMIN)
    assert api.post("/fluent-grant", json=body).status_code in (400, 404)
    assert not list(store.ds.get_context(URIRef(HOLONS)).subjects(None, URIRef(fg.HOLON + "FluentGrant")))
