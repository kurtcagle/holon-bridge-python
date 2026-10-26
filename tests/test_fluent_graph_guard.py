"""The fluent pre-commit gate and the graph-crossing guard (issue #2).

``shacl.validate_full``/``validate_delta`` refuse a shapes graph whose
SPARQLConstraint references ``GRAPH``, because Jena's SHACL service validates one
flat graph and such a constraint would silently conform. The fluent gate
validates through the client directly and used to skip that check, so on a gated
dataset the same constraint passed every fluent transition without a word.

The stand-in store answers "conforms" to every validation, exactly as Jena
would for these shapes, so the only thing that can stop the write is the guard.
"""

from __future__ import annotations

import pytest

from holonbridge import fluent as fluent_mod
from holonbridge.fluent import FluentError, Operation, update_fluent

from .test_fluent_ledger_race import FLUENT, RdflibStore, _conn, _seed

PREFIXES = """@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <urn:ex:> .
"""
CROSSING = PREFIXES + '''ex:OnePerGraph a sh:NodeShape ; sh:targetClass ex:Home ;
  sh:sparql [ sh:select """SELECT $this ?other WHERE { GRAPH ?g { $this a ex:Home } GRAPH ?g { ?other a ex:Home } FILTER (?other != $this) }""" ] .'''
FLAT = PREFIXES + '''ex:OnePerGraph a sh:NodeShape ; sh:targetClass ex:Home ;
  sh:sparql [ sh:select """SELECT $this WHERE { ?other a ex:Home FILTER (?other != $this) }""" ] .'''


@pytest.fixture(autouse=True)
def _no_triggers(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(fluent_mod, "_evaluate_state_triggers", noop)


async def _set(shapes: str):
    conn, store = _conn(), RdflibStore(shapes=shapes)
    _seed(store, conn)
    try:
        return store, conn, await update_fluent(store, conn, fluent=FLUENT, operation=Operation.SET, value=2)
    except FluentError as exc:
        return store, conn, exc


async def test_a_graph_crossing_constraint_stops_a_gated_fluent_write():
    store, conn, outcome = await _set(CROSSING)

    assert isinstance(outcome, FluentError)
    assert "OnePerGraph" in str(outcome)  # names the shape, like the other write paths
    assert "issues/2" in str(outcome)
    assert len(store.ledger(conn)) == 1  # only the seed entry: nothing was written


async def test_the_flat_form_of_the_same_constraint_is_accepted():
    _, _, outcome = await _set(FLAT)

    assert not isinstance(outcome, FluentError)


async def test_an_ungated_dataset_is_unaffected():
    _, _, outcome = await _set("")

    assert not isinstance(outcome, FluentError)
