"""Tests that run without a live Fuseki.

The backend is stubbed at the :class:`FusekiClient` boundary, so route
wiring, auth, dataset override, the SHACL gate, and delta diffing are all
exercised for real.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from holonbridge.config import BankStore, Settings
from holonbridge.conn import Conn, resolve_conn
from holonbridge.databook import DataBook
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app
from holonbridge.shacl import parse_report
from holonbridge.turtle import escape_literal, literal, looks_like_rdf12

TOKEN = "test-token"

CONFORMING_REPORT = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
[] a sh:ValidationReport ; sh:conforms true .
"""

VIOLATION_REPORT = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .
[] a sh:ValidationReport ;
   sh:conforms false ;
   sh:result [
     a sh:ValidationResult ;
     sh:resultSeverity sh:Violation ;
     sh:focusNode ex:sensor-A1 ;
     sh:resultPath ex:temperature ;
     sh:resultMessage "out of range"
   ] .
"""

# What build_animus's resolve_person query expects back for a successful
# identity resolution -- shared by every test that needs AnimusDep to
# actually resolve rather than 401.
IDENTITY_RESULT = {
    "results": {
        "bindings": [
            {
                "person": {"type": "uri", "value": "urn:ds:person:kurt"},
                "label": {"type": "literal", "value": "Kurt Cagle"},
            }
        ]
    }
}


class StubFuseki:
    """Records calls and replays scripted responses.

    CHANGED 2026-08-17: select() now branches on query shape rather than
    always returning one canned ``select_result``. AnimusDep's
    build_animus sends two distinct query shapes of its own
    (hasExternalIdentity for the person, memberOfTeam for teams) before a
    route handler's own queries ever run -- routes that don't require
    AnimusDep never hit this branch at all, so nothing about their
    existing behaviour changes.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.select_result: dict = {"results": {"bindings": []}}
        self.identity_result: dict = IDENTITY_RESULT
        self.construct_result = "# empty\n"
        self.graphs: dict[str, str] = {}
        self.shacl_reports: list[str] = []

    async def select(self, conn, query, *, default_graph=None):
        self.calls.append(("select", query))
        if "hasExternalIdentity" in query:
            return self.identity_result
        if "memberOfTeam" in query:
            return {"results": {"bindings": []}}
        return self.select_result

    async def construct(self, conn, query, *, default_graph=None):
        self.calls.append(("construct", query))
        return self.construct_result

    async def update(self, conn, update):
        self.calls.append(("update", update))

    async def get_graph(self, conn, graph_iri):
        self.calls.append(("get_graph", graph_iri))
        return self.graphs.get(graph_iri, "")

    async def post_graph(self, conn, graph_iri, turtle):
        self.calls.append(("post_graph", graph_iri))
        self.graphs[graph_iri] = self.graphs.get(graph_iri, "") + turtle

    async def put_graph(self, conn, graph_iri, turtle):
        self.calls.append(("put_graph", graph_iri))
        self.graphs[graph_iri] = turtle

    async def drop_graph(self, conn, graph_iri):
        self.calls.append(("drop_graph", graph_iri))
        self.graphs.pop(graph_iri, None)

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        self.calls.append(("shacl", target_graph))
        return self.shacl_reports.pop(0) if self.shacl_reports else CONFORMING_REPORT

    async def ping(self, conn):
        return True

    async def aclose(self):
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bearer_token=TOKEN,
        fuseki_dataset="ds",
        shacl_required=False,
        shacl_delta=True,
    )


@pytest.fixture
def stub() -> StubFuseki:
    return StubFuseki()


@pytest.fixture
def client(settings: Settings, stub: StubFuseki, tmp_path):
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.fuseki = stub  # replace the real client after lifespan startup
        # Isolated per test -- without this, app.state.personas is
        # PersonaStore()'s real default (~/.holonbridge/persona-state.json),
        # shared with every other test and every other suite run in the
        # same environment. See tests/test_persona.py's matching fix.
        app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
        yield test_client


