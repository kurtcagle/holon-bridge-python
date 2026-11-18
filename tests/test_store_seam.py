"""The store seam: ``Conn`` URLs per kind, ``make_client``, and both clients on the wire.

Everything here uses ``httpx.MockTransport``; no server is started.
"""

from __future__ import annotations

import httpx
import pytest

from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.fluree import FlureeClient
from holonbridge.fuseki import FusekiClient, FusekiError
from holonbridge.store import Store, make_client

G = "urn:example:g1"
BASE = "http://127.0.0.1:9999"


def conn(kind: str = "fuseki", **kw) -> Conn:
    return Conn(base_url=BASE, dataset="ds", overridden=False, bank_name="b",
                store_kind=kind, **kw)


def wired(client, handler):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return seen


def ok(_r):
    return httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": []}})


# --- Conn ----------------------------------------------------------------------


def test_default_kind_is_fuseki_and_urls_are_unchanged():
    c = Conn(base_url=BASE, dataset="ds", overridden=False, bank_name="b")
    assert c.store_kind == "fuseki"
    assert c.query_endpoint == "http://127.0.0.1:9999/ds/query"
    assert c.update_endpoint == "http://127.0.0.1:9999/ds/update"
    assert c.gsp_endpoint == "http://127.0.0.1:9999/ds/data"
    assert c.shacl_endpoint == "http://127.0.0.1:9999/ds/shacl"
    assert conn("fuseki").query_endpoint == c.query_endpoint


def test_fluree_urls_are_ledger_aware():
    c = conn("fluree")
    assert c.query_endpoint == f"{BASE}/v1/fluree/query/ds"
    assert c.update_endpoint == f"{BASE}/v1/fluree/update/ds"
    assert c.gsp_endpoint == f"{BASE}/v1/fluree/data/ds"


def test_settings_store_kind_default_and_env(monkeypatch):
    monkeypatch.delenv("STORE_KIND", raising=False)
    assert Settings.from_env().store_kind == "fuseki"
    monkeypatch.setenv("STORE_KIND", " Fluree ")
    assert Settings.from_env().store_kind == "fluree"


def test_make_client_picks_class_and_rejects_unknown():
    assert type(make_client()) is FusekiClient
    assert type(make_client("fuseki")) is FusekiClient
    assert type(make_client("fluree")) is FlureeClient
    with pytest.raises(ValueError):
        make_client("nope")


def test_both_clients_conform_to_the_protocol():
    assert isinstance(FusekiClient(), Store)
    assert isinstance(FlureeClient(), Store)


def test_resolve_conn_carries_store_kind():
    from holonbridge.config import BankStore
    from holonbridge.conn import resolve_conn

    s = Settings(store_kind="fluree")
    c = resolve_conn(settings=s, banks=BankStore(s))
    assert c.store_kind == "fluree"
    s = Settings()
    assert resolve_conn(settings=s, banks=BankStore(s)).store_kind == "fuseki"


# --- Fuseki on the wire (unchanged behaviour) --------------------------------------


async def test_fuseki_wire():
    c = FusekiClient()
    seen = wired(c, ok)
    cn = conn(token="t0k")
    await c.select(cn, "ASK {}", default_graph=G)
    r = seen[-1]
    assert (r.method, str(r.url)) == (
        "POST", f"{BASE}/ds/query?default-graph-uri=urn%3Aexample%3Ag1")
    assert r.headers["content-type"] == "application/sparql-query"
    assert r.headers["authorization"] == "Bearer t0k"
    assert r.content == b"ASK {}"

    await c.update(cn, "DROP ALL")
    assert (seen[-1].method, str(seen[-1].url)) == ("POST", f"{BASE}/ds/update")
    assert seen[-1].headers["content-type"] == "application/sparql-update"

    await c.put_graph(cn, G, "<a> <b> <c> .")
    assert (seen[-1].method, str(seen[-1].url)) == (
        "PUT", f"{BASE}/ds/data?graph=urn%3Aexample%3Ag1")
    await c.post_graph(cn, G, "x")
    assert seen[-1].method == "POST"
    await c.drop_graph(cn, G)
    assert seen[-1].content == f"DROP SILENT GRAPH <{G}>".encode()


async def test_fuseki_get_graph_404_is_empty_and_ping_uses_ask():
    c = FusekiClient()
    seen = wired(c, lambda r: httpx.Response(404))
    assert await c.get_graph(conn(), G) == ""
    seen.clear()
    c2 = FusekiClient()
    seen2 = wired(c2, lambda r: httpx.Response(200, json={"boolean": False}))
    assert await c2.ping(conn()) is True
    assert seen2[0].content == b"ASK { ?s ?p ?o }"


async def test_fuseki_ignores_identity_fields():
    c = FusekiClient()
    seen = wired(c, ok)
    await c.select(conn(identity="did:example:a", policy_class="ex:P"), "ASK {}")
    assert "fluree-identity" not in seen[0].headers


# --- Fluree on the wire ------------------------------------------------------------


