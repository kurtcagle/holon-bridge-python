"""The two clocks on a fluent ledger entry: valid time and transaction time.

``update_fluent`` records ``hevt:assertedDateTime`` (when it was true in the
world, which the caller may backdate) and ``hevt:insertedAt`` (when the bridge
wrote it, always the wall clock). A fact learnt late is exactly where they
differ: a body found at 23:06 that was dead by 22:41.

These run the real ``update_fluent`` against the same rdflib-backed store the
ledger-race tests use.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from rdflib import Dataset, URIRef
from rdflib.namespace import XSD

from holonbridge import fluent as fluent_mod
from holonbridge.fluent import Operation, update_fluent

from .test_fluent_ledger_race import FLUENT, HEVT, RdflibStore, _conn, _seed


@pytest.fixture(autouse=True)
def _no_triggers(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(fluent_mod, "_evaluate_state_triggers", noop)


def _times(store: RdflibStore, conn, assertion: str) -> tuple[datetime, datetime | None]:
    events = store.ds.graph(URIRef(conn.graph("events")))
    asserted = events.value(URIRef(assertion), URIRef(f"{HEVT}assertedDateTime"))
    inserted = events.value(URIRef(assertion), URIRef(f"{HEVT}insertedAt"))
    return asserted.toPython(), inserted.toPython() if inserted is not None else None


async def test_every_entry_is_stamped_with_the_wall_clock_insertion_time():
    conn, store = _conn(), RdflibStore()
    _seed(store, conn)
    before = datetime.now(timezone.utc)

    result = await update_fluent(store, conn, fluent=FLUENT, operation=Operation.SET, value=2)

    _, inserted = _times(store, conn, result.assertion_iri)
    assert inserted is not None and inserted.tzinfo is not None
    assert before <= inserted <= datetime.now(timezone.utc)


async def test_backdating_moves_valid_time_and_leaves_insertion_time_alone():
    conn, store = _conn(), RdflibStore()
    _seed(store, conn)
    discovered_late = datetime(2020, 11, 12, 22, 41, tzinfo=timezone.utc)
    before = datetime.now(timezone.utc)

    result = await update_fluent(
        store, conn, fluent=FLUENT, operation=Operation.SET, value=2,
        asserted_datetime=discovered_late,
    )

    asserted, inserted = _times(store, conn, result.assertion_iri)
    assert asserted == discovered_late
    assert inserted is not None and inserted >= before  # not the backdated time
    assert inserted - asserted > timedelta(days=365)


async def test_insertion_time_is_a_datetime_literal():
    conn, store = _conn(), RdflibStore()
    _seed(store, conn)
    result = await update_fluent(store, conn, fluent=FLUENT, operation=Operation.SET, value=2)
    events = store.ds.graph(URIRef(conn.graph("events")))
    literal = events.value(URIRef(result.assertion_iri), URIRef(f"{HEVT}insertedAt"))
    assert literal.datatype == XSD.dateTime
