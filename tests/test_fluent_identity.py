"""``POST /fluent/update`` and ``GET /fluent/{id}/prior`` identify the caller.

Before this the update route depended only on the connection: anyone holding
the shared bearer token could write a fluent's value into the scene graph and
append to the events ledger, with no grant, and with an ``asserted_by`` field
the ledger recorded as fact. These tests run the REAL route, with the write
function replaced by a recorder, so they can assert both what the route hands
down and -- for every refusal -- that nothing was run.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from rdflib import Dataset, Namespace, URIRef

from holonbridge import audit, deps
from holonbridge.acl import Animus
from holonbridge.conn import Conn
from holonbridge.deps import get_client, require_animus, require_conn
from holonbridge.fluent import FluentUpdateResult, Operation, TypedValue
from holonbridge.routes import fluent as fluent_routes

HOLON = "https://w3id.org/holon/"
HB = Namespace(audit.HB)
HOLONS = "urn:example:holons"
EVENTS = "urn:example:events"
SCENE = "urn:example:scene"
SEQUENCES = "urn:example:sequences"
AUDIT = "urn:example:audit"
ADMIN = "urn:example:person:root"
JANE = "urn:example:person:jane"
JOHN = "urn:example:person:john"
FLUENT = "urn:example:fluent:stock-level"

FIXTURE = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<urn:example:role:admin> a holon:Role .
<{ADMIN}> a holon:Person ; rdfs:label "Root" ; holon:hasRole <urn:example:role:admin> .
<{JANE}> a holon:Person ; rdfs:label "Jane" .
<{JOHN}> a holon:Person ; rdfs:label "John" .
"""


class Store:
    def __init__(self) -> None:
        self.ds = Dataset()
        self.ds.get_context(URIRef(HOLONS)).parse(data=FIXTURE, format="turtle")

    async def select(self, conn, query, *, default_graph=None):
        result = self.ds.query(query)
        if result.type == "ASK":
            return {"boolean": bool(result.askAnswer)}
        rows = []
        for row in result:
            binding = {}
            for var in result.vars:
                val = row[var]
                if val is None:
                    continue
                text = str(val)
                binding[str(var)] = {"type": "uri" if text.startswith(("http", "urn")) else "literal",
                                     "value": text}
            rows.append(binding)
        return {"results": {"bindings": rows}}

    async def post_graph(self, conn, graph_iri, turtle):
        self.ds.get_context(URIRef(graph_iri)).parse(data=turtle, format="turtle")


def grant(store, *graphs, person=JANE):
    lines = [f"@prefix holon: <{HOLON}> ."] + [f"<{g}> holon:grantsWrite <{person}> ." for g in graphs]
    store.ds.get_context(URIRef(HOLONS)).parse(data="\n".join(lines), format="turtle")


class Recorder:
    """Stands in for fluent.update_fluent / prior_value."""

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.priors: list[str] = []
        self.prior = TypedValue("literal", "41")

    async def update_fluent(self, client, conn, **kw):
        self.updates.append(kw)
        new = TypedValue("literal", str(kw.get("value")))
        return FluentUpdateResult(
            fluent=kw["fluent"], operation=kw["operation"], old_value=None, new_value=new,
            assertion_iri="urn:example:assertion:1", superseded=None, sequence_id="urn:example:id-event-0001",
        )

    async def prior_value(self, client, conn, *, fluent):
        self.priors.append(fluent)
        return self.prior


@pytest.fixture(autouse=True)
def _reset_audit(monkeypatch):
    audit._level_cache.clear()
    monkeypatch.delenv("HOLONBRIDGE_AUDIT_LEVEL", raising=False)


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def conn() -> Conn:
    return Conn(base_url="http://localhost:3030", dataset="example", overridden=False, bank_name="local")


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(fluent_routes, "update_fluent", rec.update_fluent)
    monkeypatch.setattr(fluent_routes, "prior_value", rec.prior_value)
    return rec


@pytest.fixture
def who():
    return {"person": JANE, "real": JANE, "acting_as": False}


@pytest.fixture
def api(store, conn, who, recorder):
    app = FastAPI()
    app.state.fuseki = store
    app.add_middleware(audit.AuditMiddleware)
    app.include_router(fluent_routes.router)

    def _conn(request: Request) -> Conn:
        request.state.conn = conn
        return conn

    async def _animus(request: Request) -> Animus:
        animus = Animus("x", "GitHubIdentity", person=who["person"], real_person=who["real"],
                        acting_as=who["acting_as"])
        request.state.animus = animus
        return animus

    app.dependency_overrides[require_conn] = _conn
    app.dependency_overrides[get_client] = lambda: store
    app.dependency_overrides[require_animus] = _animus
    client = TestClient(app, raise_server_exceptions=False)
    client.app_ = app  # type: ignore[attr-defined]
    return client