async def test_fluree_select_construct_update():
    c = FlureeClient()
    seen = wired(c, ok)
    cn = conn("fluree", token="t0k")
    await c.select(cn, "ASK {}")
    r = seen[-1]
    assert (r.method, str(r.url)) == ("POST", f"{BASE}/v1/fluree/query/ds")
    assert r.headers["content-type"] == "application/sparql-query"
    assert r.headers["accept"] == "application/sparql-results+json"
    assert r.headers["authorization"] == "Bearer t0k"
    assert r.content == b"ASK {}"

    await c.construct(cn, "CONSTRUCT {?s ?p ?o} WHERE {?s ?p ?o}")
    assert seen[-1].headers["accept"] == "text/turtle"

    await c.update(cn, "INSERT DATA {}")
    r = seen[-1]
    assert (r.method, str(r.url)) == ("POST", f"{BASE}/v1/fluree/update/ds")
    assert r.headers["content-type"] == "application/sparql-update"
    assert r.content == b"INSERT DATA {}"


async def test_fluree_graph_store_targets():
    c = FlureeClient()
    seen = wired(c, lambda r: httpx.Response(200, text="<a> <b> <c> ."))
    cn = conn("fluree")
    await c.put_graph(cn, G, "<a> <b> <c> .")
    r = seen[-1]
    assert (r.method, str(r.url)) == (
        "PUT", f"{BASE}/v1/fluree/data/ds?graph=urn%3Aexample%3Ag1")
    assert r.headers["content-type"] == "text/turtle"
    assert r.content == b"<a> <b> <c> ."
    await c.post_graph(cn, G, "x")
    assert seen[-1].method == "POST"
    assert await c.get_graph(cn, G) == "<a> <b> <c> ."
    assert (seen[-1].method, seen[-1].headers["accept"]) == ("GET", "text/turtle")
    for default in ("default", "", None):
        await c.put_graph(cn, default, "x")
        assert str(seen[-1].url) == f"{BASE}/v1/fluree/data/ds?default=true"
        assert "graph=" not in str(seen[-1].url)
    await c.get_graph(cn, "default")
    assert str(seen[-1].url).endswith("?default=true")


async def test_fluree_drop_graph_is_a_sparql_update():
    c = FlureeClient()
    seen = wired(c, ok)
    await c.drop_graph(conn("fluree"), G)
    assert str(seen[-1].url) == f"{BASE}/v1/fluree/update/ds"
    assert seen[-1].content == f"DROP SILENT GRAPH <{G}>".encode()


async def test_fluree_get_graph_404_empty_and_errors_raise():
    c = FlureeClient()
    wired(c, lambda r: httpx.Response(404))
    assert await c.get_graph(conn("fluree"), G) == ""
    c = FlureeClient()
    wired(c, lambda r: httpx.Response(400, text="bad"))
    with pytest.raises(FusekiError) as ei:
        await c.put_graph(conn("fluree"), G, "x")
    assert ei.value.status == 400


async def test_fluree_ping_uses_health():
    c = FlureeClient()
    seen = wired(c, lambda r: httpx.Response(200, text="ok"))
    assert await c.ping(conn("fluree")) is True
    assert (seen[0].method, str(seen[0].url)) == ("GET", f"{BASE}/health")
    c = FlureeClient()
    wired(c, lambda r: httpx.Response(503))
    assert await c.ping(conn("fluree")) is False

    def boom(r):
        raise httpx.ConnectError("down")

    c = FlureeClient()
    wired(c, boom)
    assert await c.ping(conn("fluree")) is False


async def test_fluree_list_datasets_shape():
    body = [
        {"name": "ben", "branch": "main", "type": "ledger", "t": 4},
        {"name": "ben", "branch": "dev", "type": "ledger", "t": 2},
        {"name": "acl", "branch": "main", "type": "ledger", "t": 1},
    ]
    c = FlureeClient()
    seen = wired(c, lambda r: httpx.Response(200, json=body))
    out = await c.list_datasets(conn("fluree"))
    assert (seen[0].method, str(seen[0].url)) == ("GET", f"{BASE}/v1/fluree/ledgers")
    assert [d["name"] for d in out] == ["acl", "ben"]
    assert set(out[0]) == {"name", "state", "services"}  # the Fuseki shape
    assert out[0]["state"] is True and isinstance(out[0]["services"], list)


async def test_fluree_identity_headers():
    c = FlureeClient()
    seen = wired(c, ok)
    await c.select(conn("fluree", identity="did:example:a"), "ASK {}")
    assert seen[-1].headers["fluree-identity"] == "did:example:a"
    assert "fluree-policy-class" not in seen[-1].headers
    await c.update(conn("fluree", identity="did:example:a", policy_class="ex:Staff"), "X")
    assert seen[-1].headers["fluree-identity"] == "did:example:a"
    assert seen[-1].headers["fluree-policy-class"] == "ex:Staff"
    await c.put_graph(conn("fluree", identity="did:example:a"), G, "x")
    assert seen[-1].headers["fluree-identity"] == "did:example:a"
    await c.select(conn("fluree"), "ASK {}")
    assert "fluree-identity" not in seen[-1].headers


async def test_fluree_shacl_validate_raises_phase_2():
    with pytest.raises(NotImplementedError, match="phase 2"):
        await FlureeClient().shacl_validate(conn("fluree"), target_graph=G, shapes_turtle="")
