"""``hb:Portal``: registering, governing a position property, and crossing.

Real ``traverse``, ``update_fluents`` and ``sequence.mint`` against rdflib's SPARQL
engine, with the lock and effect queries as real SELECTs over the same store; only
the named-query registry is replaced (by a dict), since its loader is not what is
under test. The world: Ann is in Calais and wants to be in Dover; a portal joins them.

    ann --ex:position--> <ann-loc>  (SetValue fluent, property ex:locatedIn, now <Calais>)
    ann --ex:purse-----> <ann-purse> (numeric fluent, holding 10)
    lock:   the traveller must hold a passport
    effect: the crossing costs 3 from the purse
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

pytest.importorskip("pyshacl")

from holonbridge import fluent_transaction as tx_mod  # noqa: E402
from holonbridge import portal as portal_mod  # noqa: E402
from holonbridge.named_queries import NamedQuery  # noqa: E402
from holonbridge.params import Parameter  # noqa: E402
from holonbridge.portal import (  # noqa: E402
    HB_NAMESPACE,
    Portal,
    PortalError,
    deletion_update,
    governing_portals,
    load_portals,
    portal_node,
    registration_update,
    render_portal_turtle,
    status_update,
    traverse,
)

from .test_fluent_gate_scope import GatedStore  # noqa: E402
from .test_fluent_ledger_race import ALICE, HEVT, HOLON, _conn  # noqa: E402

EX = "urn:ex:"
ANN = f"{EX}ann"
LOC = f"{EX}ann-loc"
PURSE = f"{EX}ann-purse"
CALAIS = f"{EX}Calais"
DOVER = f"{EX}Dover"
LOCATED_IN = f"{EX}locatedIn"
PURSE_PROP = f"{EX}purseProp"
SHAPES = f"{EX}arrivalShapes"


class PortalStore(GatedStore):
    """The gated rdflib store, which can also hand back an arbitrary graph (the
    boundary shapes) and drops the scratch graphs the SHACL path creates."""

    async def get_graph(self, conn, graph_iri: str) -> str:
        if graph_iri == conn.shapes_graph:
            return self.shapes
        return self.ds.graph(URIRef(graph_iri)).serialize(format="turtle")


def _add(g, s, p, o):
    g.add((URIRef(s), URIRef(p), o if not isinstance(o, str) else URIRef(o)))


def _world(*, passport: bool = True):
    conn, store = _conn(), PortalStore()
    holons = store.ds.graph(URIRef(conn.holons_graph))
    onto = store.ds.graph(URIRef(conn.ontology_graph))
    scene = store.ds.graph(URIRef(conn.scene_graph))
    events = store.ds.graph(URIRef(conn.graph("events")))

    _add(holons, LOC, f"{HOLON}fluentProperty", LOCATED_IN)
    _add(holons, LOC, RDF.type, f"{HOLON}Fluent")
    _add(holons, ANN, f"{EX}position", LOC)
    _add(holons, PURSE, f"{HOLON}fluentProperty", PURSE_PROP)
    _add(holons, ANN, f"{EX}purse", PURSE)
    if passport:
        _add(holons, ANN, f"{EX}hasPassport", Literal(True))
    _add(onto, LOCATED_IN, f"{HOLON}fluentOperationMode", f"{HOLON}SetValueMode")
    _add(onto, PURSE_PROP, f"{HOLON}fluentOperationMode", f"{HOLON}NumericAccumulator")

    ten = Literal("10", datatype=XSD.decimal)
    _add(scene, LOC, f"{HOLON}currentValue", URIRef(CALAIS))
    _add(scene, LOC, f"{HOLON}currentAssertion", "urn:ex:assertion:seed-loc")
    _add(scene, PURSE, f"{HOLON}currentValue", ten)
    _add(scene, PURSE, f"{HOLON}currentAssertion", "urn:ex:assertion:seed-purse")
    for assertion, fluent, value in (
        ("urn:ex:assertion:seed-loc", LOC, URIRef(CALAIS)),
        ("urn:ex:assertion:seed-purse", PURSE, ten),
    ):
        _add(events, assertion, f"{HEVT}forFluent", fluent)
        _add(events, assertion, f"{HEVT}hasValue", value)
    return conn, store


def _portal(conn, **over) -> Portal:
    base = dict(
        iri=portal_node(conn, "customs"), id="customs", from_holon=CALAIS, to_holon=DOVER,
        position_property=LOCATED_IN, lock="has-passport", effect="pay-toll",
    )
    base.update(over)
    return Portal(**base)


def _queries(conn, **extra):
    holons = conn.holons_graph
    focus = Parameter(name="focus", datatype="http://www.w3.org/2001/XMLSchema#anyURI")
    queries = {
        "has-passport": NamedQuery(
            id="has-passport", iri="urn:q:1", vocabulary="hb", params=[focus],
            sparql=f"SELECT ?ok WHERE {{ GRAPH <{holons}> {{ {{{{focus}}}} <{EX}hasPassport> ?ok }} }}",
        ),
        "pay-toll": NamedQuery(
            id="pay-toll", iri="urn:q:2", vocabulary="hb", params=[focus],
            sparql=(
                f"SELECT ?fluent ?value ?operation ?description WHERE {{ GRAPH <{holons}> {{ "
                f"{{{{focus}}}} <{EX}purse> ?fluent }} BIND(3 AS ?value) BIND(\"Remove\" AS ?operation) "
                f"BIND(\"toll\" AS ?description) }}"
            ),
        ),
        **extra,
    }
    return queries


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tx_mod, "_evaluate_state_triggers", noop)


@pytest.fixture
def registry(monkeypatch):
    holder = {}

    async def load(client, conn):
        return SimpleNamespace(by_id=lambda i: holder["queries"].get(i), queries=list(holder["queries"].values()))

    monkeypatch.setattr(portal_mod, "load_named_queries", load)
    return holder


def value_of(store, conn, fluent):
    q = f"SELECT ?v WHERE {{ GRAPH <{conn.scene_graph}> {{ <{fluent}> <{HOLON}currentValue> ?v }} }}"
    rows = list(store.ds.query(q))
    return str(rows[0].v) if rows else None


async def cross(store, conn, portal, **kw):
    return await traverse(store, conn, portal, traveller=kw.pop("traveller", ANN), asserted_by=ALICE, **kw)


# --- crossing ------------------------------------------------------------------


async def test_a_crossing_moves_the_traveller_and_takes_the_toll_together(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)

    crossing = await cross(store, conn, _portal(conn))

    assert value_of(store, conn, LOC) == DOVER
    assert Decimal(value_of(store, conn, PURSE)) == 7
    assert crossing.effects_applied == 1
    assert [r.fluent for r in crossing.transaction.results] == [LOC, PURSE]

    q = f"""SELECT ?p ?t WHERE {{ GRAPH <{conn.graph("events")}> {{
      <{crossing.transaction.transaction}> <{HEVT}viaPortal> ?p ; <{HEVT}traveller> ?t }} }}"""
    (row,) = list(store.ds.query(q))
    assert (str(row.p), str(row.t)) == (portal_node(conn, "customs"), ANN)


async def test_a_portal_with_no_lock_or_effect_just_moves_the_traveller(registry):
    conn, store = _world(passport=False)
    registry["queries"] = _queries(conn)
    crossing = await cross(store, conn, _portal(conn, lock="", effect=""))
    assert value_of(store, conn, LOC) == DOVER
    assert crossing.effects_applied == 0
    assert Decimal(value_of(store, conn, PURSE)) == 10


async def test_a_traveller_somewhere_else_cannot_use_the_portal(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    await cross(store, conn, _portal(conn))
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn))  # now in Dover, not Calais
    assert caught.value.code == "not_at_origin" and caught.value.status == 409
    assert Decimal(value_of(store, conn, PURSE)) == 7  # the toll was taken once


async def test_a_suspended_portal_refuses(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn, status="Suspended"))
    assert caught.value.code == "portal_suspended"
    assert value_of(store, conn, LOC) == CALAIS


async def test_a_traveller_with_no_position_fluent_is_refused(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn), traveller=f"{EX}nobody")
    assert caught.value.code == "traveller_has_no_position"


async def test_two_position_fluents_are_ambiguous(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    holons = store.ds.graph(URIRef(conn.holons_graph))
    _add(holons, f"{EX}ann-loc2", f"{HOLON}fluentProperty", LOCATED_IN)
    _add(holons, ANN, f"{EX}position2", f"{EX}ann-loc2")
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn))
    assert caught.value.code == "traveller_position_ambiguous"


# --- the lock ------------------------------------------------------------------


async def test_a_lock_with_no_rows_keeps_the_door_shut(registry):
    conn, store = _world(passport=False)
    registry["queries"] = _queries(conn)
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn))
    assert caught.value.code == "locked" and caught.value.status == 403
    assert value_of(store, conn, LOC) == CALAIS
    assert Decimal(value_of(store, conn, PURSE)) == 10


async def test_a_lock_can_say_no_and_why(registry):
    conn, store = _world()
    registry["queries"] = _queries(
        conn,
        **{
            "curfew": NamedQuery(
                id="curfew", iri="urn:q:3", vocabulary="hb",
                sparql='SELECT ?open ?reason WHERE { BIND(false AS ?open) BIND("closed after dark" AS ?reason) }',
            )
        },
    )
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn, lock="curfew"))
    assert caught.value.code == "locked"
    assert "closed after dark" in caught.value.message and caught.value.extra["reason"] == "closed after dark"


async def test_an_unknown_lock_query_is_a_conflict_not_an_open_door(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn, lock="gone"))
    assert caught.value.code == "unknown_lock" and caught.value.status == 409
    assert value_of(store, conn, LOC) == CALAIS


# --- the boundary --------------------------------------------------------------

ARRIVAL_SHAPES = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <{EX}> .
ex:ArrivalShape a sh:NodeShape ;
    sh:targetClass ex:Traveller ;
    sh:property [ sh:path ex:declaredGoods ; sh:minCount 1 ; sh:message "declare your goods" ] .
"""
DECLARED = f"@prefix ex: <{EX}> . ex:ann a ex:Traveller ; ex:declaredGoods \"tea\" ."
UNDECLARED = f"@prefix ex: <{EX}> . ex:ann a ex:Traveller ."