def update(api, **overrides):
    body = {"fluent": FLUENT, "operation": "Set", "value": 42}
    body.update(overrides)
    return api.post("/fluent/update", json=body)


def audit_entries(store):
    g = store.ds.get_context(URIRef(AUDIT))
    out = []
    for e in g.subjects(URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), HB.AuditEntry):
        one = lambda p: next(iter(g.objects(e, p)), None)  # noqa: E731
        out.append({
            "outcome": str(one(HB.outcome)), "operation": str(one(HB.operation)),
            "actor": str(one(HB.actor)) if one(HB.actor) else None,
            "real": str(one(HB.realActor)) if one(HB.realActor) else None,
            "graphs": sorted(str(o) for o in g.objects(e, HB.targetGraph)),
            "hash": str(one(HB.payloadHash)) if one(HB.payloadHash) else None,
            "reason": str(one(HB.reason)) if one(HB.reason) else None,
        })
    return out


# --- identity is required ------------------------------------------------------------


def test_both_routes_depend_on_an_identity(api):
    routes = {r.path: r for r in fluent_routes.router.routes if isinstance(r, APIRoute)}
    assert set(routes) == {"/fluent/update", "/fluent/{fluent_id:path}/prior"}
    for path, route in routes.items():
        calls = [d.call for d in route.dependant.dependencies]
        assert deps.require_animus in calls, f"{path} does not require an identity"


# --- authorisation: three graphs ----------------------------------------------------------


def test_a_user_with_no_grants_is_refused_and_nothing_runs(api, recorder):
    response = update(api)
    assert response.status_code == 403
    assert "no explicit grantsWrite" in response.json()["detail"] and EVENTS in response.json()["detail"]
    assert recorder.updates == []


@pytest.mark.parametrize("missing", [EVENTS, SCENE, SEQUENCES])
def test_write_access_to_every_one_of_the_three_graphs_is_needed(api, store, recorder, missing):
    grant(store, *[g for g in (EVENTS, SCENE, SEQUENCES) if g != missing])
    response = update(api)
    assert response.status_code == 403 and missing in response.json()["detail"]
    assert recorder.updates == []


