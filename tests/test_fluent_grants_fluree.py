"""Per-fluent write grants on a real Fluree (``pytest -m fluree``): real routes, real FlureeClient, read-backs.

Fluree answers 200 to some writes that did nothing, so every claim is read back from the store.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from holonbridge.acl import Animus, _admin_role
from holonbridge.cache import RegistryCache
from holonbridge.config import Settings
from holonbridge.deps import get_acting_as, get_client, get_settings_dep, require_animus, require_conn
from holonbridge.fluree import FlureeClient
from holonbridge.persona_state import PersonaStore
from holonbridge.routes import fluent as fr, fluent_grants as gr, graphs as gp

from . import fluree_support as fs

pytestmark = pytest.mark.fluree
HOLON = "https://w3id.org/holon/"
HEVT = "https://w3id.org/holon/event/"
ADMIN, JANE = "urn:person:admin", "urn:person:jane"
PROP = "urn:ex:stage"
PROC = "urn:ex:proc:campaign:spring:"
MINE, OTHER = PROC + "channel:email", "urn:ex:proc:campaign:autumn:channel:email"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not fs.configured():
        pytest.skip(fs.skip_reason())
    s = fs.FlureeServer(tmp_path_factory.mktemp("fluree-grants")).start()
    try:
        yield s
    finally:
        s.stop()


def test_grants_on_real_fluree(server):
    server.reset()
    conn = fs.conn_for(fs.FlureeDataset(server))

    def upd(q):
        r = httpx.post(f"{server.api}/update/{server.dataset}", content=q.encode(), headers={"Content-Type": "application/sparql-update"})
        assert r.status_code < 400, r.text[:300]

    def ins(g, s, p, o):
        upd(f"INSERT DATA {{ GRAPH <{g}> {{ <{s}> <{p}> {o} . }} }}")

    ins(conn.holons_graph, ADMIN, f"{HOLON}hasRole", f"<{_admin_role(conn.holons_graph)}>")
    ins(conn.holons_graph, ADMIN, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"<{HOLON}Person>")
    ins(conn.holons_graph, JANE, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"<{HOLON}Person>")
    ins(conn.ontology_graph, PROP, f"{HOLON}fluentOperationMode", f"<{HOLON}SetValueMode>")
    for i, f in enumerate((MINE, OTHER)):
        a = f"urn:ex:assertion:seed:{i}"
        ins(conn.holons_graph, f, f"{HOLON}fluentProperty", f"<{PROP}>")
        ins(conn.holons_graph, f, "http://www.w3.org/1999/02/22-rdf-syntax-ns#type", f"<{HOLON}Fluent>")
        ins(conn.scene_graph, f, f"{HOLON}currentValue", '"Draft"')
        ins(conn.scene_graph, f, f"{HOLON}currentAssertion", f"<{a}>")
        ins(conn.graph("events"), a, f"{HEVT}forFluent", f"<{f}>")
        ins(conn.graph("events"), a, f"{HEVT}hasValue", '"Draft"')

    who = {"p": ADMIN}
    app = FastAPI()
    app.state.registry = RegistryCache(ttl=0)
    app.state.personas = PersonaStore()
    for r in (fr.router, gr.router, gp.router):
        app.include_router(r)

    async def animus():
        return Animus("x", "GitHubIdentity", person=who["p"], real_person=who["p"])

    app.dependency_overrides[require_conn] = lambda: conn
    app.dependency_overrides[get_client] = lambda: FlureeClient()
    app.dependency_overrides[require_animus] = animus
    app.dependency_overrides[get_settings_dep] = lambda: Settings()
    app.dependency_overrides[get_acting_as] = lambda: type("A", (), {"get": lambda *a, **k: None})()
    http = TestClient(app)

    def value(f):
        r = httpx.post(f"{server.api}/query/{server.dataset}", headers={"Content-Type": "application/sparql-query", "Accept": "application/sparql-results+json"},
                       content=f"SELECT ?v WHERE {{ GRAPH <{conn.scene_graph}> {{ <{f}> <{HOLON}currentValue> ?v }} }}".encode())
        return [b["v"]["value"] for b in r.json()["results"]["bindings"]]

    def write(f, v):
        return http.post("/fluent/update", json={"fluent": f, "operation": "Set", "value": v})

    who["p"] = JANE
    assert write(MINE, "Ready").status_code == 403 and value(MINE) == ["Draft"]       # no grant yet

    who["p"] = ADMIN
    r = http.post("/fluent-grant", json={"grantee": JANE, "prefixes": [PROC], "lifetime_days": 7})
    assert r.status_code == 200, r.text
    grant = r.json()["grant"]
    assert http.get("/fluent-grant", params={"grantee": JANE}).json()["grants"][0]["prefixes"] == [PROC]

    who["p"] = JANE
    r = write(MINE, "Ready")
    assert r.status_code == 200, r.text
    assert value(MINE) == ["Ready"]                                                    # read back
    r = write(OTHER, "Ready")
    assert r.status_code == 403 and value(OTHER) == ["Draft"]                          # another campaign untouched
    many = http.post("/fluent/update-many", json={"operations": [{"fluent": MINE, "operation": "Set", "value": "Live"}, {"fluent": OTHER, "operation": "Set", "value": "Live"}]})
    assert many.status_code == 403 and value(MINE) == ["Ready"] and value(OTHER) == ["Draft"]   # all or nothing
    push = http.post("/graph/push", json={"graph_iri": conn.scene_graph, "mode": "merge", "turtle": f"<{OTHER}> <{HOLON}currentValue> \"Hacked\" ."})
    assert push.status_code in (401, 403) and value(OTHER) == ["Draft"]                # a grant gives no graph write

    who["p"] = ADMIN
    rv = http.post("/fluent-grant/revoke", json={"grant": grant})
    assert rv.status_code == 200 and rv.json().get("revoked") is True, (rv.status_code, rv.text)
    who["p"] = JANE
    assert write(MINE, "Sold").status_code == 403 and value(MINE) == ["Ready"]         # revocation bites