def _with_boundary(store):
    store.ds.graph(URIRef(SHAPES)).parse(data=ARRIVAL_SHAPES, format="turtle")


async def test_a_portal_with_boundary_shapes_needs_a_payload(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    _with_boundary(store)
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn, boundary_shapes=SHAPES))
    assert caught.value.code == "boundary_needs_payload"


async def test_a_payload_that_breaks_the_boundary_is_refused_with_the_violations(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    _with_boundary(store)
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn, boundary_shapes=SHAPES), payload_turtle=UNDECLARED)
    assert caught.value.code == "boundary_refused" and caught.value.status == 403
    assert caught.value.extra["violations"]
    assert value_of(store, conn, LOC) == CALAIS


async def test_a_payload_that_satisfies_the_boundary_crosses(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    _with_boundary(store)
    await cross(store, conn, _portal(conn, boundary_shapes=SHAPES), payload_turtle=DECLARED)
    assert value_of(store, conn, LOC) == DOVER


async def test_missing_boundary_shapes_are_a_conflict_not_a_pass(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn, boundary_shapes=SHAPES), payload_turtle=DECLARED)
    assert caught.value.code == "boundary_shapes_unusable"
    assert value_of(store, conn, LOC) == CALAIS


# --- atomicity -----------------------------------------------------------------