def test_with_all_three_grants_the_update_goes_through(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    response = update(api)
    assert response.status_code == 200, response.text
    assert len(recorder.updates) == 1 and response.json()["assertedBy"] == JANE


def test_an_admin_is_allowed(api, who, recorder):
    who.update(person=ADMIN, real=ADMIN)
    assert update(api).status_code == 200 and len(recorder.updates) == 1


def test_a_grant_on_someone_elses_behalf_is_not_enough_on_its_own(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES, person=JOHN)  # John holds them; Jane does not
    assert update(api).status_code == 403
    assert recorder.updates == []


# --- valid time ---------------------------------------------------------------------------


def test_asserted_datetime_is_forwarded(api, store, recorder):
    from datetime import datetime, timezone

    grant(store, EVENTS, SCENE, SEQUENCES)
    assert update(api, asserted_datetime="2026-11-12T22:41:00+00:00").status_code == 200
    assert recorder.updates[0]["asserted_datetime"] == datetime(2026, 11, 12, 22, 41, tzinfo=timezone.utc)


def test_a_naive_asserted_datetime_is_read_as_utc(api, store, recorder):
    from datetime import datetime, timezone

    grant(store, EVENTS, SCENE, SEQUENCES)
    assert update(api, asserted_datetime="2026-11-12T22:41:00").status_code == 200
    assert recorder.updates[0]["asserted_datetime"] == datetime(2026, 11, 12, 22, 41, tzinfo=timezone.utc)


def test_asserted_datetime_defaults_to_none_so_the_bridge_uses_now(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    assert update(api).status_code == 200
    assert recorder.updates[0]["asserted_datetime"] is None


def test_a_malformed_asserted_datetime_is_refused_before_anything_runs(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    assert update(api, asserted_datetime="last tuesday").status_code == 422
    assert recorder.updates == []


# --- attribution ---------------------------------------------------------------------------


def test_the_ledger_is_attributed_to_the_authenticated_person(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    update(api)
    assert recorder.updates[0]["asserted_by"] == JANE


def test_naming_yourself_is_accepted_and_omitting_is_fine(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    assert update(api, asserted_by=JANE).status_code == 200
    assert update(api).status_code == 200
    assert all(u["asserted_by"] == JANE for u in recorder.updates)


def test_asserting_as_someone_else_is_refused_not_silently_overridden(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    response = update(api, asserted_by=JOHN)
    assert response.status_code == 400 and "act_as" in response.json()["detail"]
    assert recorder.updates == []


def test_under_act_as_the_target_is_the_asserter_and_the_audit_names_both(api, store, who, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES, person=JOHN)
    who.update(person=JOHN, real=ADMIN, acting_as=True)
    assert update(api).status_code == 200
    assert recorder.updates[0]["asserted_by"] == JOHN
    (entry,) = audit_entries(store)
    assert entry["actor"] == JOHN and entry["real"] == ADMIN


# --- injection -------------------------------------------------------------------------------

HOSTILE = [
    "urn:example:f> ; a <urn:example:evil",
    "urn:example:f }",
    "urn:example:f x",
    "urn:example:f\n",
    'urn:example:f"',
    "urn:example:f|g",
]


@pytest.mark.parametrize("bad", HOSTILE)
def test_a_hostile_fluent_iri_is_refused_before_anything_runs(api, store, recorder, bad):
    grant(store, EVENTS, SCENE, SEQUENCES)
    response = update(api, fluent=bad)
    assert response.status_code == 400 and "fluent" in response.json()["detail"]
    assert recorder.updates == []


@pytest.mark.parametrize("bad", HOSTILE)
def test_a_hostile_iri_valued_update_is_refused(api, store, recorder, bad):
    grant(store, EVENTS, SCENE, SEQUENCES)
    response = update(api, operation="ListInsert", value=bad, is_iri=True)
    assert response.status_code == 400 and "value" in response.json()["detail"]
    assert recorder.updates == []


def test_an_iri_valued_update_needs_a_string(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    assert update(api, operation="ListInsert", value=12, is_iri=True).status_code == 400
    assert update(api, operation="ListInsert", value=None, is_iri=True).status_code == 400
    assert recorder.updates == []


def test_a_literal_value_is_not_treated_as_an_iri(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    text = 'anything at all: > } "quotes" and <angles>'
    assert update(api, value=text).status_code == 200  # escaped downstream, not an IRI
    assert recorder.updates[0]["value"] == text


def test_a_safe_iri_valued_update_passes_through(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    assert update(api, operation="ListInsert", value="urn:example:member:1", is_iri=True).status_code == 200
    assert recorder.updates[0]["value"] == "urn:example:member:1"


@pytest.mark.parametrize("encoded", ["urn:example:f%3E1", "urn:example:f%20x", "urn:example:f%22", "urn:example:f%7D"])
def test_the_prior_route_validates_its_path_parameter(api, recorder, encoded):
    response = api.get(f"/fluent/{encoded}/prior")
    assert response.status_code == 400
    assert recorder.priors == []  # never reached the query


# --- the read route -----------------------------------------------------------------------------


def test_prior_is_readable_by_any_identified_caller(api, recorder):
    response = api.get(f"/fluent/{FLUENT}/prior")
    assert response.status_code == 200
    assert response.json() == {"fluent": FLUENT, "priorValue": {"kind": "literal", "value": "41"}}
    assert recorder.priors == [FLUENT]


def test_prior_with_no_history_is_a_404(api, recorder):
    recorder.prior = None
    assert api.get(f"/fluent/{FLUENT}/prior").status_code == 404


# --- audit -----------------------------------------------------------------------------------------


def test_a_refused_update_is_recorded_with_its_graph_and_never_the_value(api, store, recorder):
    update(api, value="sensitive-figure-1234")
    (entry,) = audit_entries(store)
    assert entry["outcome"] == "denied" and entry["operation"] == "POST /fluent/update"
    assert entry["actor"] == JANE and EVENTS in (entry["reason"] or "")
    assert entry["hash"].startswith("sha256:")
    assert "sensitive-figure-1234" not in store.ds.get_context(URIRef(AUDIT)).serialize(format="turtle")


def test_an_allowed_update_is_recorded_against_all_three_graphs(api, store, recorder):
    grant(store, EVENTS, SCENE, SEQUENCES)
    update(api)
    (entry,) = audit_entries(store)
    assert entry["outcome"] == "allowed" and entry["actor"] == JANE
    assert entry["graphs"] == sorted([EVENTS, SCENE, SEQUENCES])
