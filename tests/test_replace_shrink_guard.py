"""The replace-shrink guard in ``write_turtle_to_graph`` (architecture review N6).

On 6 Oct 2026 a ``push_turtle mode=replace`` with a partial payload emptied a
167-triple graph to 1 triple and the response said it conformed. A replace is a
legitimate operation, so nothing flagged it. These tests pin the guard: a replace
that would leave under 10% of a graph's triples is refused with a 409 unless the
caller says ``force_shrink``, and the payload is counted by the backend in a
throwaway graph that is always dropped. They use a small fake backend, because the
point is the decision logic, not Fuseki.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from holonbridge import ingest
from holonbridge.acl import AclDecision, Animus
from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiError
from holonbridge.routes.graphs import PushRequest

TARGET = "urn:test:insights"


class FakeFuseki:
    """Counts one 'triple' per non-blank payload line, keeps a call log."""

    def __init__(self, graphs: dict[str, int] | None = None, fail_post: bool = False):
        self.graphs = dict(graphs or {})
        self.fail_post = fail_post
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _n(turtle: str) -> int:
        return len([ln for ln in turtle.splitlines() if ln.strip()])

    async def select(self, conn, query):
        self.calls.append(("select", query))
        graph = query.split("GRAPH <", 1)[1].split(">", 1)[0]
        return {"results": {"bindings": [{"n": {"value": str(self.graphs.get(graph, 0))}}]}}

    async def post_graph(self, conn, graph, turtle):
        self.calls.append(("post", graph))
        if self.fail_post:
            raise FusekiError(400, "bad syntax")
        self.graphs[graph] = self.graphs.get(graph, 0) + self._n(turtle)

    async def put_graph(self, conn, graph, turtle):
        self.calls.append(("put", graph))
        self.graphs[graph] = self._n(turtle)

    async def drop_graph(self, conn, graph):
        self.calls.append(("drop", graph))
        self.graphs.pop(graph, None)


@pytest.fixture(autouse=True)
def _allow_everything(monkeypatch):
    async def allowed(*a, **k):
        return AclDecision(True, "test", person="urn:p:ben", matched_grant="urn:g")

    monkeypatch.setattr(ingest, "check_replace", allowed)
    monkeypatch.setattr(ingest, "check_write", allowed)


CONN = Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="b")
ANIMUS = Animus("x", "GitHubIdentity", person="urn:p:ben", real_person="urn:p:ben", acting_as=False)


def _payload(n: int) -> str:
    return "\n".join(f"<urn:s:{i}> <urn:p:p> <urn:o:{i}> ." for i in range(n))


async def _write(client, turtle, mode="replace", **kw):
    return await ingest.write_turtle_to_graph(
        turtle=turtle, graph_iri=TARGET, mode=mode, shapes_graph=None, reduction_rule_id=None,
        conn=CONN, client=client, shacl_required=False, shacl_delta=False, animus=ANIMUS, **kw,
    )


@pytest.mark.asyncio
async def test_the_6_oct_case_is_refused():
    client = FakeFuseki({TARGET: 167})
    with pytest.raises(HTTPException) as exc:
        await _write(client, _payload(1), guard_shrink=True)
    assert exc.value.status_code == 409
    d = exc.value.detail
    assert d["error"] == "replace_would_shrink_graph" and (d["triples_before"], d["triples_after"]) == (167, 1)
    assert "force_shrink" in d["message"]
    assert client.graphs[TARGET] == 167, "the target must be untouched"
    assert not any(c[0] == "put" for c in client.calls)


@pytest.mark.asyncio
async def test_the_throwaway_graph_is_always_dropped():
    client = FakeFuseki({TARGET: 167})
    with pytest.raises(HTTPException):
        await _write(client, _payload(1), guard_shrink=True)
    assert list(client.graphs) == [TARGET]
    client = FakeFuseki({TARGET: 167}, fail_post=True)
    with pytest.raises(HTTPException) as exc:
        await _write(client, _payload(1), guard_shrink=True)
    assert exc.value.status_code == 400
    assert list(client.graphs) == [TARGET]
    assert any(c[0] == "drop" for c in client.calls)


@pytest.mark.asyncio
async def test_force_shrink_allows_it_and_reports_the_counts():
    client = FakeFuseki({TARGET: 167})
    out = await _write(client, _payload(1), guard_shrink=True, force_shrink=True)
    assert out["ok"] and (out["triplesBefore"], out["triplesAfter"]) == (167, 1)
    assert client.graphs[TARGET] == 1


@pytest.mark.asyncio
async def test_a_modest_shrink_is_allowed():
    client = FakeFuseki({TARGET: 167})
    out = await _write(client, _payload(100), guard_shrink=True)
    assert (out["triplesBefore"], out["triplesAfter"]) == (167, 100)


@pytest.mark.asyncio
async def test_exactly_ten_percent_is_allowed_and_just_under_is_not():
    out = await _write(FakeFuseki({TARGET: 100}), _payload(10), guard_shrink=True)
    assert out["triplesAfter"] == 10
    with pytest.raises(HTTPException) as exc:
        await _write(FakeFuseki({TARGET: 100}), _payload(9), guard_shrink=True)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_small_graphs_are_never_guarded():
    client = FakeFuseki({TARGET: 19})
    out = await _write(client, _payload(1), guard_shrink=True)
    assert (out["triplesBefore"], out["triplesAfter"]) == (19, 1)


@pytest.mark.asyncio
async def test_a_new_graph_is_never_guarded():
    out = await _write(FakeFuseki(), _payload(5), guard_shrink=True)
    assert (out["triplesBefore"], out["triplesAfter"]) == (0, 5)


@pytest.mark.asyncio
async def test_the_guard_is_off_unless_the_caller_asks_for_it():
    """Pipelines and triggers call the same function and must behave as before."""
    client = FakeFuseki({TARGET: 167})
    out = await _write(client, _payload(1))
    assert "triplesBefore" not in out
    assert client.graphs[TARGET] == 1
    assert not any(c[0] in ("select", "post") for c in client.calls)


@pytest.mark.asyncio
async def test_merge_is_never_guarded():
    client = FakeFuseki({TARGET: 167})
    out = await _write(client, _payload(1), mode="merge", guard_shrink=True)
    assert "triplesBefore" not in out
    assert client.graphs[TARGET] == 168


def test_push_request_carries_force_shrink_and_defaults_to_false():
    assert PushRequest(turtle="<a> <b> <c> .", graph_iri="urn:g").force_shrink is False
    assert PushRequest(turtle="<a> <b> <c> .", graph_iri="urn:g", force_shrink=True).force_shrink is True
