"""Regression tests for how ``update_fluent`` decides a transition has landed
(fixed 2026-09-30 -- see the ``CHANGED 2026-09-30 (confirmation)`` note in
``holonbridge/fluent.py``).

It used to read back the scene graph's ``holon:currentAssertion`` and compare it
with its own. That was wrong in three ways, each pinned down below:

* **List-mode fluents never have a ``currentAssertion``**, so a ListInsert or
  ListRemove could never confirm: it wrote its member, looped to the attempt
  limit writing one ledger entry per attempt, then raised.
* **A third writer landing between a transition's commit and its read-back**
  made the winner think it had lost. It retried from a fresh read and applied
  its operation a second time -- an ``Insert`` delta counted twice.
* **``newValue`` was the scene graph's value at read-back**, which in that case
  belongs to someone else.

It now asks the ledger for its own entry. The ledger append shares the scene
update's guard and transaction (PR #41), so the entry exists exactly when the
transition fired, and being append-only it cannot be removed afterwards.

Same harness as ``test_fluent_ledger_race``: the real ``update_fluent`` and
``sequence.mint`` against rdflib's SPARQL engine (not Jena).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from rdflib import URIRef

from holonbridge import fluent as fluent_mod
from holonbridge.config import Settings
from holonbridge.fluent import FluentError, Operation, update_fluent
from holonbridge.routes import fluent as fluent_routes
from holonbridge.sequence import SequenceError
from tests.test_fluent_ledger_race import (
    ALICE,
    BOB,
    FLUENT,
    HEVT,
    HOLON,
    SEED_ASSERTION,
    RdflibStore,
    _conn,
    _seed,
)


@pytest.fixture(autouse=True)
def _no_triggers(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(fluent_mod, "_evaluate_state_triggers", noop)


def members(store: RdflibStore, conn) -> set[str]:
    q = f"""SELECT ?o WHERE {{ GRAPH <{conn.scene_graph}> {{ <{FLUENT}> <{HOLON}hasListItem> ?o }} }}"""
    return {str(r.o) for r in store.ds.query(q)}


def minted(store: RdflibStore, conn) -> int:
    return len(store.ds.graph(URIRef(conn.graph("sequences"))))


# --- list mode ------------------------------------------------------------------


async def test_a_list_insert_confirms_on_its_first_attempt():
    conn, store = _conn(), RdflibStore()
    _seed(store, conn, mode="ListAccumulator", with_current=False)

    result = await update_fluent(
        store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value="m1", asserted_by=ALICE
    )

    assert result.new_value.lexical == "m1"
    assert members(store, conn) == {"m1"}
    assert len(store.ledger(conn)) == 1  # not one per attempt
    assert store.current(conn) == (None, None)  # list fluents still have no currentAssertion


async def test_concurrent_list_inserts_both_land_and_each_is_recorded_once():
    conn, store = _conn(), RdflibStore()
    _seed(store, conn, mode="ListAccumulator", with_current=False)

    await asyncio.gather(
        update_fluent(store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value="m1", asserted_by=ALICE),
        update_fluent(store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value="m2", asserted_by=BOB),
    )

    assert members(store, conn) == {"m1", "m2"}
    assert len(store.ledger(conn)) == 2


async def test_a_list_remove_removes_a_present_member():
    conn, store = _conn(), RdflibStore()
    _seed(store, conn, mode="ListAccumulator", with_current=False)
    for m in ("m1", "m2"):
        await update_fluent(
            store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value=m, asserted_by=ALICE
        )

    await update_fluent(
        store, conn, fluent=FLUENT, operation=Operation.LIST_REMOVE, value="m1", asserted_by=ALICE
    )

    assert members(store, conn) == {"m2"}
    assert len(store.ledger(conn)) == 3


async def test_removing_a_non_member_is_refused_cleanly_and_writes_nothing():
    """Its guard can never match, so without an up-front check it would retry to
    the attempt limit and report contention that does not exist."""
    conn, store = _conn(), RdflibStore()
    _seed(store, conn, mode="ListAccumulator", with_current=False)

    with pytest.raises(FluentError, match="nothing to remove"):
        await update_fluent(
            store, conn, fluent=FLUENT, operation=Operation.LIST_REMOVE, value="ghost", asserted_by=ALICE
        )

    assert store.ledger(conn) == []
    assert minted(store, conn) == 0  # refused before an id was spent


async def test_a_list_member_that_is_an_iri_is_found_and_removed():
    conn, store = _conn(), RdflibStore()
    _seed(store, conn, mode="ListAccumulator", with_current=False)
    member = "urn:ds:member:one"
    await update_fluent(
        store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value=member, is_iri=True, asserted_by=ALICE
    )

    await update_fluent(
        store, conn, fluent=FLUENT, operation=Operation.LIST_REMOVE, value=member, is_iri=True, asserted_by=ALICE
    )

    assert members(store, conn) == set()


# --- a third writer between commit and read-back -----------------------------------


class InterleavingStore(RdflibStore):
    """After the first update carrying ``marker``, run ``then`` once: a third
    writer's commit landing between this transition's update and its read-back."""

    def __init__(self, marker: str, then) -> None:
        super().__init__()
        self.marker, self.then, self.fired = marker, then, False

    async def update(self, conn, sparql: str) -> None:
        await super().update(conn, sparql)
        if not self.fired and self.marker in sparql:
            self.fired = True
            self.then(self, conn)


def _third_writer(store: RdflibStore, conn) -> None:
    store.ds.update(
        f"""PREFIX holon: <{HOLON}>