async def test_an_effect_that_cannot_apply_stops_the_move_too(registry):
    conn, store = _world()
    registry["queries"] = _queries(
        conn,
        **{
            "pay-toll": NamedQuery(
                id="pay-toll", iri="urn:q:2", vocabulary="hb",
                # a fluent whose property declares no operation mode
                sparql=f'SELECT ?fluent ?value WHERE {{ BIND(<{EX}unmodelled> AS ?fluent) BIND(1 AS ?value) }}',
            )
        },
    )
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn))
    assert caught.value.code == "crossing_refused"
    assert value_of(store, conn, LOC) == CALAIS  # not moved
    assert Decimal(value_of(store, conn, PURSE)) == 10


async def test_a_malformed_effect_row_stops_everything(registry):
    conn, store = _world()
    registry["queries"] = _queries(
        conn,
        **{
            "pay-toll": NamedQuery(
                id="pay-toll", iri="urn:q:2", vocabulary="hb",
                sparql=f"SELECT ?fluent WHERE {{ BIND(<{PURSE}> AS ?fluent) }}",  # no ?value
            )
        },
    )
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn))
    assert caught.value.code == "effect_invalid"
    assert value_of(store, conn, LOC) == CALAIS


async def test_an_effect_may_not_touch_the_position_fluent_again(registry):
    conn, store = _world()
    registry["queries"] = _queries(
        conn,
        **{
            "pay-toll": NamedQuery(
                id="pay-toll", iri="urn:q:2", vocabulary="hb",
                sparql=f"SELECT ?fluent ?value WHERE {{ BIND(<{LOC}> AS ?fluent) BIND(<{CALAIS}> AS ?value) }}",
            )
        },
    )
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn))
    assert caught.value.code == "crossing_refused" and "more than once" in caught.value.message
    assert value_of(store, conn, LOC) == CALAIS


