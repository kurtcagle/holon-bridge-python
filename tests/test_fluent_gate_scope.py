"""``FLUENT_GATE_SCOPE``: what the fluent update's pre-commit SHACL gate can see.

The default scope is the fluent's own live StateAssertion, so a rule that needs
the holder or a sibling fluent cannot be a gate shape. ``holder`` widens the
validated graph. These tests run the real update path against the rdflib stand-in
store with real pyshacl validation and a SHACL-SPARQL shape that needs data from
outside the assertion:

    a train's location must be a station on the train's own line
    a train that is out of service may only be in a depot (a sibling fluent)
"""

from __future__ import annotations

import pytest
from rdflib import Graph, Literal, Namespace, RDF, URIRef

pytest.importorskip("pyshacl")
import pyshacl  # noqa: E402

from holonbridge import fluent as fluent_mod  # noqa: E402
from holonbridge.config import Settings  # noqa: E402
from holonbridge.fluent import FluentError, Operation, update_fluent  # noqa: E402

from .test_fluent_ledger_race import ALICE, FLUENT, PROP, RdflibStore, _conn, _seed  # noqa: E402

EX = Namespace("urn:ds:")
HOLON = Namespace("https://w3id.org/holon/")
STATUS = "urn:ds:fluent:status"
STATUS_PROP = "urn:ds:prop:status"

SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix holon: <https://w3id.org/holon/> .
@prefix ex: <urn:ds:> .

ex:LocationOnOwnLineShape a sh:NodeShape ;
    sh:targetClass holon:Fluent ;
    sh:sparql [
        sh:message "location must be a station on the train's own line" ;
        sh:select \"\"\"SELECT $this ?value WHERE {
            ?holder ?link $this ; <urn:ds:onLine> ?trainLine .
            $this <https://w3id.org/holon/currentValue> ?value .
            ?value <urn:ds:onLine> ?stationLine .
            FILTER (?stationLine != ?trainLine)
        }\"\"\" ;
    ] .

ex:OutOfServiceOnlyInDepotShape a sh:NodeShape ;
    sh:targetClass holon:Fluent ;
    sh:sparql [
        sh:message "an out-of-service train may only be in a depot" ;
        sh:select \"\"\"SELECT $this WHERE {
            ?holder ?link $this ; ?link2 ?sibling .
            ?sibling <https://w3id.org/holon/fluentProperty> <urn:ds:prop:status> ;
                     <https://w3id.org/holon/currentValue> <urn:ds:OutOfService> .
            $this <https://w3id.org/holon/currentValue> ?value .
            FILTER (?value != <urn:ds:OutOfService>)
            FILTER NOT EXISTS { ?value <urn:ds:kind> <urn:ds:Depot> }
        }\"\"\" ;
    ] .