PREFIX hevt: <{HEVT}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT {{ GRAPH <{conn.graph("events")}> {{
    <urn:ds:assertion:third> a hevt:StateAssertion ; hevt:forFluent <{FLUENT}> ;
        hevt:hasValue "100"^^xsd:decimal ; hevt:supersedes ?a }} }}
WHERE {{ GRAPH <{conn.scene_graph}> {{ <{FLUENT}> holon:currentAssertion ?a }} }} ;
DELETE {{ GRAPH <{conn.scene_graph}> {{ <{FLUENT}> holon:currentValue ?v ; holon:currentAssertion ?a }} }}
INSERT {{ GRAPH <{conn.scene_graph}> {{ <{FLUENT}> holon:currentValue "100"^^xsd:decimal ;
        holon:currentAssertion <urn:ds:assertion:third> }} }}
WHERE {{ GRAPH <{conn.scene_graph}> {{ <{FLUENT}> holon:currentValue ?v ; holon:currentAssertion ?a }} }}"""
    )


async def test_a_winner_is_not_made_to_retry_by_a_later_writer():
    """The defect: this Insert fires, a third writer commits before the read-back,
    the old comparison saw someone else's assertion, retried, and applied +5 again."""
    conn = _conn()
    store = InterleavingStore("hevt:operation hevt:Insert", _third_writer)
    _seed(store, conn, mode="NumericAccumulator")

    result = await update_fluent(
        store, conn, fluent=FLUENT, operation=Operation.INSERT, value=5, asserted_by=ALICE
    )

    assert store.fired
    inserts = [a for a in store.ledger(conn) if a not in (SEED_ASSERTION, "urn:ds:assertion:third")]
    assert len(inserts) == 1, "the Insert was applied more than once"
    assert store.current(conn)[0] == "100"  # the later writer's value stands; nothing was re-applied on top


async def test_new_value_is_this_transitions_own_not_a_later_writers():
    conn = _conn()
    store = InterleavingStore("hevt:operation hevt:Insert", _third_writer)
    _seed(store, conn, mode="NumericAccumulator")

    result = await update_fluent(
        store, conn, fluent=FLUENT, operation=Operation.INSERT, value=5, asserted_by=ALICE
    )

    assert float(result.new_value.lexical) == 6.0  # 1 + 5, not the 100 the scene graph now holds
    assert result.superseded == SEED_ASSERTION


# --- the route ----------------------------------------------------------------------


@pytest.fixture
def allowed_route(monkeypatch):
    async def allowed(*_a, **_k):
        return SimpleNamespace(allowed=True, matched_grant=None, reason="test")

    monkeypatch.setattr(fluent_routes, "check_write", allowed)


def _body() -> fluent_routes.UpdateFluentRequest:
    return fluent_routes.UpdateFluentRequest(fluent=FLUENT, operation=Operation.SET, value=1)


class _NoPortals:
    """A client with nothing in the portals graph: the route asks it whether the
    fluent's property is governed by a portal, and the answer is no."""

    async def select(self, conn, query):
        return {"results": {"bindings": []}}


async def _call_route():
    return await fluent_routes.update(
        _body(), _conn(), _NoPortals(), SimpleNamespace(person=ALICE), Settings()
    )


async def test_an_exhausted_id_minter_is_a_409_not_a_bare_500(monkeypatch, allowed_route):
    async def exhausted(*_a, **_k):
        raise SequenceError("could not mint from sequence 'event' after 8 attempts (contention)")

    monkeypatch.setattr(fluent_routes, "update_fluent", exhausted)

    with pytest.raises(HTTPException) as err:
        await _call_route()

    assert err.value.status_code == 409
    assert err.value.detail["error"] == "sequence_contention"
    assert "after 8 attempts" in err.value.detail["message"]


async def test_an_ordinary_fluent_refusal_is_still_a_400(monkeypatch, allowed_route):
    async def refused(*_a, **_k):
        raise FluentError("not valid for this mode")

    monkeypatch.setattr(fluent_routes, "update_fluent", refused)

    with pytest.raises(HTTPException) as err:
        await _call_route()

    assert err.value.status_code == 400
    assert err.value.detail["error"] == "fluent_error"
