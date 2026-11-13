"""The ``holder`` gate scope against real Jena Fuseki.

Skipped unless ``HOLONBRIDGE_TEST_FUSEKI_URL`` is set (see
``test_get_holon_asof_live.py`` for how to start one). The unit tests in
``test_fluent_gate_scope.py`` use rdflib; this checks that Jena evaluates the
scope query (UNIONs of GRAPH blocks, a FILTER between them) and its SHACL
service accepts and refuses as the stand-in does. Uses a scratch dataset: the
graphs it writes are dropped afterwards.
"""

from __future__ import annotations

import os

import httpx
import pytest

from holonbridge import fluent as fluent_mod
from holonbridge.conn import Conn
from holonbridge.fluent import FluentError, Operation, update_fluent
from holonbridge.fuseki import FusekiClient

from .test_fluent_gate_scope import SHAPES

URL = os.environ.get("HOLONBRIDGE_TEST_FUSEKI_URL", "").rstrip("/")
DATASET = os.environ.get("HOLONBRIDGE_TEST_FUSEKI_DATASET", "example")

pytestmark = pytest.mark.skipif(not URL, reason="HOLONBRIDGE_TEST_FUSEKI_URL not set")

FLUENT = "urn:ds:fluent:score"
PROP = "urn:ds:prop:score"
ALICE = "urn:ds:person:alice"

HOLONS_TTL = """
@prefix ex: <urn:ds:> .
@prefix holon: <https://w3id.org/holon/> .
<urn:ds:fluent:score> a holon:Fluent ; holon:fluentProperty <urn:ds:prop:score> .
ex:train1 a ex:Train ; ex:onLine ex:L1 ; ex:hasLocation <urn:ds:fluent:score> .
ex:S1 ex:onLine ex:L1 . ex:S1b ex:onLine ex:L1 . ex:S2 ex:onLine ex:L2 .
"""
ONTOLOGY_TTL = """
@prefix holon: <https://w3id.org/holon/> .
<urn:ds:prop:score> holon:fluentOperationMode holon:SetValueMode .
"""
SCENE_TTL = """
@prefix holon: <https://w3id.org/holon/> .
<urn:ds:fluent:score> holon:currentValue <urn:ds:S1> ; holon:currentAssertion <urn:ds:assertion:seed> .
"""
EVENTS_TTL = """
@prefix hevt: <https://w3id.org/holon/event/> .
<urn:ds:assertion:seed> a hevt:StateAssertion ; hevt:forFluent <urn:ds:fluent:score> ;
    hevt:hasValue <urn:ds:S1> ; hevt:operation hevt:Set .
"""


@pytest.fixture
async def live(monkeypatch):
    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(fluent_mod, "_evaluate_state_triggers", noop)
    conn = Conn(base_url=URL, dataset=DATASET, overridden=False, bank_name="local")
    graphs = {
        conn.holons_graph: HOLONS_TTL,
        conn.ontology_graph: ONTOLOGY_TTL,
        conn.scene_graph: SCENE_TTL,
        conn.graph("events"): EVENTS_TTL,
        conn.shapes_graph: SHAPES,
    }
    async with httpx.AsyncClient() as http:
        for iri, ttl in graphs.items():
            r = await http.put(
                f"{URL}/{DATASET}/data", params={"graph": iri}, content=ttl,
                headers={"Content-Type": "text/turtle"},
            )
            assert r.status_code in (200, 201, 204), r.text
        client = FusekiClient()
        try:
            yield client, conn
        finally:
            await client.aclose()
            for iri in [*graphs, conn.graph("sequences")]:
                await http.delete(f"{URL}/{DATASET}/data", params={"graph": iri})


async def _location(client, conn) -> str:
    rows = (await client.select(
        conn,
        f"SELECT ?v WHERE {{ GRAPH <{conn.scene_graph}> {{ <{FLUENT}> <https://w3id.org/holon/currentValue> ?v }} }}",
    ))["results"]["bindings"]
    return rows[0]["v"]["value"]


async def _move(client, conn, to, scope):
    return await update_fluent(
        client, conn, fluent=FLUENT, operation=Operation.SET, value=to, is_iri=True,
        asserted_by=ALICE, gate_scope=scope,
    )


async def test_the_default_scope_lets_the_wrong_line_through(live):
    client, conn = live
    await _move(client, conn, "urn:ds:S2", "assertion")
    assert await _location(client, conn) == "urn:ds:S2"


async def test_holder_scope_refuses_the_wrong_line_and_leaves_the_scene_alone(live):
    client, conn = live
    with pytest.raises(FluentError, match="rejected by shapes graph"):
        await _move(client, conn, "urn:ds:S2", "holder")
    assert await _location(client, conn) == "urn:ds:S1"


async def test_holder_scope_accepts_a_station_on_the_same_line(live):
    client, conn = live
    await _move(client, conn, "urn:ds:S1b", "holder")
    assert await _location(client, conn) == "urn:ds:S1b"


async def test_holder_scope_sees_a_sibling_fluents_current_value_on_jena(live):
    client, conn = live
    async with httpx.AsyncClient() as http:
        for graph, ttl in (
            (conn.holons_graph, """
                @prefix ex: <urn:ds:> . @prefix holon: <https://w3id.org/holon/> .
                ex:fluent_status a holon:Fluent ; holon:fluentProperty <urn:ds:prop:status> .
                ex:train1 ex:hasStatus ex:fluent_status .
                ex:Depot2 ex:kind ex:Depot ; ex:onLine ex:L1 ."""),
            (conn.scene_graph, """
                @prefix ex: <urn:ds:> . @prefix holon: <https://w3id.org/holon/> .
                ex:fluent_status holon:currentValue ex:OutOfService ."""),
        ):
            r = await http.post(
                f"{URL}/{DATASET}/data", params={"graph": graph}, content=ttl,
                headers={"Content-Type": "text/turtle"},
            )
            assert r.status_code in (200, 201, 204), r.text

    with pytest.raises(FluentError, match="rejected by shapes graph"):
        await _move(client, conn, "urn:ds:S1b", "holder")  # out of service, not a depot
    await _move(client, conn, "urn:ds:Depot2", "holder")
    assert await _location(client, conn) == "urn:ds:Depot2"