def auth(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    headers.update(extra or {})
    return headers


def animus_auth(extra: dict[str, str] | None = None) -> dict[str, str]:
    """auth() plus a resolvable animus identity -- for routes gated by
    AnimusDep (whoami, /endpoint, /holon, the sparql endpoints)."""
    headers = auth(
        {"X-Holon-Animus-Id": "kurtcagle", "X-Holon-Animus-Type": "GitHubIdentity"}
    )
    headers.update(extra or {})
    return headers


# --- auth ---------------------------------------------------------------------


def test_missing_token_rejected(client):
    assert client.get("/graphs").status_code == 401


def test_wrong_token_rejected(client):
    response = client.get("/graphs", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_health_needs_no_token(client):
    assert client.get("/health").json()["ok"] is True


# --- conn ---------------------------------------------------------------------


def test_graph_naming_convention():
    conn = Conn(base_url="http://x", dataset="bridgerton", overridden=False, bank_name="local")
    assert conn.holons_graph == "urn:bridgerton:holons"
    assert conn.shapes_graph == "urn:bridgerton:shacl"
    with pytest.raises(ValueError):
        conn.graph("nonsense")


def test_dataset_override_applies(client, stub):
    response = client.get("/graphs", headers=auth({"X-Dataset-Override": "worldtest"}))
    assert response.status_code == 200
    body = response.json()
    assert body["dataset"] == "worldtest"
    assert body["overridden"] is True


def test_override_rejects_path_escape(settings):
    banks = BankStore(settings)
    with pytest.raises(ValueError):
        resolve_conn(settings=settings, banks=banks, override="../evil")


def test_override_disabled_is_ignored(stub):
    settings = Settings(bearer_token=TOKEN, allow_dataset_override=False)
    banks = BankStore(settings)
    conn = resolve_conn(settings=settings, banks=banks, override="worldtest")
    assert conn.dataset == "ds"
    assert conn.overridden is False


# --- sparql -------------------------------------------------------------------


def test_update_sent_to_query_endpoint_is_refused(client):
    response = client.post(
        "/sparql/select", json={"query": "INSERT DATA { <urn:a> <urn:b> <urn:c> }"}, headers=auth()
    )
    assert response.status_code == 400


def test_select_sent_to_update_endpoint_is_refused(client):
    response = client.post(
        "/sparql/update", json={"update": "SELECT * WHERE { ?s ?p ?o }"}, headers=auth()
    )
    assert response.status_code == 400


def test_list_graphs_filters(client, stub):
    stub.select_result = {
        "results": {
            "bindings": [
                {"g": {"value": "urn:ds:holons"}, "triples": {"value": "12"}},
                {"g": {"value": "urn:ds:shacl"}, "triples": {"value": "3"}},
            ]
        }
    }
    body = client.get("/graphs", params={"filter": "shacl"}, headers=auth()).json()
    assert body["count"] == 1
    assert body["graphs"][0]["graph"] == "urn:ds:shacl"


# --- shacl gate ---------------------------------------------------------------


def test_push_without_shapes_skips_validation(client, stub):
    response = client.post(
        "/graph/push",
        json={"turtle": "<urn:a> <urn:b> <urn:c> .", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    )
    assert response.status_code == 200
    assert response.json()["validated"] is False
    assert not any(call[0] == "shacl" for call in stub.calls)


def test_delta_mode_ignores_pre_existing_violation(client, stub):
    stub.graphs["urn:ds:shacl"] = "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    # baseline already has the violation; merged report is identical
    stub.shacl_reports = [VIOLATION_REPORT, VIOLATION_REPORT]

    response = client.post(
        "/graph/push",
        json={
            "turtle": "<urn:a> <urn:b> <urn:c> .",
            "graph_iri": "urn:ds:holons",
            "shapes_graph": "urn:ds:shacl",
        },
        headers=auth(),
    )
    assert response.status_code == 200
    assert response.json()["validation"]["conforms"] is True
    assert response.json()["validation"]["mode"] == "delta"


def test_delta_mode_blocks_new_violation(client, stub):
    stub.graphs["urn:ds:shacl"] = "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    stub.shacl_reports = [CONFORMING_REPORT, VIOLATION_REPORT]

    response = client.post(
        "/graph/push",
        json={
            "turtle": "<urn:a> <urn:b> <urn:c> .",
            "graph_iri": "urn:ds:holons",
            "shapes_graph": "urn:ds:shacl",
        },
        headers=auth(),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "shacl_violation"
    assert detail["violations"] == 1


def test_empty_shapes_graph_is_a_clear_error(client, stub):
    response = client.post(
        "/validate",
        json={"turtle": "<urn:a> <urn:b> <urn:c> .", "shapes_graph": "urn:ds:shacl"},
        headers=auth(),
    )
    assert response.status_code == 400
    assert "empty or absent" in response.json()["detail"]


def test_report_parsing():
    report = parse_report(VIOLATION_REPORT)
    assert report.conforms is False
    assert report.results[0]["focusNode"] == "https://example.org/sensor-A1"


# --- holon --------------------------------------------------------------------


def test_holon_requires_identity(client):
    """CHANGED 2026-08-17: /holon now requires AnimusDep, same shape as
    /endpoint's breaking change in PR #7 -- personaOverride/scope are
    per-person, so the route can't answer without knowing who's asking."""
    response = client.get("/holon", params={"iri": "urn:holon:earth"}, headers=auth())
    assert response.status_code == 401


def test_get_holon_renders_a_databook(client, stub):
    stub.construct_result = "<urn:holon:earth> a <https://w3id.org/holon/Holon> ."
    stub.select_result = {
        "results": {
            "bindings": [
                {
                    "neighbour": {"value": "urn:holon:moon"},
                    "label": {"value": "Moon"},
                    "predicate": {"value": "https://w3id.org/holon/isPartOf"},
                }
            ]
        }
    }
    response = client.get("/holon", params={"iri": "urn:holon:earth"}, headers=animus_auth())
    assert response.status_code == 200

    book = DataBook.parse(response.text)
    assert book.frontmatter["id"] == "urn:holon:earth"
    assert book.block("holon-state").lang == "turtle"
    assert "subPropertyOf*" in book.block("retrieval-query").body


def test_get_holon_with_no_persona_scopes_to_ground_truth_only(client, stub):
    """No persona switched -> scope is exactly the two ground-truth
    graphs, same as this route's behaviour before persona-scoping."""
    response = client.get("/holon", params={"iri": "urn:holon:earth"}, headers=animus_auth())
    assert response.status_code == 200
    book = DataBook.parse(response.text)
    assert book.frontmatter["scope"] == ["urn:ds:holons", "urn:ds:scene"]


def test_get_holon_with_active_persona_widens_scope(client, stub):
    """With a persona active, scope grows to include that persona's
    user-private and public tiers, ranked ahead of ground truth --
    exactly what resolve_scope_graphs promises, now proven through the
    actual route rather than only through persona_scope's own unit tests.
    """
    app = client.app
    personas: PersonaStore = app.state.personas
    personas.set(person_id="urn:ds:person:kurt", dataset="ds", persona="aimee")

    response = client.get("/holon", params={"iri": "urn:holon:earth"}, headers=animus_auth())
    assert response.status_code == 200
    book = DataBook.parse(response.text)
    assert book.frontmatter["scope"] == [
        "urn:ds:persona:aimee:user:kurt:holons",
        "urn:ds:persona:aimee:user:kurt:scene",
        "urn:ds:persona:aimee:user:public:holons",
        "urn:ds:persona:aimee:user:public:scene",
        "urn:ds:holons",
        "urn:ds:scene",
    ]


def test_bad_projection_mode(client):
    response = client.get(
        "/holon",
        params={"iri": "urn:holon:earth", "projection_mode": "wat"},
        headers=animus_auth(),
    )
    assert response.status_code == 400


# --- sequences ----------------------------------------------------------------


def test_mint_uses_dataset_scoped_counter_graph(client, stub):
    stub.select_result = {"results": {"bindings": [{"value": {"value": "7"}}]}}
    response = client.post(
        "/sequence/mint", json={"name": "FI", "pad": 3}, headers=auth({"X-Dataset-Override": "chloe"})
    )
    # the stub always reports 7, so the compare-and-set never confirms
    assert response.status_code == 409


def test_mint_success(client, stub):
    values = iter(["0", "1"])

    async def select(conn, query, *, default_graph=None):
        return {"results": {"bindings": [{"value": {"value": next(values)}}]}}

    stub.select = select  # type: ignore[method-assign]
    body = client.post("/sequence/mint", json={"name": "FI"}, headers=auth()).json()
    assert body == {
        "sequence": "FI",
        "value": 1,
        "id": "FI-0001",
        "graph": "urn:ds:sequences",
    }


# --- turtle and databook ------------------------------------------------------


def test_escaping_closes_the_literal_injection_hole():
    assert escape_literal('say "hi"\nnow') == 'say \\"hi\\"\\nnow'
    assert literal("a\nb", datatype="xsd:string") == '"a\\nb"^^xsd:string'


def test_rdf12_detection():
    assert looks_like_rdf12("<<( :a :b :c )>> :certainty 0.9 .") is True
    assert looks_like_rdf12("<urn:a> <urn:b> <urn:c> .") is False


def test_databook_round_trip():
    book = DataBook.parse(
        """---
id: https://example.org/db/1
graph:
  named_graph: urn:ds:holons
---

Prose here.

<!-- databook:id: primary-graph -->
<!-- databook:label: Primary -->
```turtle
<urn:a> <urn:b> <urn:c> .
```
"""
    )
    assert book.named_graph == "urn:ds:holons"
    assert book.block("primary-graph").body.strip() == "<urn:a> <urn:b> <urn:c> ."
    assert "Prose here." in book.body
    assert "databook:id: primary-graph" in book.render()
