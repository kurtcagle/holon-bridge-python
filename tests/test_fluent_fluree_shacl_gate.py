"""The fluent pre-commit SHACL gate on a real Fluree (``pytest -m fluree``).

Fluree has no SHACL endpoint, so ``FlureeClient.shacl_validate`` raises. Before
the fix, ``_precommit_check_many`` called it directly, so every fluent write
failed with NotImplementedError once the shapes graph held a shape. The gate
now applies the candidate transition to in-memory copies and validates with
``fluree_shacl`` (read-only). Skipped unless a Fluree is configured.
"""
from __future__ import annotations

import httpx
import pytest
from rdflib import Graph, URIRef

from holonbridge.fluent import FluentError, Operation, update_fluent
from holonbridge.fluent_transaction import FluentOp, update_fluents
from holonbridge.fluree import FlureeClient

from . import fluree_support as fs
from . import test_fluent_ledger_race as R
from . import test_fluent_transaction as T

pytestmark = pytest.mark.fluree


class _G:
    def __init__(self, server, iri):
        self.server, self.iri = server, str(iri)

    def add(self, t):
        n3 = lambda x: x.n3() if hasattr(x, "n3") else str(x)  # noqa: E731
        q = f"INSERT DATA {{ GRAPH <{self.iri}> {{ {n3(t[0])} {n3(t[1])} {n3(t[2])} . }} }}"
        r = httpx.post(f"{self.server.api}/update/{self.server.dataset}", content=q.encode(),
                       headers={"Content-Type": "application/sparql-update"})
        assert r.status_code < 400, r.text[:300]


class _DS:
    def __init__(self, server):
        self.server = server

    def graph(self, iri):
        return _G(self.server, iri)


class _Client(FlureeClient):
    """Real FlureeClient; only the shapes graph is served from memory."""

    def __init__(self, shapes):
        super().__init__()
        self._shapes = shapes

    async def get_graph(self, conn, graph_iri):
        if graph_iri == conn.shapes_graph:
            return self._shapes
        return await super().get_graph(conn, graph_iri)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    if not fs.configured():
        pytest.skip(fs.skip_reason())
    server = fs.FlureeServer(tmp_path_factory.mktemp("fluree-gate")).start()
    try:
        yield server
    finally:
        server.stop()


def _seeded(server, shapes):
    server.reset()
    conn = fs.conn_for(fs.FlureeDataset(server))
    holder = type("S", (), {"ds": _DS(server)})()
    R._seed(holder, conn)
    T._seed_more(holder, conn)
    return conn, _Client(shapes)


async def test_update_fluent_passes_the_gate_on_fluree_with_shapes(world):
    conn, client = _seeded(world, T.CROSS_SHAPES)
    await update_fluent(client, conn, fluent=T.FLUENT, operation=Operation.SET, value=7, asserted_by=T.ALICE)


async def test_update_fluents_blocks_a_violating_transaction_on_fluree(world):
    conn, client = _seeded(world, T.CROSS_SHAPES)
    with pytest.raises(FluentError, match="rejected by shapes graph"):
        await update_fluents(client, conn, ops=T.two_ops(), asserted_by=T.ALICE)


async def test_update_fluents_passes_a_clear_transaction_on_fluree(world):
    conn, client = _seeded(world, T.CROSS_SHAPES)
    await update_fluents(
        client, conn,
        ops=[FluentOp(T.FLUENT, Operation.SET, 2), FluentOp(T.OTHER, Operation.REMOVE, 1)],
        asserted_by=T.ALICE,
    )
