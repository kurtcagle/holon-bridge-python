"""Tests for the GRAPH-crossing SPARQLConstraint detection added 2026-09-08.

Jena's ``/{ds}/shacl?graph=`` service validates a single flat graph, never a
full dataset -- a SPARQLConstraint whose ``sh:select`` (or ``sh:ask``)
contains a ``GRAPH ?g { ... }`` block has no named graphs to iterate over in
that context and silently reports conformance instead of firing. Reproduced
against raw Fuseki 6.1.0 by Ben Wortley, 2026-09-08 -- not a bridge bug. See
https://github.com/kurtcagle/holon-bridge-python/issues/2.

These tests prove the guard fires before any Fuseki interaction (so a
rejection is cheap, not a wasted scratch-graph round trip), stays quiet for
shapes that do not need cross-graph visibility, and applies uniformly to
Jena-backed ``validate_full``/``validate_delta`` and the offline
``pyshacl`` fallback (``validate_local``) -- the same flat-graph limitation
applies to a plain ``rdflib.Graph`` either way.
"""

from __future__ import annotations

import pytest
from rdflib import Graph

from holonbridge import shacl as shacl_mod
from holonbridge.conn import Conn

GRAPH_CROSSING_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .

ex:HomeUniquenessShape a sh:NodeShape ;
    sh:targetClass ex:Home ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "duplicate Home" ;
        sh:select \"\"\"
            SELECT $this WHERE {
                GRAPH ?g { $this a ex:Home }
            }
        \"\"\" ;
    ] .
"""

ORDINARY_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:TemperatureSensorShape a sh:NodeShape ;
    sh:targetClass ex:TemperatureSensor ;
    sh:property [
        sh:path ex:temperature ;
        sh:minCount 1 ; sh:maxCount 1 ;
        sh:datatype xsd:decimal
    ] .
"""

SINGLE_GRAPH_SPARQL_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .

ex:PositiveBalanceShape a sh:NodeShape ;
    sh:targetClass ex:Account ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "balance must be non-negative" ;
        sh:select \"\"\"
            SELECT $this WHERE {
                $this ex:balance ?b .
                FILTER (?b < 0)
            }
        \"\"\" ;
    ] .
"""

CONFORMS_REPORT = (
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n[] a sh:ValidationReport ; sh:conforms true .\n"
)


def _conn() -> Conn:
    return Conn(base_url="http://fuseki.test", dataset="ds", overridden=False, bank_name="local")


class FakeClient:
    """Records every call so a test can assert none happened."""

    def __init__(self, *, shapes_turtle: str) -> None:
        self.shapes_turtle = shapes_turtle
        self.updates: list[str] = []
        self.dropped: list[str] = []
        self.posted: list[tuple[str, str]] = []
        self.validated: list[str] = []

    async def get_graph(self, conn, graph_iri):
        return self.shapes_turtle

    async def update(self, conn, sparql):
        self.updates.append(sparql)

    async def drop_graph(self, conn, graph_iri):
        self.dropped.append(graph_iri)

    async def post_graph(self, conn, graph_iri, turtle):
        self.posted.append((graph_iri, turtle))

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        self.validated.append(target_graph)
        return CONFORMS_REPORT

    async def select(self, conn, query):
        return {"boolean": False}  # no baseline graph yet, for validate_delta


# --- pure detection -------------------------------------------------------


def test_hits_a_graph_crossing_select():
    g = Graph()
    g.parse(data=GRAPH_CROSSING_SHAPES, format="turtle")
    hits = shacl_mod._graph_crossing_hits(g)
    assert len(hits) == 1
    _, prop, owners = hits[0]
    assert prop == "select"
    assert owners == ["https://example.org/HomeUniquenessShape"]


def test_silent_on_shapes_with_no_sparql_constraint():
    g = Graph()
    g.parse(data=ORDINARY_SHAPES, format="turtle")
    assert shacl_mod._graph_crossing_hits(g) == []


def test_silent_on_a_single_graph_scoped_sparql_constraint():
    """A SPARQLConstraint that never leaves the flat graph Jena hands it is
    exactly what Jena's ``/shacl?graph=`` endpoint validates correctly --
    this must not be flagged."""
    g = Graph()
    g.parse(data=SINGLE_GRAPH_SPARQL_SHAPES, format="turtle")
    assert shacl_mod._graph_crossing_hits(g) == []


def test_reject_helper_names_the_owning_shape():
    with pytest.raises(ValueError, match="HomeUniquenessShape"):
        shacl_mod._reject_graph_crossing_constraints(GRAPH_CROSSING_SHAPES)


def test_reject_helper_is_silent_for_ordinary_shapes():
    shacl_mod._reject_graph_crossing_constraints(ORDINARY_SHAPES)  # must not raise


# --- validate_full / validate_delta ----------------------------------------


async def test_validate_full_fails_loud_before_touching_fuseki():
    client = FakeClient(shapes_turtle=GRAPH_CROSSING_SHAPES)
    conn = _conn()
    with pytest.raises(ValueError, match="HomeUniquenessShape"):
        await shacl_mod.validate_full(
            client,
            conn,
            turtle="<urn:a> <urn:b> <urn:c> .",
            shapes_graph=conn.shapes_graph,
            target_graph="urn:ds:holons",
        )
    # the guard fires before any scratch-graph work -- no wasted round trip
    assert client.updates == []
    assert client.dropped == []
    assert client.posted == []
    assert client.validated == []


async def test_validate_delta_fails_loud_before_touching_fuseki():
    client = FakeClient(shapes_turtle=GRAPH_CROSSING_SHAPES)
    conn = _conn()
    with pytest.raises(ValueError, match="HomeUniquenessShape"):
        await shacl_mod.validate_delta(
            client,
            conn,
            turtle="<urn:a> <urn:b> <urn:c> .",
            shapes_graph=conn.shapes_graph,
            target_graph="urn:ds:holons",
        )
    assert client.updates == []
    assert client.dropped == []
    assert client.posted == []
    assert client.validated == []


async def test_validate_full_unaffected_by_ordinary_shapes():
    client = FakeClient(shapes_turtle=ORDINARY_SHAPES)
    conn = _conn()
    report = await shacl_mod.validate_full(
        client,
        conn,
        turtle="<urn:a> <urn:b> <urn:c> .",
        shapes_graph=conn.shapes_graph,
        target_graph="urn:ds:holons",
    )
    assert report.conforms is True
    assert client.validated  # the real validation path still ran


async def test_validate_delta_unaffected_by_ordinary_shapes():
    client = FakeClient(shapes_turtle=ORDINARY_SHAPES)
    conn = _conn()
    report = await shacl_mod.validate_delta(
        client,
        conn,
        turtle="<urn:a> <urn:b> <urn:c> .",
        shapes_graph=conn.shapes_graph,
        target_graph="urn:ds:holons",
    )
    assert report.conforms is True


# --- validate_local (pyshacl) -----------------------------------------------


def test_validate_local_fails_loud_before_running_pyshacl():
    pytest.importorskip("pyshacl")
    with pytest.raises(ValueError, match="HomeUniquenessShape"):
        shacl_mod.validate_local("<urn:a> <urn:b> <urn:c> .", GRAPH_CROSSING_SHAPES)


def test_validate_local_unaffected_by_ordinary_shapes():
    pytest.importorskip("pyshacl")
    report = shacl_mod.validate_local("<urn:a> <urn:b> <urn:c> .", ORDINARY_SHAPES)
    assert report.conforms is True