async def test_the_same_traveller_cannot_cross_twice_concurrently(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    outcomes = await asyncio.gather(
        cross(store, conn, _portal(conn)), cross(store, conn, _portal(conn)), return_exceptions=True
    )
    done = [o for o in outcomes if not isinstance(o, Exception)]
    refused = [o for o in outcomes if isinstance(o, PortalError)]
    assert len(done) == 1 and len(refused) == 1
    assert refused[0].code == "not_at_origin"
    assert Decimal(value_of(store, conn, PURSE)) == 7  # one toll, not two


SHAPE_NO_DOVER = f"""
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix hevt: <https://w3id.org/holon/event/> .
@prefix ex: <{EX}> .
ex:NoDoverShape a sh:NodeShape ;
    sh:targetClass hevt:StateAssertion ;
    sh:property [ sh:path hevt:hasValue ; sh:not [ sh:hasValue ex:Dover ] ; sh:message "Dover is closed" ] .
"""


async def test_the_dataset_shapes_still_gate_a_crossing(registry):
    conn, store = _world()
    registry["queries"] = _queries(conn)
    store.shapes = SHAPE_NO_DOVER
    with pytest.raises(PortalError) as caught:
        await cross(store, conn, _portal(conn))
    assert caught.value.code == "crossing_refused"
    assert caught.value.extra["violations"]
    assert value_of(store, conn, LOC) == CALAIS and Decimal(value_of(store, conn, PURSE)) == 10


# --- the registry --------------------------------------------------------------


def _turtle(conn, **over):
    args = dict(
        node=portal_node(conn, "customs"), portal_id="customs", from_holon=CALAIS, to_holon=DOVER,
        position_property=LOCATED_IN, lock="has-passport", effect="pay-toll", boundary_shapes=SHAPES,
        status="Active", label="Customs", description="the border", registered_by=ALICE,
        registered_at="2026-11-13T00:00:00+00:00",
    )
    args.update(over)
    return render_portal_turtle(**args)


async def test_what_is_registered_is_what_is_loaded():
    conn, store = _world()
    registry = conn.graph("portals")
    await store.update(conn, registration_update(registry, portal_node(conn, "customs"), _turtle(conn)))

    result = await load_portals(store, conn)
    (portal,) = result.portals
    assert portal.as_dict() == {
        "iri": portal_node(conn, "customs"), "id": "customs", "from": CALAIS, "to": DOVER,
        "positionProperty": LOCATED_IN, "status": "Active", "lock": "has-passport",
        "effect": "pay-toll", "boundaryShapes": SHAPES, "label": "Customs",
        "description": "the border", "registeredBy": ALICE,
    }
    assert result.warnings == []


async def test_re_registering_replaces_and_status_and_delete_work():
    conn, store = _world()
    registry, node = conn.graph("portals"), portal_node(conn, "customs")
    await store.update(conn, registration_update(registry, node, _turtle(conn)))
    await store.update(conn, registration_update(registry, node, _turtle(conn, lock=None, effect=None, boundary_shapes=None)))
    (portal,) = (await load_portals(store, conn)).portals
    assert portal.lock == "" and portal.effect == "" and portal.boundary_shapes == ""

    await store.update(conn, status_update(registry, node, "Suspended"))
    assert (await load_portals(store, conn)).by_id("customs").status == "Suspended"

    await store.update(conn, deletion_update(registry, node))
    assert (await load_portals(store, conn)).portals == []


@pytest.mark.parametrize(
    "over, message",
    [
        ({"portal_id": "has space"}, "portal id"),
        ({"from_holon": "not an iri"}, "from"),
        ({"to_holon": CALAIS}, "two different holons"),
        ({"position_property": "x"}, "positionProperty"),
        ({"boundary_shapes": "<bad>"}, "boundaryShapes"),
        ({"status": "Open"}, "status"),
    ],
)
def test_a_bad_definition_is_refused(over, message):
    conn = _conn()
    with pytest.raises(PortalError, match=message):
        _turtle(conn, **over)


async def test_an_incomplete_registry_entry_is_skipped_with_a_warning():
    conn, store = _world()
    registry = conn.graph("portals")
    g = store.ds.graph(URIRef(registry))
    g.add((URIRef("urn:ex:half"), RDF.type, URIRef(f"{HB_NAMESPACE}Portal")))
    g.add((URIRef("urn:ex:half"), URIRef(f"{HB_NAMESPACE}id"), Literal("half")))
    result = await load_portals(store, conn)
    assert result.portals == [] and any("missing" in w for w in result.warnings)


# --- governing the position property --------------------------------------------


async def test_a_registered_portal_governs_its_position_property():
    conn, store = _world()
    await store.update(
        conn, registration_update(conn.graph("portals"), portal_node(conn, "customs"), _turtle(conn))
    )
    assert await governing_portals(store, conn, LOC) == ["customs"]
    assert await governing_portals(store, conn, PURSE) == []  # a different property
    assert await governing_portals(store, conn, f"{EX}unknown") == []


async def test_a_dataset_with_no_portals_governs_nothing():
    conn, store = _world()
    assert await governing_portals(store, conn, LOC) == []


# --- the fluent routes refuse a governed property ------------------------------------


async def _governed_world():
    conn, store = _world()
    await store.update(
        conn, registration_update(conn.graph("portals"), portal_node(conn, "customs"), _turtle(conn))
    )
    return conn, store


@pytest.fixture
def allowed_route(monkeypatch):
    from holonbridge.routes import fluent as fluent_routes

    async def allowed(*_a, **_k):
        return SimpleNamespace(allowed=True, matched_grant=None, reason="test")

    monkeypatch.setattr(fluent_routes, "check_write", allowed)


async def test_update_refuses_a_position_property_with_409(allowed_route):
    from fastapi import HTTPException

    from holonbridge.config import Settings
    from holonbridge.fluent import Operation
    from holonbridge.routes import fluent as fluent_routes

    conn, store = await _governed_world()
    body = fluent_routes.UpdateFluentRequest(fluent=LOC, operation=Operation.SET, value=DOVER, is_iri=True)
    with pytest.raises(HTTPException) as err:
        await fluent_routes.update(body, conn, store, SimpleNamespace(person=ALICE), Settings())
    assert err.value.status_code == 409
    assert err.value.detail["error"] == "portal_governed" and err.value.detail["portals"] == ["customs"]
    assert value_of(store, conn, LOC) == CALAIS


async def test_update_still_works_for_an_ungoverned_fluent(allowed_route):
    from holonbridge.config import Settings
    from holonbridge.fluent import Operation
    from holonbridge.routes import fluent as fluent_routes

    conn, store = await _governed_world()
    body = fluent_routes.UpdateFluentRequest(fluent=PURSE, operation=Operation.INSERT, value=5)
    await fluent_routes.update(body, conn, store, SimpleNamespace(person=ALICE), Settings())
    assert Decimal(value_of(store, conn, PURSE)) == 15


async def test_update_many_refuses_the_whole_batch_if_one_member_is_governed(allowed_route):
    from fastapi import HTTPException

    from holonbridge.config import Settings
    from holonbridge.fluent import Operation
    from holonbridge.routes import fluent as fluent_routes

    conn, store = await _governed_world()
    body = fluent_routes.UpdateFluentsRequest(
        operations=[
            fluent_routes.FluentOperationItem(fluent=PURSE, operation=Operation.INSERT, value=5),
            fluent_routes.FluentOperationItem(fluent=LOC, operation=Operation.SET, value=DOVER, is_iri=True),
        ]
    )
    with pytest.raises(HTTPException) as err:
        await fluent_routes.update_many(body, conn, store, SimpleNamespace(person=ALICE), Settings())
    assert err.value.status_code == 409 and err.value.detail["error"] == "portal_governed"
    assert Decimal(value_of(store, conn, PURSE)) == 10  # the ungoverned member was not applied either


# --- the portal routes ------------------------------------------------------------------


@pytest.fixture
def routes(monkeypatch, registry):
    """The portal routes with grants allowed; ``deny`` flips them off."""
    from holonbridge.routes import portals as pr
    from holonbridge.trigger_authz import TriggerDenied

    state = SimpleNamespace(deny=False)

    async def grant(*_a, **_k):
        if state.deny:
            raise TriggerDenied(403, "no grant", "no grant")

    async def reads(*_a, **_k):
        if state.deny:
            raise TriggerDenied(403, "cannot read", "cannot read")

    async def allowed(*_a, **_k):
        return SimpleNamespace(allowed=not state.deny, matched_grant=None, reason="test")

    async def load(client, conn):
        return SimpleNamespace(by_id=lambda i: registry["queries"].get(i), queries=list(registry["queries"].values()))

    monkeypatch.setattr(pr, "require_grant", grant)
    monkeypatch.setattr(pr, "authorize_reads", reads)
    monkeypatch.setattr(pr, "check_write", allowed)
    monkeypatch.setattr(pr, "load_named_queries", load)
    state.module = pr
    return state


def _register_body(**over):
    from holonbridge.routes.portals import RegisterPortalRequest

    args = dict(
        id="customs", **{"from": CALAIS, "to": DOVER}, position_property=LOCATED_IN,
        lock="has-passport", effect="pay-toll",
    )
    args.update(over)
    return RegisterPortalRequest(**args)


ANIMUS = SimpleNamespace(person=ALICE)


async def test_register_then_cross_over_http_handlers(routes, registry):
    from holonbridge.config import Settings
    from holonbridge.routes.portals import TraverseRequest

    conn, store = _world()
    registry["queries"] = _queries(conn)
    pr = routes.module

    out = await pr.register_portal(_register_body(), conn, store, ANIMUS)
    assert out["ok"] and out["warnings"] == []

    crossed = await pr.traverse_portal(
        "customs", TraverseRequest(traveller=ANN), conn, store, ANIMUS, Settings()
    )
    assert crossed["effectsApplied"] == 1 and crossed["assertedBy"] == ALICE
    assert {u["fluent"] for u in crossed["updates"]} == {LOC, PURSE}
    assert value_of(store, conn, LOC) == DOVER


async def test_registering_with_an_unknown_lock_is_a_400(routes, registry):
    from fastapi import HTTPException

    conn, store = _world()
    registry["queries"] = _queries(conn)
    with pytest.raises(HTTPException) as err:
        await routes.module.register_portal(_register_body(lock="nope"), conn, store, ANIMUS)
    assert err.value.status_code == 400 and err.value.detail["error"] == "unknown_lock"
    assert (await load_portals(store, conn)).portals == []


async def test_a_lock_that_ignores_the_traveller_is_warned_about(routes, registry):
    conn, store = _world()
    registry["queries"] = _queries(
        conn,
        **{"any": NamedQuery(id="any", iri="urn:q:9", vocabulary="hb", sparql="SELECT ?x WHERE { BIND(1 AS ?x) }")},
    )
    out = await routes.module.register_portal(_register_body(lock="any", effect=None), conn, store, ANIMUS)
    assert any("focus" in w for w in out["warnings"])


async def test_registering_without_a_grant_is_a_403_and_writes_nothing(routes, registry):
    from fastapi import HTTPException

    conn, store = _world()
    registry["queries"] = _queries(conn)
    routes.deny = True
    with pytest.raises(HTTPException) as err:
        await routes.module.register_portal(_register_body(), conn, store, ANIMUS)
    assert err.value.status_code == 403
    assert (await load_portals(store, conn)).portals == []


async def test_a_refused_crossing_carries_its_status_and_reason(routes, registry):
    from fastapi import HTTPException

    from holonbridge.config import Settings
    from holonbridge.routes.portals import TraverseRequest

    conn, store = _world(passport=False)
    registry["queries"] = _queries(conn)
    pr = routes.module
    await pr.register_portal(_register_body(), conn, store, ANIMUS)
    with pytest.raises(HTTPException) as err:
        await pr.traverse_portal("customs", TraverseRequest(traveller=ANN), conn, store, ANIMUS, Settings())
    assert err.value.status_code == 403 and err.value.detail["error"] == "locked"


async def test_crossing_needs_the_three_write_grants(routes, registry):
    from fastapi import HTTPException

    from holonbridge.config import Settings
    from holonbridge.routes.portals import TraverseRequest

    conn, store = _world()
    registry["queries"] = _queries(conn)
    pr = routes.module
    await pr.register_portal(_register_body(), conn, store, ANIMUS)
    routes.deny = True
    with pytest.raises(HTTPException) as err:
        await pr.traverse_portal("customs", TraverseRequest(traveller=ANN), conn, store, ANIMUS, Settings())
    assert err.value.status_code == 403
    assert value_of(store, conn, LOC) == CALAIS


async def test_crossing_cannot_be_attributed_to_someone_else(routes, registry):
    from fastapi import HTTPException

    from holonbridge.config import Settings
    from holonbridge.routes.portals import TraverseRequest

    conn, store = _world()
    registry["queries"] = _queries(conn)
    pr = routes.module
    await pr.register_portal(_register_body(), conn, store, ANIMUS)
    with pytest.raises(HTTPException) as err:
        await pr.traverse_portal(
            "customs", TraverseRequest(traveller=ANN, asserted_by="urn:ex:mallory"), conn, store, ANIMUS, Settings()
        )
    assert err.value.status_code == 400


async def test_unknown_portal_is_a_404_listing_what_exists(routes, registry):
    from fastapi import HTTPException

    conn, store = _world()
    registry["queries"] = _queries(conn)
    pr = routes.module
    await pr.register_portal(_register_body(), conn, store, ANIMUS)
    with pytest.raises(HTTPException) as err:
        await pr.get_portal("nowhere", conn, store, ANIMUS)
    assert err.value.status_code == 404


async def test_status_and_delete_handlers_and_unfreezing_the_property(routes, registry):
    from fastapi import HTTPException

    from holonbridge.config import Settings
    from holonbridge.routes.portals import StatusRequest, TraverseRequest

    conn, store = _world()
    registry["queries"] = _queries(conn)
    pr = routes.module
    await pr.register_portal(_register_body(), conn, store, ANIMUS)

    await pr.set_portal_status("customs", StatusRequest(status="Suspended"), conn, store, ANIMUS)
    with pytest.raises(HTTPException) as err:
        await pr.traverse_portal("customs", TraverseRequest(traveller=ANN), conn, store, ANIMUS, Settings())
    assert err.value.detail["error"] == "portal_suspended"
    assert await governing_portals(store, conn, LOC) == ["customs"]  # suspended still governs

    await pr.delete_portal("customs", conn, store, ANIMUS)
    assert await governing_portals(store, conn, LOC) == []
    listed = await pr.list_portals(conn, store, ANIMUS, portal_status=None)
    assert listed["count"] == 0 and listed["portals"] == []


# --- a fluent trigger's rows are one transaction -------------------------------------


def _action(rows_sparql: str) -> NamedQuery:
    return NamedQuery(id="act", iri="urn:q:act", vocabulary="hb", sparql=rows_sparql)


async def test_a_fluent_trigger_applies_its_rows_all_or_none():
    from holonbridge.fluent_trigger_action import FluentActionInvalid, fire_fluent

    conn, store = _world()
    trig = SimpleNamespace(id="move", armed_by=ALICE)

    good = _action(
        f"SELECT ?fluent ?value ?operation ?isIri WHERE {{ "
        f"{{ BIND(<{LOC}> AS ?fluent) BIND(\"{DOVER}\" AS ?value) BIND(\"Set\" AS ?operation) BIND(true AS ?isIri) }} UNION "
        f"{{ BIND(<{PURSE}> AS ?fluent) BIND(3 AS ?value) BIND(\"Remove\" AS ?operation) BIND(false AS ?isIri) }} }}"
    )
    outcome = await fire_fluent(store, conn, trig, ANN, action_query=good)
    assert (outcome.applied, outcome.total) == (2, 2)
    assert value_of(store, conn, LOC) == DOVER and Decimal(value_of(store, conn, PURSE)) == 7

    # The second row cannot apply (no operation mode declared), so the first must not either.
    bad = _action(
        f"SELECT ?fluent ?value ?operation ?isIri WHERE {{ "
        f"{{ BIND(<{LOC}> AS ?fluent) BIND(\"{CALAIS}\" AS ?value) BIND(\"Set\" AS ?operation) BIND(true AS ?isIri) }} UNION "
        f"{{ BIND(<{EX}unmodelled> AS ?fluent) BIND(1 AS ?value) BIND(\"Set\" AS ?operation) BIND(false AS ?isIri) }} }}"
    )
    with pytest.raises(FluentActionInvalid, match="none of the 2"):
        await fire_fluent(store, conn, trig, ANN, action_query=bad)
    assert value_of(store, conn, LOC) == DOVER  # not moved back
