"""Probes for behaviours the bridge relies on that the ACL lane does not cover.

Marker ``fluree``; skipped unless ``HOLONBRIDGE_TEST_FLUREE_BIN`` is set (see
``tests/fluree_support.py``). Each probe states the behaviour the bridge relies on
today. A failure here is information (a port cost), not necessarily a bug in the
client. Synthetic ``example.org`` data only.

Rules these probes follow: a 200 is never read as "applied" (every write is
confirmed by re-reading), and nothing depends on a drop being prompt (graph
removal is awaited by re-reading, up to ``SETTLE`` seconds).
"""

from __future__ import annotations

import time

import httpx
import pytest

from holonbridge.fluree import FlureeClient

from . import fluree_support as js

pytestmark = pytest.mark.fluree

G1 = "urn:example:probe:g1"
G2 = "urn:example:probe:g2"
EX = "http://example.org/"
SETTLE = 10.0


@pytest.fixture(scope="module")
def fluree_server(tmp_path_factory):
    if not js.configured():
        pytest.skip(js.skip_reason())
    server = js.FlureeServer(tmp_path_factory.mktemp("fluree-probe")).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def ds(fluree_server) -> js.FlureeDataset:
    fluree_server.reset()
    d = js.FlureeDataset(fluree_server)
    d.update(
        f"INSERT DATA {{ GRAPH <{G1}> {{ <{EX}a> <{EX}p> \"one\" }} "
        f"GRAPH <{G2}> {{ <{EX}b> <{EX}p> \"two\" }} }}"
    )
    return d


def rows(ds, sparql):
    return sorted(tuple(r.values()) for r in ds.query(sparql))


def settle(check, what):
    """Re-read until ``check()`` is true; never trust a status code or a single read."""
    deadline = time.monotonic() + SETTLE
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.25)
    raise AssertionError(f"not settled within {SETTLE:g}s: {what}")


def test_fixture_is_in_two_named_graphs(ds):
    assert rows(ds, f"SELECT ?o WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}") == [("one",)]
    assert rows(ds, f"SELECT ?o WHERE {{ GRAPH <{G2}> {{ ?s ?p ?o }} }}") == [("two",)]


def test_from_restricts_the_default_graph_to_the_named_graph(ds):
    # authorize_query relies on FROM <g> meaning "this graph and no other".
    assert rows(ds, f"SELECT ?o FROM <{G1}> WHERE {{ ?s ?p ?o }}") == [("one",)]
    assert rows(ds, f"SELECT ?o FROM <{G2}> WHERE {{ ?s ?p ?o }}") == [("two",)]


def test_from_two_graphs_is_their_union(ds):
    assert rows(ds, f"SELECT ?o FROM <{G1}> FROM <{G2}> WHERE {{ ?s ?p ?o }}") == [
        ("one",), ("two",)]


def test_from_named_restricts_graph_variable_to_the_listed_graphs(ds):
    got = rows(ds, f"SELECT ?g ?o FROM NAMED <{G1}> WHERE {{ GRAPH ?g {{ ?s ?p ?o }} }}")
    assert got == [(G1, "one")]


def test_from_named_hides_graphs_not_listed_even_when_named_explicitly(ds):
    # Strict dataset semantics: GRAPH <g2> under FROM NAMED <g1> must match nothing.
    got = rows(ds, f"SELECT ?o FROM NAMED <{G1}> WHERE {{ GRAPH <{G2}> {{ ?s ?p ?o }} }}")
    assert got == []


def test_bare_query_sees_no_named_graph_data(ds):
    assert rows(ds, "SELECT ?o WHERE { ?s ?p ?o }") == []


def test_from_does_not_expose_the_other_graph_to_graph_patterns(ds):
    # FROM <g1> sets the default graph only; GRAPH <g2> without FROM NAMED is outside
    # the declared dataset under strict semantics. Records what Fluree does.
    got = rows(ds, f"SELECT ?o FROM <{G1}> WHERE {{ GRAPH <{G2}> {{ ?s ?p ?o }} }}")
    assert got == []


