"""Named-query registry tests.

The registry is served from a stub that routes on query shape, so loading,
vocabulary detection, and both binding strategies are exercised end to end
without a Fuseki.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from holonbridge.config import Settings
from holonbridge.params import ParameterError, render_term, values_clause
from holonbridge.server import create_app
from holonbridge.sparql_kind import accepts_values_clause, classify, form

TOKEN = "test-token"

HB_TYPE = "https://w3id.org/holonbridge/NamedQuery"
HQ_TYPE = "https://w3id.org/holon/query/NamedQuery"
HB_NS = "https://w3id.org/holonbridge/"
HQ_NS = "https://w3id.org/holon/query/"

HB_BODY = (
    "SELECT ?player WHERE {\n"
    "  GRAPH <urn:ds:holons> { ?player <urn:onTeam> {{team}} }\n"
    "}"
)
HQ_BODY = (
    "SELECT ?child ?label WHERE {\n"
    "  GRAPH <urn:ds:holons> { ?child ?p ?focus . OPTIONAL { ?child <urn:label> ?label } }\n"
    "}\nORDER BY ?child\nLIMIT 50"
)


def _row(**cells: str) -> dict:
    return {key: {"value": value} for key, value in cells.items()}


def registry_rows() -> list[dict]:
    """``?q ?type ?p ?o`` rows for one hb: and one hquery: query."""
    hb = "urn:ds:named-query:hb-team-roster"
    hq = f"{HQ_NS}am-get-down"
    return [
        _row(q=hb, type=HB_TYPE, p=f"{HB_NS}id", o="hb-team-roster"),
        _row(q=hb, type=HB_TYPE, p=f"{HB_NS}sparql", o=HB_BODY),
        _row(q=hb, type=HB_TYPE, p="http://www.w3.org/2000/01/rdf-schema#label", o="Team roster"),
        _row(q=hb, type=HB_TYPE, p=f"{HB_NS}queryType", o="SELECT"),
        _row(q=hq, type=HQ_TYPE, p=f"{HQ_NS}sparql", o=HQ_BODY),
        _row(q=hq, type=HQ_TYPE, p="http://www.w3.org/2000/01/rdf-schema#label", o="Descend containment"),
        _row(q=hq, type=HQ_TYPE, p=f"{HQ_NS}queryType", o="SELECT"),
    ]


def param_rows() -> list[dict]:
    """``?q ?param ?p ?o`` rows."""
    hb = "urn:ds:named-query:hb-team-roster"
    hq = f"{HQ_NS}am-get-down"
    return [
        _row(q=hb, param="_:p1", p=f"{HB_NS}name", o="team"),
        _row(q=hb, param="_:p1", p=f"{HB_NS}datatype", o="xsd:anyURI"),
        _row(q=hb, param="_:p1", p=f"{HB_NS}required", o="true"),
        _row(q=hq, param="_:p2", p=f"{HQ_NS}name", o="focus"),
        _row(q=hq, param="_:p2", p=f"{HQ_NS}datatype", o="xsd:anyURI"),
        _row(q=hq, param="_:p2", p=f"{HQ_NS}required", o="true"),
        _row(q=hq, param="_:p3", p=f"{HQ_NS}name", o="label"),
        _row(q=hq, param="_:p3", p=f"{HQ_NS}datatype", o="xsd:string"),
        _row(q=hq, param="_:p3", p=f"{HQ_NS}required", o="false"),
    ]


class RegistryStub:
    """Serves the registry, and records whatever query is executed after it."""

    def __init__(self) -> None:
        self.registry = registry_rows()
        self.params = param_rows()
        self.executed: list[str] = []
        self.updates: list[str] = []

    async def select(self, conn, query, *, default_graph=None):
        if "?link" in query:
            return {"results": {"bindings": self.params}}
        if "NamedQuery" in query:
            return {"results": {"bindings": self.registry}}
        self.executed.append(query)
        return {"results": {"bindings": []}}

    async def construct(self, conn, query, *, default_graph=None):
        self.executed.append(query)
        return "# constructed\n"

    async def update(self, conn, update):
        self.updates.append(update)

    async def get_graph(self, conn, graph_iri):
        return ""

    async def post_graph(self, conn, graph_iri, turtle):
        return None

    async def put_graph(self, conn, graph_iri, turtle):
        return None

    async def drop_graph(self, conn, graph_iri):
        return None

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        return ""

    async def ping(self, conn):
        return True

    async def aclose(self):
        return None


@pytest.fixture
def stub() -> RegistryStub:
    return RegistryStub()


@pytest.fixture
def client(stub: RegistryStub):
    app = create_app(Settings(bearer_token=TOKEN, named_query_ttl=0.0))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        yield test_client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# --- loading ------------------------------------------------------------------


def test_both_vocabularies_load(client):
    body = client.get("/named-queries", headers=auth()).json()
    assert body["count"] == 2
    by_id = {q["id"]: q for q in body["queries"]}
    assert by_id["hb-team-roster"]["vocabulary"] == "hb"
    assert by_id["am-get-down"]["vocabulary"] == "hquery"


def test_hquery_id_comes_from_the_iri_local_name(client):
    body = client.get("/named-queries", params={"vocabulary": "hquery"}, headers=auth()).json()
    assert [q["id"] for q in body["queries"]] == ["am-get-down"]
    assert body["queries"][0]["iri"].endswith("/am-get-down")


def test_parameters_load_with_their_declared_datatypes(client):
    detail = client.get("/named-query/am-get-down", headers=auth()).json()
    params = {p["name"]: p for p in detail["parameters"]}
    assert params["focus"]["datatype"] == "xsd:anyURI"
    assert params["focus"]["required"] is True
    assert params["label"]["required"] is False


def test_registry_read_failure_degrades_to_empty(client, stub):
    async def boom(conn, query, *, default_graph=None):
        from holonbridge.fuseki import FusekiError

        raise FusekiError(503, "backend down")

    stub.select = boom  # type: ignore[method-assign]
    body = client.get("/named-queries", headers=auth()).json()
    assert body["count"] == 0
    assert body["warnings"]


def test_missing_parameter_metadata_leaves_queries_runnable(client, stub):
    original = stub.select

    async def partial(conn, query, *, default_graph=None):
        if "?link" in query:
            from holonbridge.fuseki import FusekiError

            raise FusekiError(500, "parameter load failed")
        return await original(conn, query, default_graph=default_graph)

    stub.select = partial  # type: ignore[method-assign]
    body = client.get("/named-queries", headers=auth()).json()
    assert body["count"] == 2
    assert any("runnable unbound" in w for w in body["warnings"])


def test_duplicate_id_prefers_hquery_and_warns(client, stub):
    stub.registry = stub.registry + [
        _row(q="urn:ds:named-query:shadow", type=HB_TYPE, p=f"{HB_NS}id", o="am-get-down"),
        _row(q="urn:ds:named-query:shadow", type=HB_TYPE, p=f"{HB_NS}sparql", o="SELECT * WHERE { ?s ?p ?o }"),
    ]
    body = client.get("/named-queries", headers=auth()).json()
    survivor = next(q for q in body["queries"] if q["id"] == "am-get-down")
    assert survivor["vocabulary"] == "hquery"
    assert any("registered twice" in w for w in body["warnings"])


# --- binding dispatch ---------------------------------------------------------


def test_hb_query_substitutes_placeholders(client, stub):
    body = client.post(
        "/named-query/hb-team-roster/run",
        json={"params": {"team": "https://example.org/team/northbridge"}},
        headers=auth(),
    ).json()

    assert body["strategy"] == "placeholder"
    assert body["bound"] == ["team"]
    assert "{{team}}" not in body["sparql"]
    assert "<https://example.org/team/northbridge>" in body["sparql"]


def test_hquery_appends_values_after_the_solution_modifiers(client):
    body = client.post(
        "/named-query/am-get-down/run",
        json={"params": {"focus": "https://example.org/holon/earth"}},
        headers=auth(),
    ).json()

    assert body["strategy"] == "values"
    lines = body["sparql"].strip().splitlines()
    assert lines[-1].startswith("VALUES ?focus")
    assert "LIMIT 50" in body["sparql"]
    # the VALUES clause follows LIMIT, which is where SPARQL's grammar puts it
    assert body["sparql"].index("LIMIT 50") < body["sparql"].index("VALUES")


def test_unsupplied_optional_parameter_stays_unbound(client):
    body = client.post(
        "/named-query/am-get-down/run",
        json={"params": {"focus": "https://example.org/holon/earth"}},
        headers=auth(),
    ).json()
    assert body["bound"] == ["focus"]
    assert "?label" not in body["sparql"].split("VALUES")[1]


def test_both_parameters_bind_as_a_tuple(client):
    body = client.post(
        "/named-query/am-get-down/run",
        json={"params": {"focus": "https://example.org/holon/earth", "label": "Moon"}},
        headers=auth(),
    ).json()
    assert body["sparql"].strip().endswith(
        'VALUES (?focus ?label) { (<https://example.org/holon/earth> "Moon"^^<http://www.w3.org/2001/XMLSchema#string>) }'
    )


def test_missing_required_parameter_is_a_client_error(client):
    response = client.post("/named-query/am-get-down/run", json={"params": {}}, headers=auth())
    assert response.status_code == 400
    assert response.json()["detail"]["missing"] == ["focus"]


def test_undeclared_parameter_is_rejected_not_ignored(client):
    response = client.post(
        "/named-query/am-get-down/run",
        json={"params": {"focus": "https://example.org/x", "fokus": "typo"}},
        headers=auth(),
    )
    assert response.status_code == 400
    assert "does not declare fokus" in response.json()["detail"]["message"]


def test_dry_run_returns_the_bound_query_without_executing(client, stub):
    body = client.post(
        "/named-query/am-get-down/run",
        json={"params": {"focus": "https://example.org/holon/earth"}, "dry_run": True},
        headers=auth(),
    ).json()
    assert body["executed"] is False
    assert stub.executed == []


def test_run_reaches_the_backend(client, stub):
    client.post(
        "/named-query/am-get-down/run",
        json={"params": {"focus": "https://example.org/holon/earth"}},
        headers=auth(),
    )
    assert len(stub.executed) == 1
    assert "VALUES ?focus" in stub.executed[0]


def test_unknown_query_lists_what_is_available(client):
    response = client.get("/named-query/nope", headers=auth())
    assert response.status_code == 404
    assert "am-get-down" in response.json()["detail"]["available"]


def test_reload_invalidates_the_cache(client, stub):
    client.get("/named-queries", headers=auth())
    stub.registry = []
    body = client.post("/named-queries/reload", headers=auth()).json()
    assert body["count"] == 0


# --- injection and lexical guards ---------------------------------------------


def test_iri_parameter_cannot_smuggle_sparql(client):
    response = client.post(
        "/named-query/am-get-down/run",
        json={"params": {"focus": "https://example.org/x> } INSERT DATA { <urn:a> <urn:b> <urn:c> } #"}},
        headers=auth(),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "parameter_error"


def test_iri_parameter_must_have_a_scheme():
    with pytest.raises(ParameterError):
        render_term("not-an-iri", "xsd:anyURI")


def test_datetime_without_a_timezone_is_refused():
    with pytest.raises(ParameterError) as exc:
        render_term("2026-07-25T09:00:00", "xsd:dateTime")
    assert "timezone" in str(exc.value)
    assert render_term("2026-07-25T09:00:00Z", "xsd:dateTime").endswith("dateTime>")


def test_integer_lexical_form_is_checked():
    with pytest.raises(ParameterError):
        render_term("twelve", "xsd:integer")
    assert render_term(12, "xsd:integer") == "12"


def test_string_parameters_are_escaped():
    rendered = render_term('he said "stop"\nthen left', None)
    assert rendered == '"he said \\"stop\\"\\nthen left"'


def test_values_clause_shapes():
    assert values_clause([("a", "<urn:x>")]) == "VALUES ?a { <urn:x> }"
    assert values_clause([("a", "<urn:x>"), ("b", '"y"')]) == 'VALUES (?a ?b) { (<urn:x> "y") }'
    assert values_clause([]) == ""
    with pytest.raises(ParameterError):
        values_clause([("bad name", "<urn:x>")])


# --- sparql classification ----------------------------------------------------


def test_classify_sees_past_the_prologue_and_comments():
    query = (
        "# leading comment\n"
        "PREFIX ex: <https://example.org/>\n"
        "BASE <https://example.org/>\n"
        "SELECT * WHERE { ?s ?p ?o }"
    )
    assert classify(query) == "read"
    assert form(query) == "SELECT"


def test_classify_recognises_updates():
    assert classify("PREFIX ex: <urn:x#>\nINSERT DATA { <urn:a> <urn:b> <urn:c> }") == "update"
    assert classify("WITH <urn:g> DELETE { ?s ?p ?o } WHERE { ?s ?p ?o }") == "update"
    assert form("DROP GRAPH <urn:g>") == "UPDATE"


def test_values_cannot_be_appended_to_an_update_or_a_bound_query():
    assert accepts_values_clause("SELECT * WHERE { ?s ?p ?o }") is True
    assert accepts_values_clause("INSERT DATA { <urn:a> <urn:b> <urn:c> }") is False
    assert accepts_values_clause("SELECT * WHERE { ?s ?p ?o } VALUES ?s { <urn:a> }") is False


def test_comment_stripper_leaves_iris_and_literals_intact():
    from holonbridge.sparql_kind import strip_comments

    query = (
        'PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>  # trailing comment\n'
        'SELECT * WHERE { ?s ?p "a # not a comment" FILTER(?n < 5) }'
    )
    stripped = strip_comments(query)
    assert "XMLSchema#>" in stripped
    assert "a # not a comment" in stripped
    assert "trailing comment" not in stripped
    assert "?n < 5" in stripped
