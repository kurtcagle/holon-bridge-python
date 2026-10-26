"""List-mode fluents on a gated dataset, checked with the real reference shapes.

``holonbridge/shapes/fluent.ttl`` is what a dataset puts in its shapes graph to gate fluent
transitions. The pre-commit check scopes to a fluent's non-invalidated, non-superseded
StateAssertions. A scalar fluent has exactly one of those (the chain supersedes), but list
entries never supersede one another -- there is no single current assertion for a set -- so
every list entry is "current". ``StateAssertionNonOverlapShape`` therefore flagged any list
fluent with two entries, and a gated dataset refused the second ``ListInsert``.

The stand-in store elsewhere in the suite answers "conforms" to every validation, which is
why this was not caught. Here ``shacl_validate`` runs pyshacl with the shipped shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, RDF, URIRef, XSD

pytest.importorskip("pyshacl")
import pyshacl  # noqa: E402

from holonbridge import fluent as fluent_mod  # noqa: E402
from holonbridge.fluent import FluentError, Operation, update_fluent  # noqa: E402

from .test_fluent_ledger_race import ALICE, FLUENT, RdflibStore, _conn, _seed  # noqa: E402

SHAPES = (Path(fluent_mod.__file__).parent / "shapes" / "fluent.ttl").read_text()
HEVT = Namespace("https://w3id.org/holon/event/")
EX = Namespace("urn:ds:")



class PyshaclStore(RdflibStore):
    """The stand-in store, but with real validation against the shipped fluent shapes."""

    def __init__(self) -> None:
        super().__init__(shapes=SHAPES)

    async def shacl_validate(self, conn, *, target_graph: str, shapes_turtle: str) -> str:
        data = Graph()
        for triple in self.ds.graph(URIRef(target_graph)):
            data.add(triple)
        _, report, _ = pyshacl.validate(
            data, shacl_graph=shapes_turtle, shacl_graph_format="turtle", advanced=True, inference="none",
        )
        return report.serialize(format="turtle")


@pytest.fixture(autouse=True)
def _no_triggers(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(fluent_mod, "_evaluate_state_triggers", noop)


def _ledger(entries):
    """entries: (id, fluent, operation, member, iso time)."""
    g = Graph()
    for ident, fluent, op, member, when in entries:
        a = EX[ident]
        g.add((a, RDF.type, HEVT.StateAssertion))
        g.add((a, HEVT.forFluent, EX[fluent]))
        g.add((a, HEVT.operation, HEVT[op]))
        g.add((a, HEVT.hasValue, EX[member]))
        g.add((a, HEVT.assertedDateTime, Literal(when, datatype=XSD.dateTime)))
    return g


def _focus(graph):
    _, report, _ = pyshacl.validate(graph, shacl_graph=SHAPES, shacl_graph_format="turtle", advanced=True, inference="none")
    sh = Namespace("http://www.w3.org/ns/shacl#")
    return {(str(report.value(r, sh.focusNode)), str(report.value(r, sh.sourceShape)).rsplit("/", 1)[-1])
            for r in report.subjects(RDF.type, sh.ValidationResult)}


# --- the gated write path -------------------------------------------------------------


async def test_a_gated_list_fluent_accepts_a_second_member():
    conn, store = _conn(), PyshaclStore()
    _seed(store, conn, mode="ListAccumulator", with_current=False)
    # the scene graph must exist before the first list write (separate issue: an empty guard group matches nothing on a missing graph)
    store.ds.graph(URIRef(conn.scene_graph)).add((URIRef("urn:ds:x"), URIRef("urn:ds:y"), URIRef("urn:ds:z")))

    await update_fluent(store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value="urn:ds:a", is_iri=True, asserted_by=ALICE)
    await update_fluent(store, conn, fluent=FLUENT, operation=Operation.LIST_INSERT, value="urn:ds:b", is_iri=True, asserted_by=ALICE)

    assert len(store.ledger(conn)) == 2


async def test_a_gated_list_fluent_survives_insert_remove_and_reinsert():
    conn, store = _conn(), PyshaclStore()
    _seed(store, conn, mode="ListAccumulator", with_current=False)
    store.ds.graph(URIRef(conn.scene_graph)).add((URIRef("urn:ds:x"), URIRef("urn:ds:y"), URIRef("urn:ds:z")))
    for op, member in [(Operation.LIST_INSERT, "urn:ds:a"), (Operation.LIST_INSERT, "urn:ds:b"),
                       (Operation.LIST_REMOVE, "urn:ds:a"), (Operation.LIST_INSERT, "urn:ds:a")]:
        await update_fluent(store, conn, fluent=FLUENT, operation=op, value=member, is_iri=True, asserted_by=ALICE)
    assert len(store.ledger(conn)) == 4


# --- the shapes themselves ------------------------------------------------------------


def test_list_entries_are_not_overlaps():
    g = _ledger([("e1", "f", "ListInsert", "a", "2026-01-01T00:00:00Z"), ("e2", "f", "ListInsert", "b", "2026-01-02T00:00:00Z"),
                 ("e3", "f", "ListRemove", "a", "2026-01-03T00:00:00Z")])
    assert _focus(g) == set()


def test_two_unsuperseded_scalar_assertions_are_still_an_overlap():
    g = _ledger([("s1", "f", "Set", "a", "2026-01-01T00:00:00Z"), ("s2", "f", "Set", "b", "2026-01-02T00:00:00Z")])
    assert {shape for _, shape in _focus(g)} == {"StateAssertionNonOverlapShape"}
    assert {node for node, _ in _focus(g)} == {"urn:ds:s1", "urn:ds:s2"}


def test_a_superseding_scalar_chain_conforms():
    g = _ledger([("s1", "f", "Set", "a", "2026-01-01T00:00:00Z"), ("s2", "f", "Set", "b", "2026-01-02T00:00:00Z")])
    g.add((EX.s2, HEVT.supersedes, EX.s1))
    assert _focus(g) == set()


def test_a_list_entry_does_not_make_a_scalar_fluent_overlap():
    g = _ledger([("s1", "f", "Set", "a", "2026-01-01T00:00:00Z"), ("l1", "f", "ListInsert", "b", "2026-01-02T00:00:00Z")])
    assert _focus(g) == set()


def test_a_list_remove_without_an_earlier_insert_is_flagged():
    g = _ledger([("r1", "f", "ListRemove", "a", "2026-01-03T00:00:00Z")])
    assert _focus(g) == {("urn:ds:r1", "ListRemoveHasInsertShape")}


def test_a_list_remove_of_a_member_never_inserted_on_this_fluent_is_flagged():
    g = _ledger([("i1", "f", "ListInsert", "a", "2026-01-01T00:00:00Z"), ("r1", "f", "ListRemove", "b", "2026-01-03T00:00:00Z"),
                 ("r2", "g", "ListRemove", "a", "2026-01-03T00:00:00Z")])
    assert _focus(g) == {("urn:ds:r1", "ListRemoveHasInsertShape"), ("urn:ds:r2", "ListRemoveHasInsertShape")}


def test_a_remove_before_its_insert_is_flagged_and_one_at_the_same_instant_is_not():
    early = _ledger([("r1", "f", "ListRemove", "a", "2026-01-01T00:00:00Z"), ("i1", "f", "ListInsert", "a", "2026-01-02T00:00:00Z")])
    assert _focus(early) == {("urn:ds:r1", "ListRemoveHasInsertShape")}
    tie = _ledger([("i1", "f", "ListInsert", "a", "2026-01-02T00:00:00Z"), ("r1", "f", "ListRemove", "a", "2026-01-02T00:00:00Z")])
    assert _focus(tie) == set()