async def test_client_default_graph_parameter_restricts_like_from(ds):
    # FlureeClient.select(default_graph=...) sends the SPARQL-protocol parameter
    # default-graph-uri, as FusekiClient does. This probes that Fluree honours it.
    client = FlureeClient()
    try:
        body = await client.select(
            js.conn_for(ds), "SELECT ?o WHERE { ?s ?p ?o }", default_graph=G1)
    finally:
        await client.aclose()
    got = [b["o"]["value"] for b in body["results"]["bindings"]]
    assert got == ["one"]


def _count(ds, g):
    return len(rows(ds, f"SELECT ?o WHERE {{ GRAPH <{g}> {{ ?s ?p ?o }} }}"))


def test_clear_graph_empties_it_and_leaves_the_other_graph(ds):
    ds.update(f"CLEAR SILENT GRAPH <{G1}>")
    settle(lambda: _count(ds, G1) == 0, "G1 empty after CLEAR")
    assert _count(ds, G2) == 1


def test_drop_graph_removes_its_triples_and_leaves_the_other_graph(ds):
    # Confirmed by re-reading: DROP SILENT was seen to return 200 yet leave the graph
    # visible in rapid cycles, so this awaits the effect rather than reading the status.
    ds.update(f"DROP SILENT GRAPH <{G1}>")
    settle(lambda: _count(ds, G1) == 0, "G1 empty after DROP")
    assert _count(ds, G2) == 1


async def test_client_drop_graph_then_reuse_name(ds):
    client = FlureeClient()
    conn = js.conn_for(ds)
    try:
        await client.drop_graph(conn, G1)
        settle(lambda: _count(ds, G1) == 0, "G1 empty after client.drop_graph")
        await client.put_graph(conn, G1, f"<{EX}c> <{EX}p> \"three\" .")
        settle(lambda: rows(
            ds, f"SELECT ?o WHERE {{ GRAPH <{G1}> {{ ?s ?p ?o }} }}") == [("three",)],
            "G1 holds only the new triple after PUT")
    finally:
        await client.aclose()


def test_drop_then_clear_difference_is_observable_via_graph_listing(ds):
    # The bridge lists graphs with SELECT DISTINCT ?g. Records whether a CLEARed graph
    # still appears (an empty graph is not a graph in an RDF dataset; engines differ).
    ds.update(f"CLEAR SILENT GRAPH <{G1}>")
    settle(lambda: _count(ds, G1) == 0, "G1 empty")
    listed = {r["g"] for r in ds.query("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")}
    assert G1 not in listed and G2 in listed


async def test_default_graph_store_target_and_ping_with_only_named_graphs(ds):
    client = FlureeClient()
    conn = js.conn_for(ds)
    try:
        assert await client.ping(conn) is True  # /health, not ASK
        ask = ds.query("ASK { ?s ?p ?o }")
        assert ask.askAnswer is False  # why ping must not use ASK here
        await client.put_graph(conn, "default", f"<{EX}d> <{EX}p> \"dflt\" .")
        settle(lambda: rows(ds, "SELECT ?o WHERE { ?s ?p ?o }") == [("dflt",)],
               "default graph holds the PUT triple")
    finally:
        await client.aclose()


def test_update_status_is_not_proof_of_effect(ds):
    # A DELETE WHERE that matches nothing returns 200 and changes nothing; the bridge must
    # confirm with a re-read. Pins that a "success" status coexists with no effect.
    before = _count(ds, G1)
    r = httpx.post(
        f"{ds.server.api}/update/{ds.server.dataset}",
        content=f"DELETE WHERE {{ GRAPH <{G1}> {{ <{EX}nope> ?p ?o }} }}".encode(),
        headers={"Content-Type": "application/sparql-update"},
    )
    assert r.status_code == 200
    assert _count(ds, G1) == before
