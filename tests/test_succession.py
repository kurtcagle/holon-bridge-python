"""Succession: close a holon and open a successor that carries its final state.

Real ``succeed``, ``set_state``, ``update_fluents`` and the route against rdflib's SPARQL
engine, with the world from ``test_lifecycle`` (``team`` has a numeric score of 10).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

pytest.importorskip("pyshacl")

from holonbridge import fluent_transaction as tx_mod
from holonbridge import lifecycle as lc
from holonbridge import succession as sc
from holonbridge.fluent import FluentError
from holonbridge.routes import lifecycle as lifecycle_routes

from .test_fluent_ledger_race import ALICE, HEVT, HOLON
from .test_lifecycle import (  # noqa: F401  (autouse fixture is re-exported for this module)
    EX,
    LEAGUE,
    MEMBER_OF,
    OTHER,
    SCORE,
    SCORE_PROP,
    TEAM,
    _add,
    _no_triggers,
    _world,
    go,
    score_of,
)

NEXT = f"{EX}team-2027"
G = lambda store, graph: store.ds.graph(URIRef(graph))


async def succeed(store, conn, **kw):
    args = {"holon": TEAM, "successor": NEXT, "asserted_by": ALICE}
    args.update(kw)
    return await sc.succeed(store, conn, **args)


def next_score(store, conn) -> Decimal | None:
    q = f"""SELECT ?v WHERE {{ GRAPH <{conn.holons_graph}> {{ <{NEXT}> <{EX}score> ?f }}
        GRAPH <{conn.scene_graph}> {{ ?f <{HOLON}currentValue> ?v }} }}"""
    rows = list(store.ds.query(q))
    return Decimal(str(rows[0].v)) if rows else None


# --- the happy path --------------------------------------------------------------------


async def test_the_predecessor_is_frozen_and_linked_to_its_successor():
    conn, store = _world()
    out = await succeed(store, conn, reason="season over")

    assert (out.holon, out.successor, out.previous_state, out.state) == (
        TEAM,
        NEXT,
        "Active",
        "Inactive",
    )
    assert (await lc.read_state(store, conn, TEAM)).state == "Inactive"
    holons = G(store, conn.holons_graph)
    assert (URIRef(TEAM), URIRef(sc.SUCCEEDED_BY), URIRef(NEXT)) in holons
    assert (URIRef(NEXT), URIRef(sc.PREDECESSOR), URIRef(TEAM)) in holons
    assert await lc.read_succession(store, conn, TEAM) == (NEXT, None)
    assert await lc.read_succession(store, conn, NEXT) == (None, TEAM)


async def test_the_successor_has_the_predecessors_structure():
    conn, store = _world()
    out = await succeed(store, conn)
    holons = G(store, conn.holons_graph)
    assert (
        URIRef(NEXT),
        RDFS.label,
        URIRef(f"{EX}Team"),
    ) not in holons  # world labels are literals
    assert (URIRef(NEXT), URIRef(MEMBER_OF), URIRef(LEAGUE)) in holons  # same parent
    assert any(holons.triples((URIRef(NEXT), RDFS.label, None)))
    assert out.copied_triples >= 2
    assert out.skipped_triples == 0
    # the predecessor keeps everything it had
    assert (URIRef(TEAM), URIRef(MEMBER_OF), URIRef(LEAGUE)) in holons
    assert (URIRef(TEAM), URIRef(f"{EX}score"), URIRef(SCORE)) in holons


async def test_the_final_fluent_value_is_carried_to_a_fluent_of_the_successors_own():
    conn, store = _world()
    out = await succeed(store, conn)

    assert next_score(store, conn) == Decimal(10)
    (item,) = out.fluents
    assert item["from"] == SCORE and item["to"] != SCORE and item["values"] == 1
    assert score_of(store, conn) == Decimal(10)  # the predecessor's own value is untouched
    holons = G(store, conn.holons_graph)
    assert (URIRef(item["to"]), URIRef(f"{HOLON}fluentProperty"), URIRef(SCORE_PROP)) in holons
    assert (URIRef(item["to"]), RDF.type, URIRef(f"{HOLON}Fluent")) in holons


async def test_the_successors_fluent_keeps_its_own_ledger_and_is_not_shared():
    conn, store = _world()
    out = await succeed(store, conn)
    target = out.fluents[0]["to"]
    q = f"""SELECT ?d WHERE {{ GRAPH <{conn.graph("events")}> {{
      ?a <{HEVT}forFluent> <{target}> ; <{HOLON}description> ?d }} }}"""
    (row,) = list(store.ds.query(q))
    assert str(row.d) == f"succession from <{TEAM}>"
    # and the old fluent's ledger is exactly what it was
    q = f"""SELECT ?a WHERE {{ GRAPH <{conn.graph("events")}> {{ ?a <{HEVT}forFluent> <{SCORE}> }} }}"""
    assert len(list(store.ds.query(q))) == 1


async def test_provenance_points_from_the_new_assertion_to_the_old_one():
    conn, store = _world()
    out = await succeed(store, conn)
    assert out.provenance_recorded is True
    q = f"""SELECT ?origin WHERE {{ GRAPH <{conn.graph("events")}> {{
      ?a <{HEVT}forFluent> <{out.fluents[0]["to"]}> ; <{sc.PROV}wasDerivedFrom> ?origin }} }}"""
    (row,) = list(store.ds.query(q))
    assert str(row.origin) == "urn:ex:assertion:seed-team"


async def test_a_label_can_be_given_to_the_successor():
    conn, store = _world()
    await succeed(store, conn, label="Team 2027")
    labels = {str(o) for o in G(store, conn.holons_graph).objects(URIRef(NEXT), RDFS.label)}
    assert labels == {"Team 2027"}


async def test_the_predecessor_can_be_tombstoned_instead():
    conn, store = _world()
    out = await succeed(store, conn, tombstone=True)
    assert out.state == "Tombstoned"
    assert (await lc.read_state(store, conn, TEAM)).state == "Tombstoned"


async def test_a_predecessor_that_is_already_inactive_stays_so():
    conn, store = _world()
    await go(store, conn, "Inactive")
    out = await succeed(store, conn)
    assert (out.previous_state, out.state) == ("Inactive", "Inactive")
    assert next_score(store, conn) == Decimal(10)


async def test_a_holon_with_no_fluents_succeeds_with_structure_only():
    conn, store = _world()
    out = await succeed(store, conn, holon=LEAGUE, successor=f"{EX}league-2")
    assert out.fluents == []
    assert (URIRef(f"{EX}league-2"), RDFS.label, None) in G(store, conn.holons_graph) or any(
        G(store, conn.holons_graph).triples((URIRef(f"{EX}league-2"), RDFS.label, None))
    )


async def test_the_successor_is_a_working_holon_of_its_own():
    conn, store = _world()
    out = await succeed(store, conn)
    # its fluent can be changed, and the predecessor's still cannot
    block_old = await lc.write_block(store, conn, SCORE)
    block_new = await lc.write_block(store, conn, out.fluents[0]["to"])
    assert block_old is not None and block_old.state == "Inactive"
    assert block_new is None


# --- list-mode fluents and datatypes ---------------------------------------------------------


def _add_list_fluent(store, conn, holon: str, members: list[str]) -> str:
    holons, onto, scene = (
        G(store, g) for g in (conn.holons_graph, conn.ontology_graph, conn.scene_graph)
    )
    fluent, prop = f"{EX}tags-{holon.rsplit(':', 1)[-1]}", f"{EX}tagsProp"
    _add(holons, fluent, RDF.type, f"{HOLON}Fluent")
    _add(holons, fluent, f"{HOLON}fluentProperty", prop)
    _add(holons, holon, f"{EX}tags", fluent)
    _add(onto, prop, f"{HOLON}fluentOperationMode", f"{HOLON}ListAccumulator")
    for member in members:
        _add(scene, fluent, f"{HOLON}hasListItem", URIRef(member))
    return fluent


async def test_list_members_are_carried_over():
    conn, store = _world()
    _add_list_fluent(store, conn, TEAM, [f"{EX}red", f"{EX}blue"])
    out = await succeed(store, conn)

    tags = next(i for i in out.fluents if i["property"] == f"{EX}tagsProp")
    assert tags["values"] == 2
    q = f"""SELECT ?m WHERE {{ GRAPH <{conn.scene_graph}> {{ <{tags["to"]}> <{HOLON}hasListItem> ?m }} }}"""
    assert {str(r.m) for r in store.ds.query(q)} == {f"{EX}red", f"{EX}blue"}


async def test_a_value_with_an_unsupported_datatype_is_refused_before_anything_changes():
    conn, store = _world()
    scene = G(store, conn.scene_graph)
    scene.remove((URIRef(SCORE), URIRef(f"{HOLON}currentValue"), None))
    scene.add((URIRef(SCORE), URIRef(f"{HOLON}currentValue"), Literal("1.5", datatype=XSD.double)))
    with pytest.raises(lc.LifecycleError) as err:
        await succeed(store, conn)
    assert err.value.code == "cannot_carry" and "double" in str(err.value)
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"  # not even frozen
    assert not any(G(store, conn.holons_graph).triples((URIRef(NEXT), None, None)))


async def test_a_value_whose_lexical_form_would_change_is_refused():
    conn, store = _world()
    scene = G(store, conn.scene_graph)
    scene.remove((URIRef(SCORE), URIRef(f"{HOLON}currentValue"), None))
    scene.add(
        (
            URIRef(SCORE),
            URIRef(f"{HOLON}currentValue"),
            Literal("007", datatype=XSD.integer, normalize=False),
        )
    )
    with pytest.raises(lc.LifecycleError) as err:
        await succeed(store, conn)
    assert err.value.code == "cannot_carry"


async def test_more_values_than_one_transaction_takes_are_refused_up_front():
    conn, store = _world()
    _add_list_fluent(
        store, conn, TEAM, [f"{EX}m{i}" for i in range(tx_mod.MAX_OPS)]
    )  # + score = 21
    with pytest.raises(lc.LifecycleError) as err:
        await succeed(store, conn)
    assert err.value.code == "too_large"
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"


# --- refusals --------------------------------------------------------------------------------


async def test_the_successor_must_not_exist():
    conn, store = _world()
    with pytest.raises(lc.LifecycleError) as err:
        await succeed(store, conn, successor=OTHER)
    assert err.value.code == "successor_exists" and err.value.status == 409


async def test_an_unknown_predecessor_and_a_self_succession_are_refused():
    conn, store = _world()
    with pytest.raises(lc.LifecycleError) as unknown:
        await succeed(store, conn, holon=f"{EX}nobody")
    assert unknown.value.code == "unknown_holon" and unknown.value.status == 404
    with pytest.raises(lc.LifecycleError) as same:
        await succeed(store, conn, successor=TEAM)
    assert same.value.code == "same_holon"


async def test_a_holon_can_be_succeeded_only_once():
    conn, store = _world()
    await succeed(store, conn)
    with pytest.raises(lc.LifecycleError) as err:
        await succeed(store, conn, successor=f"{EX}team-other")
    assert err.value.code == "already_succeeded"


async def test_a_holon_with_a_successor_cannot_be_reactivated():
    conn, store = _world()
    await succeed(store, conn)
    with pytest.raises(lc.LifecycleError) as err:
        await go(store, conn, "Active")
    assert err.value.code == "has_successor" and err.value.status == 409
    assert NEXT in str(err.value)
    # but it can move between the two inactive states
    assert (await go(store, conn, "Tombstoned")).state == "Tombstoned"
    assert (await go(store, conn, "Inactive")).state == "Inactive"
    # and the successor is its own holon and can be deactivated and reactivated
    await go(store, conn, "Inactive", holon=NEXT)
    await go(store, conn, "Active", holon=NEXT)


# --- failure leaves nothing behind -------------------------------------------------------------


async def test_a_failed_seed_removes_the_successor_and_restores_the_predecessor(monkeypatch):
    conn, store = _world()

    async def refuse(*_a, **_k):
        raise FluentError("the shapes graph said no")

    monkeypatch.setattr(sc, "update_fluents", refuse)
    with pytest.raises(lc.LifecycleError) as err:
        await succeed(store, conn)
    assert err.value.code == "succession_failed" and "rolled back" in str(err.value)

    holons = G(store, conn.holons_graph)
    assert not any(holons.triples((URIRef(NEXT), None, None)))
    assert not any(holons.triples((URIRef(TEAM), URIRef(sc.SUCCEEDED_BY), None)))
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"
    assert score_of(store, conn) == Decimal(10)


async def test_a_failed_structure_update_restores_the_predecessor(monkeypatch):
    conn, store = _world()
    real = store.update

    async def flaky(c, sparql):
        if "succeededBy" in sparql and "INSERT" in sparql:
            raise RuntimeError("store went away")
        return await real(c, sparql)

    monkeypatch.setattr(store, "update", flaky)
    with pytest.raises(lc.LifecycleError) as err:
        await succeed(store, conn)
    assert err.value.code == "succession_failed"
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"


async def test_provenance_failure_does_not_undo_the_succession(monkeypatch):
    conn, store = _world()
    real = store.update

    async def flaky(c, sparql):
        if sc.PROV in sparql:
            raise sc.FusekiError(503, "down")
        return await real(c, sparql)

    monkeypatch.setattr(store, "update", flaky)
    out = await succeed(store, conn)
    assert out.provenance_recorded is False
    assert next_score(store, conn) == Decimal(10)


# --- resolving a successor ------------------------------------------------------------------------


async def test_resolve_successor_follows_the_chain_to_its_end():
    conn, store = _world()
    third = f"{EX}team-2028"
    await succeed(store, conn)
    await sc.succeed(store, conn, holon=NEXT, successor=third, asserted_by=ALICE)
    assert await sc.resolve_successor(store, conn, TEAM) == third
    assert await sc.resolve_successor(store, conn, third) == third
    assert await sc.resolve_successor(store, conn, OTHER) == OTHER


async def test_resolve_successor_stops_at_a_cycle():
    conn, store = _world()
    holons = G(store, conn.holons_graph)
    _add(holons, TEAM, sc.SUCCEEDED_BY, OTHER)
    _add(holons, OTHER, sc.SUCCEEDED_BY, TEAM)
    assert await sc.resolve_successor(store, conn, TEAM) in {TEAM, OTHER}


# --- the route ---------------------------------------------------------------------------------------


@pytest.fixture
def grants(monkeypatch):
    asked: list[str] = []

    async def allowed(_q, _h, *, person, target):
        asked.append(target)
        return SimpleNamespace(allowed=True, matched_grant=None, reason="test")

    monkeypatch.setattr(lifecycle_routes, "check_write", allowed)
    return asked


async def post(store, conn, **body):
    request = lifecycle_routes.SucceedRequest(**{"holon": TEAM, "successor": NEXT, **body})
    return await lifecycle_routes.succeed(
        request,
        conn,
        store,
        SimpleNamespace(person=ALICE),
        SimpleNamespace(fluent_gate_scope="assertion"),
    )


async def test_the_route_succeeds_and_attributes_the_change(grants):
    conn, store = _world()
    out = await post(store, conn, reason="season over", label="Team 2027")
    assert out["ok"] and out["successor"] == NEXT and out["assertedBy"] == ALICE
    assert out["state"] == "Inactive" and out["fluents"][0]["values"] == 1
    assert set(grants) == {
        conn.graph("events"), conn.scene_graph, conn.graph("sequences"), conn.holons_graph,
        conn.ontology_graph,
    }  # fmt: skip


async def test_the_route_needs_a_grant_on_every_graph_and_writes_nothing(monkeypatch):
    conn, store = _world()

    async def denied(_q, _h, *, person, target):
        return SimpleNamespace(
            allowed=target != conn.holons_graph, matched_grant=None, reason="no grant"
        )

    monkeypatch.setattr(lifecycle_routes, "check_write", denied)
    with pytest.raises(HTTPException) as err:
        await post(store, conn)
    assert err.value.status_code == 403
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"


async def test_the_route_maps_refusals_to_their_status(grants):
    conn, store = _world()
    with pytest.raises(HTTPException) as exists:
        await post(store, conn, successor=OTHER)
    assert exists.value.status_code == 409 and exists.value.detail["error"] == "successor_exists"
    with pytest.raises(HTTPException) as unknown:
        await post(store, conn, holon=f"{EX}nobody")
    assert unknown.value.status_code == 404
    with pytest.raises(HTTPException) as unsafe:
        await post(store, conn, successor="urn:ex:x> } DROP ALL #")
    assert unsafe.value.status_code == 400


async def test_the_route_needs_an_identity(grants):
    conn, store = _world()
    request = lifecycle_routes.SucceedRequest(holon=TEAM, successor=NEXT)
    with pytest.raises(HTTPException) as err:
        await lifecycle_routes.succeed(
            request,
            conn,
            store,
            SimpleNamespace(person=None),
            SimpleNamespace(fluent_gate_scope="assertion"),
        )
    assert err.value.status_code == 401


async def test_get_lifecycle_reports_the_links_both_ways(grants):
    conn, store = _world()
    await post(store, conn)
    old = await lifecycle_routes.get_lifecycle(conn, store, SimpleNamespace(person=ALICE), iri=TEAM)
    new = await lifecycle_routes.get_lifecycle(conn, store, SimpleNamespace(person=ALICE), iri=NEXT)
    assert old["successor"] == NEXT and "predecessor" not in old
    assert new["predecessor"] == TEAM and new["state"] == "Active"


async def test_a_failure_in_a_later_round_clears_what_the_first_round_wrote(monkeypatch):
    conn, store = _world()
    _add_list_fluent(store, conn, TEAM, [f"{EX}red", f"{EX}blue"])
    real = sc.update_fluents
    calls = 0

    async def second_round_fails(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FluentError("the shapes graph said no")
        return await real(*args, **kwargs)

    monkeypatch.setattr(sc, "update_fluents", second_round_fails)
    with pytest.raises(lc.LifecycleError) as err:
        await succeed(store, conn)
    assert err.value.code == "succession_failed" and calls == 2

    assert next_score(store, conn) is None  # round one's value is gone with B
    assert not any(G(store, conn.holons_graph).triples((URIRef(NEXT), None, None)))
    q = f"""SELECT ?m WHERE {{ GRAPH <{conn.scene_graph}> {{ ?f <{HOLON}hasListItem> ?m
      FILTER (STRSTARTS(STR(?f), "{conn.graph("meta")}:succession:")) }} }}"""
    assert not list(store.ds.query(q))
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"