"""


class GatedStore(RdflibStore):
    def __init__(self) -> None:
        super().__init__(shapes=SHAPES)

    async def post_graph(self, conn, graph_iri: str, turtle: str) -> None:
        # Parse into a scratch Graph and copy the triples. Parsing straight into
        # the dataset binds the document's @prefix lines (``holon:`` among them)
        # into the dataset's namespaces, which makes rdflib's SPARQL Update
        # reject a later statement's own ``PREFIX holon:``. A real store keeps no
        # such state; this is a stand-in quirk, not behaviour under test.
        parsed = Graph()
        parsed.parse(data=turtle, format="turtle")
        target = self.ds.graph(URIRef(graph_iri))
        for triple in parsed:
            target.add(triple)

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


def _world(*, status: str | None = None):
    conn, store = _conn(), GatedStore()
    _seed(store, conn)  # a fluent that already has a current value and a ledger entry
    holons = store.ds.graph(URIRef(conn.holons_graph))
    scene = store.ds.graph(URIRef(conn.scene_graph))
    events = store.ds.graph(URIRef(conn.graph("events")))
    # The seed's value is a number; the train starts at station S1 instead.
    scene.remove((FLUENT_REF, HOLON.currentValue, None))
    scene.add((FLUENT_REF, HOLON.currentValue, EX.S1))
    events.remove((URIRef("urn:ds:assertion:seed"), URIRef("https://w3id.org/holon/event/hasValue"), None))
    events.add((URIRef("urn:ds:assertion:seed"), URIRef("https://w3id.org/holon/event/hasValue"), EX.S1))
    holons.add((FLUENT_REF, RDF.type, HOLON.Fluent))
    holons.add((EX.train1, RDF.type, EX.Train))
    holons.add((EX.train1, EX.onLine, EX.L1))
    holons.add((EX.train1, EX.hasLocation, FLUENT_REF))
    for station, line in (("S1", "L1"), ("S1b", "L1"), ("S2", "L2")):
        holons.add((EX[station], EX.onLine, EX[line]))
    holons.add((EX.Depot2, EX.kind, EX.Depot))
    holons.add((EX.Depot2, EX.onLine, EX.L1))
    if status:
        holons.add((URIRef(STATUS), RDF.type, HOLON.Fluent))
        holons.add((URIRef(STATUS), HOLON.fluentProperty, URIRef(STATUS_PROP)))
        holons.add((EX.train1, EX.hasStatus, URIRef(STATUS)))
        scene.add((URIRef(STATUS), HOLON.currentValue, URIRef(status)))
    return conn, store


FLUENT_REF = URIRef(FLUENT)


async def _move(store, conn, to: str, *, scope: str):
    return await update_fluent(
        store, conn, fluent=FLUENT, operation=Operation.SET, value=to, is_iri=True,
        asserted_by=ALICE, gate_scope=scope,
    )


def _location(store, conn):
    return store.current(conn)[0]


# --- the default scope cannot see across holons ------------------------------------------


async def test_the_default_scope_cannot_enforce_a_rule_that_needs_the_holder():
    conn, store = _world()

    await _move(store, conn, "urn:ds:S2", scope="assertion")  # wrong line, but invisible to the gate

    assert _location(store, conn) == "urn:ds:S2"


# --- holder scope ---------------------------------------------------------------------


async def test_holder_scope_refuses_a_station_on_another_line_and_touches_nothing_live():
    conn, store = _world()
    ledger_before = store.ledger(conn)

    with pytest.raises(FluentError) as caught:
        await _move(store, conn, "urn:ds:S2", scope="holder")

    assert "rejected by shapes graph" in str(caught.value)
    assert _location(store, conn) == "urn:ds:S1"
    assert store.ledger(conn) == ledger_before


async def test_holder_scope_accepts_a_station_on_the_same_line():
    conn, store = _world()

    await _move(store, conn, "urn:ds:S1b", scope="holder")

    assert _location(store, conn) == "urn:ds:S1b"


async def test_holder_scope_sees_a_sibling_fluents_current_value():
    conn, store = _world(status="urn:ds:OutOfService")

    with pytest.raises(FluentError):
        await _move(store, conn, "urn:ds:S1b", scope="holder")  # not a depot

    await _move(store, conn, "urn:ds:Depot2", scope="holder")
    assert _location(store, conn) == "urn:ds:Depot2"


async def test_the_sibling_rule_is_invisible_to_the_default_scope():
    conn, store = _world(status="urn:ds:OutOfService")

    await _move(store, conn, "urn:ds:S1b", scope="assertion")

    assert _location(store, conn) == "urn:ds:S1b"


# --- the setting ------------------------------------------------------------------------


async def test_an_unknown_gate_scope_is_refused_before_anything_runs():
    conn, store = _world()
    with pytest.raises(FluentError, match="gate_scope"):
        await _move(store, conn, "urn:ds:S1", scope="everything")
    assert store.updates == []


def test_the_setting_defaults_to_assertion_and_rejects_nonsense():
    assert Settings().fluent_gate_scope == "assertion"
    assert Settings(fluent_gate_scope="holder").fluent_gate_scope == "holder"
    with pytest.raises(ValueError, match="FLUENT_GATE_SCOPE"):
        Settings(fluent_gate_scope="wide")
