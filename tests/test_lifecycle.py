"""Holon lifecycle: Active, Inactive, Tombstoned.

Real ``set_state``, ``update_fluent(s)``, ``sequence.mint``, the neighbour query and
``get_holon`` against rdflib's SPARQL engine. Only identity and the grant check are
stubbed, as in ``test_portal``. The world:

    league
      |-- team   (ex:memberOf league)   team --ex:score--> <team-score>   (numeric, 10)
      |-- other  (ex:memberOf league)   other --ex:score--> <other-score> (numeric, 5)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

pytest.importorskip("pyshacl")

from holonbridge import fluent as fluent_mod
from holonbridge import fluent_transaction as tx_mod
from holonbridge import lifecycle as lc
from holonbridge import portal as portal_mod
from holonbridge.config import Settings
from holonbridge.fluent import FluentError, Operation
from holonbridge.fluent_trigger_action import FluentActionInvalid, fire_fluent
from holonbridge.holon import get_holon
from holonbridge.named_queries import NamedQuery
from holonbridge.params import Parameter
from holonbridge.persona_scope import build_neighbour_query, resolve_scope_graphs
from holonbridge.portal import Portal, PortalError, portal_node, traverse
from holonbridge.routes import fluent as fluent_routes
from holonbridge.routes import lifecycle as lifecycle_routes

from .test_fluent_gate_scope import GatedStore
from .test_fluent_ledger_race import ALICE, HEVT, HOLON, _conn

EX = "urn:ex:"
TEAM = f"{EX}team"
OTHER = f"{EX}other"
LEAGUE = f"{EX}league"
SCORE = f"{EX}team-score"
OTHER_SCORE = f"{EX}other-score"
SCORE_PROP = f"{EX}scoreProp"
MEMBER_OF = f"{EX}memberOf"


class Store(GatedStore):
    """GatedStore plus ASK: the production client returns ``{"boolean": ...}`` for an
    ASK query through the same ``select`` call, which the base stand-in does not model."""

    async def select(self, conn, query: str) -> dict:
        result = self.ds.query(query)
        if result.type == "ASK":
            await asyncio.sleep(0)
            return {"head": {}, "boolean": bool(result.askAnswer)}
        return await super().select(conn, query)


@pytest.fixture(autouse=True)
def _no_triggers(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(fluent_mod, "_evaluate_state_triggers", noop)
    monkeypatch.setattr(tx_mod, "_evaluate_state_triggers", noop)


def _add(g, s, p, o):
    g.add((URIRef(s), URIRef(p), o if not isinstance(o, str) else URIRef(o)))


def _world():
    conn, store = _conn(), Store()
    holons = store.ds.graph(URIRef(conn.holons_graph))
    onto = store.ds.graph(URIRef(conn.ontology_graph))
    scene = store.ds.graph(URIRef(conn.scene_graph))
    events = store.ds.graph(URIRef(conn.graph("events")))

    for holon, label in ((TEAM, "Team"), (OTHER, "Other"), (LEAGUE, "League")):
        _add(holons, holon, RDFS.label, Literal(label))
    _add(holons, TEAM, MEMBER_OF, LEAGUE)
    _add(holons, OTHER, MEMBER_OF, LEAGUE)
    _add(onto, MEMBER_OF, RDFS.subPropertyOf, f"{HOLON}isPartOf")
    _add(onto, SCORE_PROP, f"{HOLON}fluentOperationMode", f"{HOLON}NumericAccumulator")

    for holon, fluent, value, seed in (
        (TEAM, SCORE, 10, "urn:ex:assertion:seed-team"),
        (OTHER, OTHER_SCORE, 5, "urn:ex:assertion:seed-other"),
    ):
        number = Literal(str(value), datatype=XSD.decimal)
        _add(holons, fluent, RDF.type, f"{HOLON}Fluent")
        _add(holons, fluent, f"{HOLON}fluentProperty", SCORE_PROP)
        _add(holons, holon, f"{EX}score", fluent)
        _add(scene, fluent, f"{HOLON}currentValue", number)
        _add(scene, fluent, f"{HOLON}currentAssertion", seed)
        _add(events, seed, f"{HEVT}forFluent", fluent)
        _add(events, seed, f"{HEVT}hasValue", number)
    return conn, store


def score_of(store, conn, fluent=SCORE) -> Decimal:
    q = f"SELECT ?v WHERE {{ GRAPH <{conn.scene_graph}> {{ <{fluent}> <{HOLON}currentValue> ?v }} }}"
    return Decimal(str(next(iter(store.ds.query(q))).v))


def ledger_entries(store, conn, fluent: str) -> int:
    q = f"SELECT ?a WHERE {{ GRAPH <{conn.graph('events')}> {{ ?a <{HEVT}forFluent> <{fluent}> }} }}"
    return len(list(store.ds.query(q)))


async def go(store, conn, state, holon=TEAM, **kw):
    return await lc.set_state(store, conn, holon=holon, state=state, asserted_by=ALICE, **kw)


@pytest.fixture
def grants(monkeypatch):
    """Allow every write and record which graphs were asked about."""
    asked: list[str] = []

    async def allowed(_query_fn, _holons, *, person, target):
        asked.append(target)
        return SimpleNamespace(allowed=True, matched_grant=None, reason="test")

    monkeypatch.setattr(lifecycle_routes, "check_write", allowed)

    async def fluent_allowed(*_a, **_k):
        return SimpleNamespace(allowed=True, matched_grant=None, reason="test")

    monkeypatch.setattr(fluent_routes, "check_write", fluent_allowed)
    return asked


# --- the state ---------------------------------------------------------------------


async def test_a_holon_that_never_changed_state_is_active():
    conn, store = _world()
    state = await lc.read_state(store, conn, TEAM)
    assert (state.state, state.fluent, state.assertion) == ("Active", None, None)


async def test_making_a_holon_inactive_records_it_on_the_ledger():
    conn, store = _world()
    change = await go(store, conn, "Inactive", reason="season over")

    assert (change.previous, change.state, change.changed) == ("Active", "Inactive", True)
    state = await lc.read_state(store, conn, TEAM)
    assert state.state == "Inactive" and state.fluent == change.fluent
    assert state.assertion == change.assertion and state.since is not None

    q = f"""SELECT ?d WHERE {{ GRAPH <{conn.graph("events")}> {{
      <{change.assertion}> <{HOLON}description> ?d ; <{HEVT}assertedBy> <{ALICE}> }} }}"""
    (row,) = list(store.ds.query(q))
    assert str(row.d) == "lifecycle: Inactive (season over)"


async def test_the_first_change_declares_the_property_and_links_the_fluent():
    conn, store = _world()
    assert not await lc.vocabulary_declared(store, conn)
    change = await go(store, conn, "Inactive")
    assert await lc.vocabulary_declared(store, conn)

    holons = store.ds.graph(URIRef(conn.holons_graph))
    assert (URIRef(TEAM), URIRef(lc.LIFECYCLE_PROPERTY), URIRef(change.fluent)) in holons
    assert (URIRef(change.fluent), RDF.type, URIRef(f"{HOLON}Fluent")) in holons
    assert (
        URIRef(change.fluent),
        URIRef(f"{HOLON}fluentProperty"),
        URIRef(lc.LIFECYCLE_PROPERTY),
    ) in holons


async def test_asking_for_the_state_a_holon_is_in_changes_nothing():
    conn, store = _world()
    already_active = await go(store, conn, "Active")
    assert already_active.changed is False and already_active.fluent is None
    assert not await lc.vocabulary_declared(store, conn)  # nothing was written at all

    first = await go(store, conn, "Inactive")
    again = await go(store, conn, "Inactive")
    assert again.changed is False and again.assertion == first.assertion
    assert ledger_entries(store, conn, first.fluent) == 1


async def test_every_transition_is_allowed_and_reversible():
    conn, store = _world()
    path = ["Inactive", "Tombstoned", "Inactive", "Active", "Tombstoned", "Active"]
    previous = "Active"
    for state in path:
        change = await go(store, conn, state)
        assert (change.previous, change.state, change.changed) == (previous, state, True)
        assert (await lc.read_state(store, conn, TEAM)).state == state
        previous = state
    assert ledger_entries(store, conn, change.fluent) == len(path)


async def test_the_lifecycle_fluent_is_stable_and_one_per_holon():
    conn, store = _world()
    assert lc.lifecycle_fluent_iri(conn, TEAM) == lc.lifecycle_fluent_iri(conn, TEAM)
    assert lc.lifecycle_fluent_iri(conn, TEAM) != lc.lifecycle_fluent_iri(conn, OTHER)
    first = await go(store, conn, "Inactive")
    second = await go(store, conn, "Active")
    assert first.fluent == second.fluent == lc.lifecycle_fluent_iri(conn, TEAM)


async def test_an_unknown_holon_and_an_unknown_state_are_refused():
    conn, store = _world()
    with pytest.raises(lc.LifecycleError) as unknown:
        await go(store, conn, "Inactive", holon=f"{EX}nobody")
    assert unknown.value.code == "unknown_holon" and unknown.value.status == 404
    with pytest.raises(lc.LifecycleError) as bad:
        await go(store, conn, "Closed")
    assert bad.value.code == "bad_state" and bad.value.status == 400
    assert not await lc.vocabulary_declared(store, conn)


async def test_a_backdated_change_keeps_its_valid_time():
    conn, store = _world()
    when = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    change = await go(store, conn, "Inactive", asserted_datetime=when)
    q = f"""SELECT ?at WHERE {{ GRAPH <{conn.graph("events")}> {{
      <{change.assertion}> <{HEVT}assertedDateTime> ?at }} }}"""
    (row,) = list(store.ds.query(q))
    assert datetime.fromisoformat(str(row.at)) == when


# --- the guard ---------------------------------------------------------------------


async def test_an_active_holon_has_no_write_block():
    conn, store = _world()
    assert await lc.write_block(store, conn, SCORE) is None
    await go(store, conn, "Inactive")
    await go(store, conn, "Active")
    assert await lc.write_block(store, conn, SCORE) is None


async def test_an_inactive_holons_fluent_is_blocked_and_names_its_holder():
    conn, store = _world()
    await go(store, conn, "Inactive")
    block = await lc.write_block(store, conn, SCORE)
    assert block is not None and block.kind == "inactive" and block.error == "holon_inactive"
    assert block.holders == (TEAM,) and block.state == "Inactive"
    assert TEAM in block.message
    assert await lc.write_block(store, conn, OTHER_SCORE) is None  # a different holon


async def test_a_tombstoned_holons_fluent_is_blocked_too():
    conn, store = _world()
    await go(store, conn, "Tombstoned")
    block = await lc.write_block(store, conn, SCORE)
    assert block is not None and block.state == "Tombstoned"


async def test_the_lifecycle_fluent_itself_is_governed_even_when_active():
    conn, store = _world()
    change = await go(store, conn, "Inactive")
    await go(store, conn, "Active")
    block = await lc.write_block(store, conn, change.fluent)
    assert block is not None and block.kind == "governed" and block.error == "lifecycle_governed"


async def test_a_fluent_with_several_holders_is_blocked_if_any_is_inactive():
    conn, store = _world()
    holons = store.ds.graph(URIRef(conn.holons_graph))
    _add(holons, OTHER, f"{EX}shared", SCORE)  # SCORE now has two holders
    await go(store, conn, "Inactive", holon=OTHER)
    block = await lc.write_block(store, conn, SCORE)
    assert block is not None and block.holders == (OTHER,)


# --- the fluent routes -------------------------------------------------------------


async def update(store, conn, fluent, value=5, op=Operation.INSERT, **kw):
    body = fluent_routes.UpdateFluentRequest(fluent=fluent, operation=op, value=value, **kw)
    return await fluent_routes.update(body, conn, store, SimpleNamespace(person=ALICE), Settings())


async def test_update_refuses_an_inactive_holons_fluent_with_409(grants):
    conn, store = _world()
    await go(store, conn, "Inactive")
    with pytest.raises(HTTPException) as err:
        await update(store, conn, SCORE)
    assert err.value.status_code == 409
    assert err.value.detail["error"] == "holon_inactive" and err.value.detail["holders"] == [TEAM]
    assert score_of(store, conn) == 10


async def test_update_works_again_once_the_holon_is_active(grants):
    conn, store = _world()
    await go(store, conn, "Inactive")
    await go(store, conn, "Active")
    await update(store, conn, SCORE)
    assert score_of(store, conn) == 15


async def test_update_still_works_for_another_holon(grants):
    conn, store = _world()
    await go(store, conn, "Tombstoned")
    await update(store, conn, OTHER_SCORE)
    assert score_of(store, conn, OTHER_SCORE) == 10


async def test_update_refuses_the_lifecycle_fluent_itself(grants):
    conn, store = _world()
    change = await go(store, conn, "Inactive")
    with pytest.raises(HTTPException) as err:
        await update(store, conn, change.fluent, value=lc.ACTIVE, op=Operation.SET, is_iri=True)
    assert err.value.status_code == 409 and err.value.detail["error"] == "lifecycle_governed"
    assert (await lc.read_state(store, conn, TEAM)).state == "Inactive"


async def test_update_many_refuses_the_whole_batch_if_one_member_is_inactive(grants):
    conn, store = _world()
    await go(store, conn, "Inactive")
    body = fluent_routes.UpdateFluentsRequest(
        operations=[
            fluent_routes.FluentOperationItem(
                fluent=OTHER_SCORE, operation=Operation.INSERT, value=5
            ),
            fluent_routes.FluentOperationItem(fluent=SCORE, operation=Operation.INSERT, value=5),
        ]
    )
    with pytest.raises(HTTPException) as err:
        await fluent_routes.update_many(
            body, conn, store, SimpleNamespace(person=ALICE), Settings()
        )
    assert err.value.status_code == 409 and err.value.detail["error"] == "holon_inactive"
    assert (
        score_of(store, conn, OTHER_SCORE) == 5
    )  # the active holon's member was not applied either


# --- a trigger's fluent action ---------------------------------------------------------


def _action(fluent: str) -> NamedQuery:
    focus = Parameter(name="focus", datatype="http://www.w3.org/2001/XMLSchema#anyURI")
    return NamedQuery(
        id="bump",
        iri="urn:q:bump",
        vocabulary="hb",
        params=[focus],
        sparql=(
            "SELECT ?fluent ?value ?operation ?description WHERE { "
            f'BIND(<{fluent}> AS ?fluent) BIND(1 AS ?value) BIND("Insert" AS ?operation) '
            'BIND("bump" AS ?description) } '
        ),
    )


async def test_a_trigger_action_cannot_change_an_inactive_holons_fluent():
    conn, store = _world()
    await go(store, conn, "Inactive")
    trig = SimpleNamespace(id="bump", armed_by=ALICE)
    with pytest.raises(FluentActionInvalid) as err:
        await fire_fluent(store, conn, trig, TEAM, action_query=_action(SCORE))
    assert "Inactive" in str(err.value) and TEAM in str(err.value)
    assert score_of(store, conn) == 10


async def test_a_trigger_action_still_changes_an_active_holons_fluent():
    conn, store = _world()
    await go(store, conn, "Inactive", holon=OTHER)
    trig = SimpleNamespace(id="bump", armed_by=ALICE)
    outcome = await fire_fluent(store, conn, trig, TEAM, action_query=_action(SCORE))
    assert outcome.applied == 1 and score_of(store, conn) == 11


# --- a portal crossing ---------------------------------------------------------------


async def test_a_crossing_is_refused_for_an_inactive_traveller():
    conn, store = _world()
    holons = store.ds.graph(URIRef(conn.holons_graph))
    onto = store.ds.graph(URIRef(conn.ontology_graph))
    scene = store.ds.graph(URIRef(conn.scene_graph))
    where, lands = f"{EX}Calais", f"{EX}Dover"
    loc, loc_prop = f"{EX}team-loc", f"{EX}locatedIn"
    _add(holons, loc, f"{HOLON}fluentProperty", loc_prop)
    _add(holons, loc, RDF.type, f"{HOLON}Fluent")
    _add(holons, TEAM, f"{EX}position", loc)
    _add(onto, loc_prop, f"{HOLON}fluentOperationMode", f"{HOLON}SetValueMode")
    _add(scene, loc, f"{HOLON}currentValue", URIRef(where))
    portal = Portal(
        iri=portal_node(conn, "gate"),
        id="gate",
        from_holon=where,
        to_holon=lands,
        position_property=loc_prop,
        lock="",
        effect="",
    )
    await go(store, conn, "Inactive")

    with pytest.raises(PortalError) as err:
        await traverse(store, conn, portal, traveller=TEAM, asserted_by=ALICE)
    assert err.value.code == "holon_inactive" and err.value.status == 409
    assert (
        str(
            next(
                iter(
                    store.ds.query(
                        f"SELECT ?v WHERE {{ GRAPH <{conn.scene_graph}> {{ <{loc}> <{HOLON}currentValue> ?v }} }}"
                    )
                )
            ).v
        )
        == where
    )

    await go(store, conn, "Active")
    await traverse(store, conn, portal, traveller=TEAM, asserted_by=ALICE)  # now it can cross
    assert portal_mod is not None


# --- POST /holon/lifecycle, GET /holon/lifecycle -------------------------------------------


async def post(store, conn, **body):
    request = lifecycle_routes.SetLifecycleRequest(**body)
    return await lifecycle_routes.set_lifecycle(
        request, conn, store, SimpleNamespace(person=ALICE), Settings()
    )


async def test_the_route_changes_state_and_attributes_it_to_the_caller(grants):
    conn, store = _world()
    out = await post(store, conn, holon=TEAM, state="Inactive", reason="season over")
    assert out["ok"] and out["state"] == "Inactive" and out["previousState"] == "Active"
    assert out["changed"] is True and out["assertedBy"] == ALICE
    assert (await lc.read_state(store, conn, TEAM)).state == "Inactive"


async def test_the_route_asks_for_the_ontology_grant_only_the_first_time(grants):
    conn, store = _world()
    await post(store, conn, holon=TEAM, state="Inactive")
    first = list(grants)
    grants.clear()
    await post(store, conn, holon=TEAM, state="Active")
    second = list(grants)

    base = {conn.graph("events"), conn.scene_graph, conn.graph("sequences"), conn.holons_graph}
    assert set(first) == base | {conn.ontology_graph}
    assert set(second) == base


async def test_the_route_refuses_without_a_grant_and_writes_nothing(monkeypatch):
    conn, store = _world()

    async def denied(_q, _h, *, person, target):
        ok = target != conn.graph("events")
        return SimpleNamespace(allowed=ok, matched_grant=None, reason="no grant")

    monkeypatch.setattr(lifecycle_routes, "check_write", denied)
    with pytest.raises(HTTPException) as err:
        await post(store, conn, holon=TEAM, state="Inactive")
    assert err.value.status_code == 403
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"
    assert not await lc.vocabulary_declared(store, conn)


async def test_the_route_needs_an_identity(grants):
    conn, store = _world()
    request = lifecycle_routes.SetLifecycleRequest(holon=TEAM, state="Inactive")
    with pytest.raises(HTTPException) as err:
        await lifecycle_routes.set_lifecycle(
            request, conn, store, SimpleNamespace(person=None), Settings()
        )
    assert err.value.status_code == 401


async def test_the_route_maps_an_unknown_holon_to_404(grants):
    conn, store = _world()
    with pytest.raises(HTTPException) as err:
        await post(store, conn, holon=f"{EX}nobody", state="Inactive")
    assert err.value.status_code == 404 and err.value.detail["error"] == "unknown_holon"


async def test_the_route_refuses_an_unsafe_iri(grants):
    conn, store = _world()
    with pytest.raises(HTTPException) as err:
        await post(store, conn, holon="urn:ex:team> } DROP ALL #", state="Inactive")
    assert err.value.status_code == 400


async def test_the_state_state_is_a_closed_set():
    with pytest.raises(ValueError):
        lifecycle_routes.SetLifecycleRequest(holon=TEAM, state="Closed")


async def test_the_get_route_reads_the_state(grants):
    conn, store = _world()
    await go(store, conn, "Tombstoned")
    out = await lifecycle_routes.get_lifecycle(conn, store, SimpleNamespace(person=ALICE), iri=TEAM)
    assert out["state"] == "Tombstoned" and out["holon"] == TEAM
    with pytest.raises(HTTPException) as err:
        await lifecycle_routes.get_lifecycle(
            conn, store, SimpleNamespace(person=ALICE), iri=f"{EX}nobody"
        )
    assert err.value.status_code == 404


# --- discovery -----------------------------------------------------------------------


def children_of_league(store, conn, *, hide: bool) -> set[str]:
    scope = resolve_scope_graphs(conn, person_id=None, persona=None)
    query = build_neighbour_query(
        conn, LEAGUE, scope, root="isPartOf", direction="down", hide_tombstoned=hide
    )
    return {str(row.neighbour) for row in store.ds.query(query)}


async def test_discovery_leaves_out_tombstoned_holons_only_when_asked():
    conn, store = _world()
    assert children_of_league(store, conn, hide=True) == {TEAM, OTHER}
    await go(store, conn, "Tombstoned", holon=OTHER)
    assert children_of_league(store, conn, hide=True) == {TEAM}
    assert children_of_league(store, conn, hide=False) == {TEAM, OTHER}  # off by default


async def test_an_inactive_holon_is_still_discovered():
    conn, store = _world()
    await go(store, conn, "Inactive", holon=OTHER)
    assert children_of_league(store, conn, hide=True) == {TEAM, OTHER}


async def test_a_holon_that_is_active_again_is_discovered_again():
    conn, store = _world()
    await go(store, conn, "Tombstoned", holon=OTHER)
    await go(store, conn, "Inactive", holon=OTHER)
    assert children_of_league(store, conn, hide=True) == {TEAM, OTHER}


# --- get_holon ------------------------------------------------------------------------


async def read(store, conn, iri, **kw):
    scope = resolve_scope_graphs(conn, person_id=None, persona=None)
    return await get_holon(store, conn, holon_iri=iri, scope=scope, include_shapes=False, **kw)


async def test_get_holon_reports_the_lifecycle_state():
    conn, store = _world()
    assert (await read(store, conn, TEAM)).frontmatter["lifecycle"]["state"] == "Active"
    await go(store, conn, "Inactive")
    book = await read(store, conn, TEAM)
    assert book.frontmatter["lifecycle"]["state"] == "Inactive"
    assert book.frontmatter["lifecycle"]["since"] is not None


async def test_get_holon_hides_tombstoned_children_unless_asked():
    conn, store = _world()
    await go(store, conn, "Tombstoned", holon=OTHER)

    hidden = await read(store, conn, LEAGUE)
    assert hidden.frontmatter["projection"]["children"] == 1
    assert hidden.frontmatter["projection"]["tombstonedHidden"] is True

    shown = await read(store, conn, LEAGUE, include_tombstoned=True)
    assert shown.frontmatter["projection"]["children"] == 2
    assert shown.frontmatter["projection"]["tombstonedHidden"] is False


async def test_a_tombstoned_holon_can_still_be_read_directly():
    conn, store = _world()
    await go(store, conn, "Tombstoned")
    book = await read(store, conn, TEAM)
    assert book.frontmatter["lifecycle"]["state"] == "Tombstoned"
    assert "team-score" in book.render()  # its state is there


async def test_an_as_of_read_says_its_lifecycle_note_is_current_state():
    conn, store = _world()
    await go(store, conn, "Inactive", asserted_datetime=datetime(2026, 11, 1, tzinfo=timezone.utc))
    book = await read(store, conn, TEAM, observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert book.frontmatter["lifecycle"]["state"] == "Inactive"
    assert "not as of observed_at" in book.frontmatter["lifecycle"]["note"]


# --- fluent errors surface as lifecycle errors ----------------------------------------------


async def test_a_gate_refusal_is_a_lifecycle_error(monkeypatch):
    conn, store = _world()

    async def refuse(*_a, **_k):
        raise FluentError("the shapes graph said no")

    monkeypatch.setattr(lc, "update_fluent", refuse)
    with pytest.raises(lc.LifecycleError) as err:
        await go(store, conn, "Inactive")
    assert err.value.code == "fluent_error" and "shapes graph" in str(err.value)
