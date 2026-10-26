"""The first list insert into a dataset whose scene graph does not exist yet.

Found live (2026-10-25, a gated dataset): the first ``ListInsert`` came back as a Fuseki 404,
"No data graph", from the SHACL endpoint, and the identical call succeeded once a ``Set`` had
created the scene graph.

Cause: ``ListInsert`` has no guard, and the guard slot was an empty group wrapped as
``GRAPH <scene> { }``. Jena evaluates that to NO solutions when the graph has no triples
(checked against the live server; a guard containing a FILTER does match, which is why ``Set``
worked). So the candidate never fired on the scratch copy, the scoped CONSTRUCT came back empty,
no scratch graph was created, and the validator found nothing to validate. On an ungated dataset
the same defect would have shown as a transition that silently never writes.

rdflib follows the SPARQL text and matches ``GRAPH <missing> { }`` once, so the stand-in store
here reproduces Jena's behaviour explicitly rather than relying on the engine.
"""

from __future__ import annotations

import re

import pytest
from rdflib import URIRef

from holonbridge import fluent as fluent_mod
from holonbridge.fluent import FluentError, Operation, update_fluent
from holonbridge.fuseki import FusekiError

from .test_fluent_ledger_race import FLUENT, HOLON, RdflibStore, _conn, _seed

EMPTY_GRAPH_GROUP = re.compile(r"WHERE\s*\{\s*GRAPH\s*<([^>]+)>\s*\{(?:\s|#[^\n]*\n)*\}\s*\}")
SHAPES = "@prefix sh: <http://www.w3.org/ns/shacl#> . <urn:s> a sh:NodeShape ."


class JenaLikeStore(RdflibStore):
    """Adds the two Jena behaviours that matter here."""

    async def update(self, conn, sparql: str) -> None:
        def jena(m: re.Match) -> str:
            # Jena: GRAPH <missing> { } has no solutions (rdflib would match once)
            return m.group(0) if len(self.ds.graph(URIRef(m.group(1)))) else "WHERE { <urn:jena:none> <urn:jena:none> <urn:jena:none> }"

        await super().update(conn, EMPTY_GRAPH_GROUP.sub(jena, sparql))

    async def shacl_validate(self, conn, *, target_graph: str, shapes_turtle: str) -> str:
        if not len(self.ds.graph(URIRef(target_graph))):
            raise FusekiError(404, f"No data graph: <{target_graph}>")
        return await super().shacl_validate(conn, target_graph=target_graph, shapes_turtle=shapes_turtle)


@pytest.fixture(autouse=True)
def _no_triggers(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(fluent_mod, "_evaluate_state_triggers", noop)


def _list_store(*, gated: bool) -> tuple[JenaLikeStore, object]:
    conn, store = _conn(), JenaLikeStore(shapes=SHAPES if gated else "")
    _seed(store, conn, mode="ListAccumulator", with_current=False)
    assert not len(store.ds.graph(URIRef(conn.scene_graph)))  # the scene graph does not exist yet
    return store, conn


MEMBER = "urn:ds:member-a"


@pytest.mark.parametrize("gated", [False, True])
async def test_first_list_insert_into_an_absent_scene_graph_writes(gated):
    store, conn = _list_store(gated=gated)

    await update_fluent(store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value=MEMBER, is_iri=True)

    items = list(store.ds.graph(URIRef(conn.scene_graph)).objects(URIRef(FLUENT), URIRef(f"{HOLON}hasListItem")))
    assert items == [URIRef(MEMBER)]
    assert len(store.ledger(conn)) == 1


async def test_the_unconditional_transition_is_not_wrapped_in_the_scene_graph():
    store, conn = _list_store(gated=False)
    await update_fluent(store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value=MEMBER, is_iri=True)

    ledger_stmt = next(u for u in store.updates if "hevt:forFluent" in u)
    assert not EMPTY_GRAPH_GROUP.search(ledger_stmt)
    assert re.search(r"WHERE\s*\{\s*\}", ledger_stmt)


async def test_a_guarded_transition_is_still_wrapped_in_the_scene_graph():
    conn, store = _conn(), JenaLikeStore()
    _seed(store, conn)  # a Set-mode fluent that already has a current value

    await update_fluent(store, conn, fluent=FLUENT, operation=Operation.SET, value=2)

    assert any(f"GRAPH <{conn.scene_graph}>" in u and "holon:currentValue ?existingValue" in u for u in store.updates)


async def test_a_candidate_that_does_not_fire_is_retried_not_passed_or_404ed():
    """If the scratch copy matched nothing there is nothing to validate; that must neither pass
    nor surface as the validator's 404. It is a lost race, so the caller retries and, if it keeps
    losing, reports contention."""
    store, conn = _list_store(gated=True)

    async def nothing(_conn, _query):
        return ""

    store.construct = nothing  # the scoped CONSTRUCT finds no assertion

    with pytest.raises(FluentError, match="contention"):
        await update_fluent(
            store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value=MEMBER, is_iri=True, attempts=3
        )
    assert store.ledger(conn) == []  # nothing live was touched
