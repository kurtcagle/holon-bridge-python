"""All-or-nothing updates to several fluents (``holonbridge.fluent_transaction``).

Real ``update_fluents`` and ``sequence.mint`` against rdflib's SPARQL Update
engine (``RdflibStore`` from ``test_fluent_ledger_race``), the way the
single-fluent race tests do it: the property under test lives in what the
generated SPARQL *does*, which a fake that matches its text cannot see.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from rdflib import Literal, URIRef
from rdflib.namespace import XSD

pytest.importorskip("pyshacl")

from holonbridge import fluent_transaction as tx_mod  # noqa: E402
from holonbridge.fluent import FluentError, Operation  # noqa: E402
from holonbridge.fluent_transaction import MAX_OPS, FluentOp, update_fluents  # noqa: E402

from .test_fluent_gate_scope import GatedStore  # noqa: E402
from .test_fluent_ledger_race import (  # noqa: E402
    ALICE,
    BOB,
    FLUENT,
    HEVT,
    HOLON,
    PROP,
    RdflibStore,
    _conn,
    _seed,
)

OTHER = "urn:ds:fluent:fuel"
OTHER_PROP = "urn:ds:prop:fuel"
LIST = "urn:ds:fluent:cargo"
LIST_PROP = "urn:ds:prop:cargo"


def _seed_more(store: RdflibStore, conn) -> None:
    """FLUENT (a set-value fluent holding 1) is seeded by ``_seed``. Add a numeric
    fluent holding 10 and a list fluent."""
    holons = store.ds.graph(URIRef(conn.holons_graph))
    onto = store.ds.graph(URIRef(conn.ontology_graph))
    scene = store.ds.graph(URIRef(conn.scene_graph))
    events = store.ds.graph(URIRef(conn.graph("events")))
    holons.add((URIRef(OTHER), URIRef(f"{HOLON}fluentProperty"), URIRef(OTHER_PROP)))
    onto.add((URIRef(OTHER_PROP), URIRef(f"{HOLON}fluentOperationMode"), URIRef(f"{HOLON}NumericAccumulator")))
    ten = Literal("10", datatype=XSD.decimal)
    scene.add((URIRef(OTHER), URIRef(f"{HOLON}currentValue"), ten))
    scene.add((URIRef(OTHER), URIRef(f"{HOLON}currentAssertion"), URIRef("urn:ds:assertion:seed-fuel")))
    events.add((URIRef("urn:ds:assertion:seed-fuel"), URIRef(f"{HEVT}forFluent"), URIRef(OTHER)))
    events.add((URIRef("urn:ds:assertion:seed-fuel"), URIRef(f"{HEVT}hasValue"), ten))
    holons.add((URIRef(LIST), URIRef(f"{HOLON}fluentProperty"), URIRef(LIST_PROP)))
    onto.add((URIRef(LIST_PROP), URIRef(f"{HOLON}fluentOperationMode"), URIRef(f"{HOLON}ListAccumulator")))


def _world(store_cls=RdflibStore, **kw):
    conn, store = _conn(), store_cls(**kw)
    _seed(store, conn)
    _seed_more(store, conn)
    return conn, store


@pytest.fixture(autouse=True)
def _count_trigger_runs(monkeypatch):
    calls: list[str] = []

    async def record(client, conn, *, fluent):
        calls.append(fluent)

    monkeypatch.setattr(tx_mod, "_evaluate_state_triggers", record)
    return calls


def value_of(store, conn, fluent: str) -> str | None:
    q = f"""SELECT ?v WHERE {{ GRAPH <{conn.scene_graph}> {{ <{fluent}> <{HOLON}currentValue> ?v }} }}"""
    rows = list(store.ds.query(q))
    return str(rows[0].v) if rows else None


def ledger_count(store, conn, fluent: str) -> int:
    q = f"""SELECT ?a WHERE {{ GRAPH <{conn.graph("events")}> {{ ?a <{HEVT}forFluent> <{fluent}> }} }}"""
    return len(list(store.ds.query(q)))


def live_count(store, conn, fluent: str) -> int:
    q = f"""SELECT ?a WHERE {{ GRAPH <{conn.graph("events")}> {{
      ?a <{HEVT}forFluent> <{fluent}> .
      FILTER NOT EXISTS {{ ?later <{HEVT}supersedes> ?a }} }} }}"""
    return len(list(store.ds.query(q)))


def two_ops() -> list[FluentOp]:
    return [
        FluentOp(FLUENT, Operation.SET, 2),
        FluentOp(OTHER, Operation.REMOVE, 4),
    ]


# --- the happy path ------------------------------------------------------------------


async def test_every_member_is_applied_and_linked_to_one_transaction():
    conn, store = _world()
    result = await update_fluents(
        store, conn, ops=two_ops(), asserted_by=ALICE, description="refuel and move"
    )

    assert [r.fluent for r in result.results] == [FLUENT, OTHER]
    assert Decimal(value_of(store, conn, FLUENT)) == 2
    assert Decimal(value_of(store, conn, OTHER)) == 6
    assert ledger_count(store, conn, FLUENT) == 2 and live_count(store, conn, FLUENT) == 1
    assert ledger_count(store, conn, OTHER) == 2 and live_count(store, conn, OTHER) == 1

    q = f"""SELECT ?a WHERE {{ GRAPH <{conn.graph("events")}> {{
      ?a <{HEVT}inTransaction> <{result.transaction}> }} }}"""
    assert {str(r.a) for r in store.ds.query(q)} == {r.assertion_iri for r in result.results}

    q = f"""SELECT ?by ?d WHERE {{ GRAPH <{conn.graph("events")}> {{
      <{result.transaction}> a <{HEVT}Transaction> ; <{HEVT}assertedBy> ?by ;
          <https://w3id.org/holon/description> ?d }} }}"""
    (row,) = list(store.ds.query(q))
    assert (str(row.by), str(row.d)) == (ALICE, "refuel and move")


async def test_each_member_supersedes_its_own_predecessor():
    conn, store = _world()
    first, second = (await update_fluents(store, conn, ops=two_ops(), asserted_by=ALICE)).results
    assert first.superseded == "urn:ds:assertion:seed"
    assert second.superseded == "urn:ds:assertion:seed-fuel"


async def test_a_list_member_needs_no_guard_and_rides_along():
    conn, store = _world()
    await update_fluents(
        store,
        conn,
        ops=[FluentOp(FLUENT, Operation.SET, 7), FluentOp(LIST, Operation.LIST_INSERT, "urn:ds:key", is_iri=True)],
        asserted_by=ALICE,
    )
    q = f"""SELECT ?i WHERE {{ GRAPH <{conn.scene_graph}> {{ <{LIST}> <{HOLON}hasListItem> ?i }} }}"""
    assert [str(r.i) for r in store.ds.query(q)] == ["urn:ds:key"]
    assert Decimal(value_of(store, conn, FLUENT)) == 7


async def test_a_transaction_of_only_list_inserts_works():
    conn, store = _world()
    await update_fluents(
        store, conn, ops=[FluentOp(LIST, Operation.LIST_INSERT, "x")], asserted_by=ALICE
    )
    q = f"""SELECT ?i WHERE {{ GRAPH <{conn.scene_graph}> {{ <{LIST}> <{HOLON}hasListItem> ?i }} }}"""
    assert [str(r.i) for r in store.ds.query(q)] == ["x"]


async def test_a_portal_crossing_is_recorded_on_the_transaction():
    conn, store = _world()
    result = await update_fluents(
        store, conn, ops=two_ops(), asserted_by=ALICE,
        via_portal="urn:ds:portal:door", traveller="urn:ds:person:bob",
    )
    q = f"""SELECT ?p ?t WHERE {{ GRAPH <{conn.graph("events")}> {{
      <{result.transaction}> <{HEVT}viaPortal> ?p ; <{HEVT}traveller> ?t }} }}"""
    (row,) = list(store.ds.query(q))
    assert (str(row.p), str(row.t)) == ("urn:ds:portal:door", "urn:ds:person:bob")


async def test_state_triggers_run_once_after_the_commit(_count_trigger_runs):
    conn, store = _world()
    await update_fluents(store, conn, ops=two_ops(), asserted_by=ALICE)
    assert len(_count_trigger_runs) == 1


# --- refusals before anything runs ---------------------------------------------------


async def test_an_empty_transaction_is_refused():
    conn, store = _world()
    with pytest.raises(FluentError, match="at least one"):
        await update_fluents(store, conn, ops=[], asserted_by=ALICE)


async def test_one_operation_per_fluent():
    conn, store = _world()
    ops = [FluentOp(OTHER, Operation.INSERT, 1), FluentOp(OTHER, Operation.INSERT, 1)]
    with pytest.raises(FluentError, match="more than once"):
        await update_fluents(store, conn, ops=ops, asserted_by=ALICE)
    assert store.updates == []


async def test_the_size_is_capped():
    conn, store = _world()
    ops = [FluentOp(f"urn:ds:fluent:f{i}", Operation.SET, 1) for i in range(MAX_OPS + 1)]
    with pytest.raises(FluentError, match="at most"):
        await update_fluents(store, conn, ops=ops, asserted_by=ALICE)


async def test_an_operation_that_does_not_fit_the_mode_refuses_the_whole_thing():
    conn, store = _world()
    ops = [FluentOp(FLUENT, Operation.SET, 9), FluentOp(LIST, Operation.SET, 1)]
    with pytest.raises(FluentError, match="not valid"):
        await update_fluents(store, conn, ops=ops, asserted_by=ALICE)
    assert Decimal(value_of(store, conn, FLUENT)) == 1  # the valid member was not applied
    assert store.updates == []


async def test_a_list_remove_of_a_non_member_refuses_the_whole_thing():
    conn, store = _world()
    ops = [FluentOp(FLUENT, Operation.SET, 9), FluentOp(LIST, Operation.LIST_REMOVE, "nope")]
    with pytest.raises(FluentError, match="nothing to remove"):
        await update_fluents(store, conn, ops=ops, asserted_by=ALICE)
    assert Decimal(value_of(store, conn, FLUENT)) == 1


async def test_a_delta_on_a_fluent_with_no_value_refuses_the_whole_thing():
    conn, store = _world()
    store.ds.graph(URIRef(conn.scene_graph)).remove((URIRef(OTHER), URIRef(f"{HOLON}currentValue"), None))
    with pytest.raises(FluentError, match="no current value"):
        await update_fluents(store, conn, ops=two_ops(), asserted_by=ALICE)
    assert Decimal(value_of(store, conn, FLUENT)) == 1


# --- atomicity under a race ----------------------------------------------------------


async def test_a_member_that_changed_underneath_writes_nothing_at_all(monkeypatch):
    """The point of the shared guard. OTHER's value is read as stale, so its
    guard fails while FLUENT's would hold; with one guard for both, neither the
    ledger nor the scene is touched, FLUENT included."""
    conn, store = _world()
    real = tx_mod._read_current

    async def stale_for_other(client, conn_, *, fluent):
        value, assertion = await real(client, conn_, fluent=fluent)
        if fluent == OTHER:
            from holonbridge.fluent import TypedValue

            return TypedValue(kind="literal", lexical="999", datatype=str(XSD.decimal)), assertion
        return value, assertion

    monkeypatch.setattr(tx_mod, "_read_current", stale_for_other)
    ledger_before = (ledger_count(store, conn, FLUENT), ledger_count(store, conn, OTHER))

    with pytest.raises(FluentError, match="nothing was written"):
        await update_fluents(store, conn, ops=two_ops(), asserted_by=ALICE, attempts=2)

    assert Decimal(value_of(store, conn, FLUENT)) == 1
    assert Decimal(value_of(store, conn, OTHER)) == 10
    assert (ledger_count(store, conn, FLUENT), ledger_count(store, conn, OTHER)) == ledger_before
    q = f"""SELECT ?t WHERE {{ GRAPH <{conn.graph("events")}> {{ ?t a <{HEVT}Transaction> }} }}"""
    assert list(store.ds.query(q)) == []


async def test_a_stale_read_is_retried_from_a_fresh_one(monkeypatch):
    conn, store = _world()
    real = tx_mod._read_current
    calls = {"other": 0}

    async def stale_once(client, conn_, *, fluent):
        value, assertion = await real(client, conn_, fluent=fluent)
        if fluent == OTHER:
            calls["other"] += 1
            if calls["other"] == 1:
                from holonbridge.fluent import TypedValue

                return TypedValue(kind="literal", lexical="999", datatype=str(XSD.decimal)), assertion
        return value, assertion

    monkeypatch.setattr(tx_mod, "_read_current", stale_once)
    await update_fluents(store, conn, ops=two_ops(), asserted_by=ALICE)

    assert Decimal(value_of(store, conn, FLUENT)) == 2
    assert Decimal(value_of(store, conn, OTHER)) == 6
    assert ledger_count(store, conn, FLUENT) == 2  # seed + one, not two: the failed attempt left nothing


async def test_two_concurrent_transactions_serialise_with_no_forks():
    conn, store = _world()
    a, b = await asyncio.gather(
        update_fluents(
            store, conn,
            ops=[FluentOp(FLUENT, Operation.SET, 2), FluentOp(OTHER, Operation.INSERT, 1)],
            asserted_by=ALICE,
        ),
        update_fluents(
            store, conn,
            ops=[FluentOp(FLUENT, Operation.SET, 3), FluentOp(OTHER, Operation.INSERT, 1)],
            asserted_by=BOB,
        ),
    )
    assert a.transaction != b.transaction
    assert Decimal(value_of(store, conn, OTHER)) == 12  # both deltas applied, neither lost nor doubled
    for fluent in (FLUENT, OTHER):
        assert ledger_count(store, conn, fluent) == 3
        assert live_count(store, conn, fluent) == 1


# --- the SHACL gate sees the whole candidate -----------------------------------------

CROSS_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix hevt: <https://w3id.org/holon/event/> .
@prefix ex: <urn:ds:> .

ex:NotBothShape a sh:NodeShape ;
    sh:targetClass hevt:StateAssertion ;
    sh:sparql [
        sh:message "the score may not be 2 while the fuel is 6" ;
        sh:select \"\"\"SELECT $this WHERE {
            $this <https://w3id.org/holon/event/forFluent> <urn:ds:fluent:score> ;
                  <https://w3id.org/holon/event/hasValue> ?s .
            ?other <https://w3id.org/holon/event/forFluent> <urn:ds:fluent:fuel> ;
                   <https://w3id.org/holon/event/hasValue> ?f .
            FILTER (?s = 2 && ?f = 6)
        }\"\"\" ;
    ] .
"""


async def test_a_shape_that_relates_two_members_sees_both_new_values():
    conn, store = _world(GatedStore)
    store.shapes = CROSS_SHAPES

    with pytest.raises(FluentError, match="rejected by shapes graph") as caught:
        await update_fluents(store, conn, ops=two_ops(), asserted_by=ALICE)

    assert caught.value.report is not None and caught.value.report.blocking
    # nothing live was touched
    assert Decimal(value_of(store, conn, FLUENT)) == 1
    assert Decimal(value_of(store, conn, OTHER)) == 10
    assert ledger_count(store, conn, FLUENT) == 1 and ledger_count(store, conn, OTHER) == 1


async def test_the_same_shape_passes_a_transaction_that_stays_clear_of_it():
    conn, store = _world(GatedStore)
    store.shapes = CROSS_SHAPES
    await update_fluents(
        store, conn,
        ops=[FluentOp(FLUENT, Operation.SET, 2), FluentOp(OTHER, Operation.REMOVE, 1)],
        asserted_by=ALICE,
    )
    assert Decimal(value_of(store, conn, FLUENT)) == 2 and Decimal(value_of(store, conn, OTHER)) == 9
