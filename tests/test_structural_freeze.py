"""Structural freeze: ingest paths cannot rewrite an Inactive or Tombstoned holon.

Real ``structural_block``, ``write_turtle_to_graph`` and ``/sparql/update`` against
rdflib's SPARQL engine, with the world from ``test_lifecycle``. Only identity, the
grant check and SHACL are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from rdflib import Literal, URIRef
from rdflib.namespace import RDFS

pytest.importorskip("pyshacl")

from holonbridge import ingest as ingest_mod
from holonbridge import lifecycle as lc
from holonbridge.routes import sparql as sparql_routes
from holonbridge.structural_freeze import structural_block
from holonbridge.touched import turtle_subjects, update_subjects

from .test_fluent_ledger_race import ALICE
from .test_lifecycle import (  # noqa: F401  (autouse fixture is re-exported for this module)
    EX,
    LEAGUE,
    OTHER,
    OTHER_SCORE,
    SCORE,
    TEAM,
    _no_triggers,
    _world,
    go,
)

NEW = f"{EX}newcomer"


def turtle(subject: str, value: str = "changed") -> str:
    return f'<{subject}> <http://www.w3.org/2000/01/rdf-schema#comment> "{value}" .\n'


@pytest.fixture
def allowed(monkeypatch):
    async def ok(*_a, **_k):
        return SimpleNamespace(allowed=True, matched_grant=None, reason="test")

    monkeypatch.setattr(ingest_mod, "check_write", ok)
    monkeypatch.setattr(ingest_mod, "check_replace", ok)
    monkeypatch.setattr(sparql_routes, "authorize_update", ok)

    async def no_hook(*_a, **_k):
        return None

    monkeypatch.setattr(ingest_mod, "after_write", no_hook)
    monkeypatch.setattr(sparql_routes, "after_write_update", no_hook)


async def push(store, conn, text, *, graph=None, mode="merge"):
    return await ingest_mod.write_turtle_to_graph(
        turtle=text,
        graph_iri=graph or conn.holons_graph,
        mode=mode,
        shapes_graph=None,
        reduction_rule_id=None,
        conn=conn,
        client=store,
        shacl_required=False,
        shacl_delta=False,
        animus=SimpleNamespace(person=ALICE),
    )


# --- the subject readers ---------------------------------------------------------------


def test_turtle_subjects_lists_iri_subjects_only():
    text = "<urn:a> <urn:p> [ <urn:q> 1 ] . <urn:b> <urn:p> <urn:a> ."
    assert turtle_subjects(text) == {"urn:a", "urn:b"}
    assert turtle_subjects("") == frozenset()
    assert turtle_subjects("this is not turtle") is None


def test_update_subjects_says_when_it_cannot_list_them_all():
    assert update_subjects("INSERT DATA { <urn:a> <urn:p> 1 }") == ({"urn:a"}, True)
    assert update_subjects("DELETE { ?s <urn:p> ?o } WHERE { ?s <urn:p> ?o }") == (
        frozenset(),
        False,
    )
    assert update_subjects("CLEAR GRAPH <urn:g>") == (frozenset(), False)
    assert update_subjects("not sparql") == (frozenset(), False)


# --- structural_block ------------------------------------------------------------------


async def test_nothing_is_blocked_while_every_holon_is_active():
    conn, store = _world()
    assert await structural_block(store, conn, subjects={TEAM, OTHER, SCORE}) is None


async def test_an_inactive_holon_as_a_subject_is_blocked():
    conn, store = _world()
    await go(store, conn, "Inactive")
    block = await structural_block(store, conn, subjects={TEAM})
    assert block is not None and block.kind == "inactive" and block.error == "holon_inactive"
    assert block.holders == (TEAM,) and block.state == "Inactive"
    assert TEAM in block.message


async def test_a_tombstoned_holon_is_blocked_and_other_holons_are_not():
    conn, store = _world()
    await go(store, conn, "Tombstoned")
    block = await structural_block(store, conn, subjects={TEAM})
    assert block is not None and block.state == "Tombstoned"
    assert await structural_block(store, conn, subjects={OTHER, LEAGUE, NEW}) is None


async def test_a_fluent_of_an_inactive_holon_is_blocked_as_a_subject():
    conn, store = _world()
    await go(store, conn, "Inactive")
    block = await structural_block(store, conn, subjects={SCORE})
    assert block is not None and block.holders == (TEAM,)
    assert await structural_block(store, conn, subjects={OTHER_SCORE}) is None


async def test_one_blocked_subject_among_many_blocks_the_write():
    conn, store = _world()
    await go(store, conn, "Inactive")
    block = await structural_block(store, conn, subjects={OTHER, LEAGUE, NEW, TEAM})
    assert block is not None and block.subjects == (TEAM,)


async def test_a_reactivated_holon_is_writable_again():
    conn, store = _world()
    await go(store, conn, "Inactive")
    await go(store, conn, "Active")
    assert await structural_block(store, conn, subjects={TEAM}) is None


async def test_the_lifecycle_fluent_is_governed_as_a_subject():
    conn, store = _world()
    change = await go(store, conn, "Inactive")
    await go(store, conn, "Active")
    block = await structural_block(store, conn, subjects={change.fluent})
    assert block is not None and block.error == "lifecycle_governed"


async def test_using_the_lifecycle_property_is_governed_even_for_an_active_holon():
    conn, store = _world()
    block = await structural_block(
        store, conn, subjects={OTHER}, predicates={lc.LIFECYCLE_PROPERTY}
    )
    assert block is not None and block.error == "lifecycle_governed"


async def test_a_replace_is_blocked_when_the_graph_holds_an_inactive_holon():
    conn, store = _world()
    await go(store, conn, "Inactive")
    block = await structural_block(
        store, conn, subjects={NEW}, graph=conn.holons_graph, replace=True
    )
    assert block is not None and block.kind == "replace" and "replace" in block.message
    # the same payload as a merge is fine
    assert await structural_block(store, conn, subjects={NEW}, graph=conn.holons_graph) is None


async def test_a_replace_of_a_graph_with_no_inactive_holon_is_fine():
    conn, store = _world()
    await go(store, conn, "Inactive")
    other_graph = f"{EX}elsewhere"
    assert (
        await structural_block(store, conn, subjects={NEW}, graph=other_graph, replace=True) is None
    )


async def test_an_unreadable_payload_is_blocked_only_when_something_is_inactive():
    conn, store = _world()
    assert await structural_block(store, conn, subjects=None) is None
    await go(store, conn, "Inactive")
    block = await structural_block(store, conn, subjects=None)
    assert block is not None and block.kind == "unreadable"


async def test_an_unreadable_payload_naming_the_lifecycle_property_is_governed():
    conn, store = _world()
    block = await structural_block(
        store, conn, subjects=None, raw_text="<< a b c >> hb:lifecycleStatus 1"
    )
    assert block is not None and block.error == "lifecycle_governed"


async def test_more_subjects_than_one_chunk_are_all_checked():
    conn, store = _world()
    await go(store, conn, "Inactive")
    many = {f"{EX}filler{i}" for i in range(1000)} | {TEAM}
    block = await structural_block(store, conn, subjects=many)
    assert block is not None and block.subjects == (TEAM,)


async def test_a_subject_that_cannot_be_an_iri_is_ignored_not_injected():
    conn, store = _world()
    await go(store, conn, "Inactive")
    assert await structural_block(store, conn, subjects={"urn:x> } DROP ALL { <urn:y"}) is None


# --- the Turtle write path -------------------------------------------------------------------


async def test_a_push_naming_an_inactive_holon_is_refused_and_writes_nothing(allowed):
    conn, store = _world()
    await go(store, conn, "Inactive")
    with pytest.raises(HTTPException) as err:
        await push(store, conn, turtle(TEAM))
    assert err.value.status_code == 409 and err.value.detail["error"] == "holon_inactive"
    assert err.value.detail["holders"] == [TEAM]
    holons = store.ds.graph(URIRef(conn.holons_graph))
    assert (URIRef(TEAM), RDFS.comment, Literal("changed")) not in holons


async def test_a_push_about_another_holon_goes_through(allowed):
    conn, store = _world()
    await go(store, conn, "Inactive")
    await push(store, conn, turtle(OTHER))
    holons = store.ds.graph(URIRef(conn.holons_graph))
    assert (URIRef(OTHER), RDFS.comment, Literal("changed")) in holons


async def test_a_push_succeeds_once_the_holon_is_active(allowed):
    conn, store = _world()
    await go(store, conn, "Inactive")
    await go(store, conn, "Active")
    await push(store, conn, turtle(TEAM))
    assert (URIRef(TEAM), RDFS.comment, Literal("changed")) in store.ds.graph(
        URIRef(conn.holons_graph)
    )


async def test_a_push_cannot_touch_the_scene_value_of_an_inactive_holons_fluent(allowed):
    conn, store = _world()
    await go(store, conn, "Inactive")
    forged = f'<{SCORE}> <https://w3id.org/holon/currentValue> "99" .\n'
    with pytest.raises(HTTPException) as err:
        await push(store, conn, forged, graph=conn.scene_graph)
    assert err.value.status_code == 409


async def test_a_push_cannot_set_a_lifecycle_state_around_the_route(allowed):
    conn, store = _world()
    forged = f"<{OTHER}> <{lc.LIFECYCLE_PROPERTY}> <{EX}fake> .\n"
    with pytest.raises(HTTPException) as err:
        await push(store, conn, forged)
    assert err.value.status_code == 409 and err.value.detail["error"] == "lifecycle_governed"


async def test_a_replace_push_is_refused_while_an_inactive_holon_lives_in_the_graph(allowed):
    conn, store = _world()
    await go(store, conn, "Inactive")
    with pytest.raises(HTTPException) as err:
        await push(store, conn, turtle(NEW), mode="replace")
    assert err.value.status_code == 409 and err.value.detail["kind"] == "replace"


async def test_the_lifecycle_route_still_works_on_an_inactive_holon(allowed):
    """The route writes its own fluent directly, so the freeze does not stand in its way."""
    conn, store = _world()
    await go(store, conn, "Inactive")
    change = await go(store, conn, "Active")
    assert change.changed and change.state == "Active"


# --- /sparql/update ----------------------------------------------------------------------------


async def update(store, conn, text):
    body = sparql_routes.UpdateRequest(update=text)
    return await sparql_routes.update(body, conn, store, SimpleNamespace(person=ALICE))


async def test_a_sparql_update_naming_an_inactive_holon_is_refused(allowed):
    conn, store = _world()
    await go(store, conn, "Inactive")
    text = f'INSERT DATA {{ GRAPH <{conn.holons_graph}> {{ <{TEAM}> <{EX}note> "x" }} }}'
    with pytest.raises(HTTPException) as err:
        await update(store, conn, text)
    assert err.value.status_code == 409 and err.value.detail["error"] == "holon_inactive"


async def test_a_sparql_delete_data_is_refused_too(allowed):
    conn, store = _world()
    await go(store, conn, "Tombstoned")
    text = f"DELETE DATA {{ GRAPH <{conn.holons_graph}> {{ <{TEAM}> <{EX}memberOf> <{LEAGUE}> }} }}"
    with pytest.raises(HTTPException) as err:
        await update(store, conn, text)
    assert err.value.status_code == 409
    assert (URIRef(TEAM), URIRef(f"{EX}memberOf"), URIRef(LEAGUE)) in store.ds.graph(
        URIRef(conn.holons_graph)
    )


async def test_a_sparql_update_about_an_active_holon_goes_through(allowed):
    conn, store = _world()
    await go(store, conn, "Inactive")
    text = f'INSERT DATA {{ GRAPH <{conn.holons_graph}> {{ <{OTHER}> <{EX}note> "x" }} }}'
    out = await update(store, conn, text)
    assert out["ok"] is True


async def test_a_sparql_update_using_the_lifecycle_property_is_refused(allowed):
    conn, store = _world()
    text = f"INSERT DATA {{ GRAPH <{conn.holons_graph}> {{ <{OTHER}> <{lc.LIFECYCLE_PROPERTY}> <{EX}x> }} }}"
    with pytest.raises(HTTPException) as err:
        await update(store, conn, text)
    assert err.value.detail["error"] == "lifecycle_governed"


async def test_the_gap_an_update_with_a_variable_subject_is_not_checked(allowed):
    """Documented limit, pinned so a change to it is deliberate: a subject that comes from
    the WHERE clause cannot be read statically, so this update is not refused."""
    conn, store = _world()
    await go(store, conn, "Inactive")
    text = (
        f'INSERT {{ GRAPH <{conn.holons_graph}> {{ ?s <{EX}note> "x" }} }} '
        f'WHERE {{ GRAPH <{conn.holons_graph}> {{ ?s <{RDFS.label}> "Team" }} }}'
    )
    out = await update(store, conn, text)
    assert out["ok"] is True


async def test_a_replace_would_also_drop_lifecycle_state_of_an_active_holon():
    conn, store = _world()
    await go(store, conn, "Inactive")
    await go(store, conn, "Active")  # nothing is inactive now, but the lifecycle fluent remains
    block = await structural_block(
        store, conn, subjects={NEW}, graph=conn.holons_graph, replace=True
    )
    assert block is not None and block.kind == "replace" and block.state is None
    assert "lifecycle state" in block.message
