"""JSON-LD input and output.

Input runs the real ``POST /graph/push`` route with the store write replaced
by a recorder, so what is asserted is exactly the Turtle that would reach
Fuseki, and that a refused payload reaches nothing. Output runs the real
``/sparql/construct`` and ``GET /graph`` routes against a stub Fuseki client
that can answer in Turtle or JSON-LD, or fail, as a real one might.

Not covered, and said plainly: nothing here talks to a live Fuseki. The ``jena``
output engine is tested against a stub that returns whatever JSON the test gives
it, so the claim that Jena's writer produces JSON-LD this code accepts is
untested.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from rdflib import Graph
from rdflib.compare import isomorphic

from holonbridge import audit, jsonld
from holonbridge.acl import Animus
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.deps import get_client, require_animus, require_conn
from holonbridge.fetch import SourceFetchError
from holonbridge.fuseki import FusekiError
from holonbridge.routes import graphs, sparql
from holonbridge.turtle import TurtleSyntaxError, from_json_ld

HOLON = "https://w3id.org/holon/"
HOLONS = "urn:example:holons"
JANE = "urn:example:person:jane"
KURT = "urn:example:person:kurt"
GROUND = "urn:example:scene"
KURT_HOLONS = "urn:example:persona:phoebe:user:kurt:holons"
PERSONA = "urn:example:persona:phoebe"

TURTLE = """@prefix ex: <http://example.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:sun a ex:Star ; ex:label "Sun"@en ; ex:mass 1.989e30 ; ex:rank 1 .
"""
SIMPLE = {"@context": {"@vocab": "http://example.org/"}, "@id": "http://example.org/a", "p": "x"}
TRIPLE = "<http://example.org/a> <http://example.org/p> \"x\""

SELECT_FIXTURE = f"""
@prefix holon: <{HOLON}> .
<{JANE}> a holon:Person .
<{KURT}> a holon:Person .
<{PERSONA}> a holon:Persona .
"""


# --- stubs ---------------------------------------------------------------------


class Stub:
    """A Fuseki client double: Turtle by default, JSON-LD when asked, or an error."""

    def __init__(self) -> None:
        from rdflib import Dataset, URIRef

        self.ds = Dataset()
        self.ds.get_context(URIRef(HOLONS)).parse(data=SELECT_FIXTURE, format="turtle")
        self.turtle = TURTLE
        self.jsonld_reply: str | FusekiError = FusekiError(406, "no such format")
        self.calls: list[tuple[str, str]] = []  # (method, accept)

    async def select(self, conn, query, *, default_graph=None):
        result = self.ds.query(query)
        if result.type == "ASK":
            return {"boolean": bool(result.askAnswer)}
        bindings = []
        for row in result:
            binding = {}
            for var in result.vars:
                val = row[var]
                if val is None:
                    continue
                text = str(val)
                binding[str(var)] = {
                    "type": "uri" if text.startswith(("http", "urn")) else "literal",
                    "value": text,
                }
            bindings.append(binding)
        return {"results": {"bindings": bindings}}

    async def construct(self, conn, query, *, default_graph=None, timeout=None, accept="text/turtle"):
        self.calls.append(("construct", accept))
        return self._answer(accept)

    async def get_graph(self, conn, graph_iri, *, accept="text/turtle"):
        self.calls.append(("get_graph", accept))
        return self._answer(accept)

    def _answer(self, accept: str) -> str:
        if accept == "application/ld+json":
            if isinstance(self.jsonld_reply, FusekiError):
                raise self.jsonld_reply
            return self.jsonld_reply
        return self.turtle


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    audit._level_cache.clear()
    monkeypatch.delenv("HOLONBRIDGE_AUDIT_LEVEL", raising=False)


@pytest.fixture
def stub() -> Stub:
    return Stub()


@pytest.fixture
def written(monkeypatch):
    """Replace the store write with a recorder."""
    calls: list[dict] = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(graphs, "write_turtle_to_graph", fake)
    return calls


@pytest.fixture
def make_client(stub):
    def build(person: str = JANE, **settings) -> TestClient:
        app = FastAPI()
        app.state.settings = Settings(**settings)
        app.include_router(graphs.router)
        app.include_router(sparql.router)
        conn = Conn(base_url="http://localhost:3030", dataset="example", overridden=False,
                    bank_name="local")

        def _conn(request: Request) -> Conn:
            request.state.conn = conn
            return conn

        async def _animus(request: Request) -> Animus:
            animus = Animus("x", "GitHubIdentity", person=person, real_person=person,
                            acting_as=False)
            request.state.animus = animus
            return animus

        app.dependency_overrides[require_conn] = _conn
        app.dependency_overrides[get_client] = lambda: stub
        app.dependency_overrides[require_animus] = _animus
        return TestClient(app, raise_server_exceptions=False)

    return build


@pytest.fixture
def client(make_client):
    return make_client()


def push_json_ld(client, doc, **extra):
    payload = doc if isinstance(doc, str) else json.dumps(doc)
    body = {"data": payload, "format": "json-ld", "graph_iri": "urn:example:g"}
    body.update(extra)
    return client.post("/graph/push", json=body)


CONSTRUCT_GROUND = f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{GROUND}> {{ ?s ?p ?o }} }}"
CONSTRUCT_PRIVATE = f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{KURT_HOLONS}> {{ ?s ?p ?o }} }}"


def construct(client, query=CONSTRUCT_GROUND, **extra):
    return client.post("/sparql/construct", json={"query": query, **extra})


# --- input: what is accepted ---------------------------------------------------


def test_json_ld_is_converted_to_turtle_before_the_write(client, written):
    response = push_json_ld(client, SIMPLE)
    assert response.status_code == 200
    (call,) = written
    graph = Graph().parse(data=call["turtle"], format="turtle")
    assert len(graph) == 1
    assert next(iter(graph))[2].toPython() == "x"


def test_a_top_level_graph_array_is_still_the_default_graph(client, written):
    doc = {"@context": SIMPLE["@context"], "@graph": [{"@id": "http://example.org/a", "p": "x"}]}
    assert push_json_ld(client, doc).status_code == 200
    assert len(written) == 1


def test_turtle_is_untouched_and_remains_the_default(client, written):
    response = client.post("/graph/push", json={"turtle": TURTLE, "graph_iri": "urn:example:g"})
    assert response.status_code == 200
    assert written[0]["turtle"] == TURTLE


def test_turtle_may_be_sent_in_the_data_field_too(client, written):
    response = client.post("/graph/push", json={"data": TURTLE, "graph_iri": "urn:example:g"})
    assert response.status_code == 200
    assert written[0]["turtle"] == TURTLE


@pytest.mark.parametrize(
    "body",
    [
        {"graph_iri": "urn:example:g"},
        {"turtle": "a", "data": "b", "graph_iri": "urn:example:g"},
    ],
)
def test_exactly_one_payload_field_is_required(client, written, body):
    assert client.post("/graph/push", json=body).status_code == 422
    assert written == []


def test_an_unknown_format_is_refused(client, written):
    response = client.post(
        "/graph/push", json={"turtle": TURTLE, "graph_iri": "urn:example:g", "format": "rdf-xml"}
    )
    assert response.status_code == 422
    assert written == []


# --- input: what is refused ----------------------------------------------------


def test_a_remote_context_is_refused_and_nothing_is_written(client, written):
    response = push_json_ld(client, {"@context": "http://evil.example/ctx", "@id": "http://e/a"})
    assert response.status_code == 400
    assert "remote @context" in response.json()["detail"]
    assert "evil.example" in response.json()["detail"]
    assert written == []


def test_a_remote_context_hidden_in_a_term_definition_is_refused(client, written):
    doc = {
        "@context": {
            "@vocab": "http://example.org/",
            "knows": {"@id": "http://example.org/knows", "@context": "http://evil.example/scoped"},
        },
        "@id": "http://example.org/a",
        "knows": "x",
    }
    assert push_json_ld(client, doc).status_code == 400
    assert written == []


def test_a_remote_context_nested_in_a_node_is_refused(client, written):
    doc = {
        "@graph": [
            {
                "@id": "http://example.org/a",
                "http://example.org/p": {
                    "@context": "http://evil.example/inner",
                    "@id": "http://example.org/z",
                },
            }
        ]
    }
    assert push_json_ld(client, doc).status_code == 400
    assert written == []


def test_a_remote_context_in_a_context_list_is_refused(client, written):
    doc = {"@context": [{"@vocab": "http://example.org/"}, "http://evil.example/ctx"], "p": "x"}
    assert push_json_ld(client, doc).status_code == 400
    assert written == []


def test_the_refusal_does_not_touch_the_network(client, written, monkeypatch):
    from holonbridge import fetch

    async def boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the bridge fetched a remote context")

    monkeypatch.setattr(fetch, "fetch_source", boom)
    assert push_json_ld(client, {"@context": "http://evil.example/ctx"}).status_code == 400


def test_import_is_refused(client, written):
    doc = {"@context": {"@import": "http://x.example/ctx", "@vocab": "http://example.org/"}, "p": "x"}
    response = push_json_ld(client, doc)
    assert response.status_code == 400
    assert "@import" in response.json()["detail"]
    assert written == []


def test_named_graphs_are_refused_not_silently_dropped(client, written):
    doc = {
        "@context": SIMPLE["@context"],
        "@id": "http://example.org/g1",
        "@graph": [{"@id": "http://example.org/a", "p": "x"}],
    }
    response = push_json_ld(client, doc)
    assert response.status_code == 400
    assert "named graphs" in response.json()["detail"]
    assert "http://example.org/g1" in response.json()["detail"]
    assert written == []


def test_a_default_graph_alongside_a_named_graph_is_still_refused(client, written):
    doc = [
        {"@id": "http://example.org/a", "http://example.org/p": [{"@value": "x"}]},
        {
            "@id": "http://example.org/g1",
            "@graph": [{"@id": "http://example.org/b", "http://example.org/p": [{"@value": "y"}]}],
        },
    ]
    assert push_json_ld(client, doc).status_code == 400
    assert written == []


def test_invalid_json_is_a_400(client, written):
    response = push_json_ld(client, "{not json")
    assert response.status_code == 400
    assert "not valid JSON" in response.json()["detail"]
    assert written == []


def test_a_scalar_document_is_a_400(client, written):
    assert push_json_ld(client, '"just a string"').status_code == 400


def test_an_oversized_document_is_a_413(make_client, written):
    client = make_client(jsonld_max_bytes=100)
    response = push_json_ld(client, {"@context": SIMPLE["@context"], "p": "x" * 500})
    assert response.status_code == 413
    assert written == []


def test_a_deeply_nested_document_is_refused_not_a_crash(client, written):
    text = "[" * 5000 + "]" * 5000
    response = push_json_ld(client, text)
    assert response.status_code == 400
    assert written == []


@pytest.mark.parametrize(
    "doc",
    [
        {},
        [],
        {"@context": {"@vocab": "http://example.org/"}},
        {"@id": "http://example.org/a", "name": "unmapped, so dropped by JSON-LD"},
    ],
)
def test_a_document_that_converts_to_nothing_is_refused_not_written(client, written, doc):
    response = push_json_ld(client, doc, mode="replace")
    assert response.status_code == 400
    assert "no triples" in response.json()["detail"]
    assert written == []  # in particular, a replace never empties the graph


# --- input: opt-in context fetching --------------------------------------------

CTX_URL = "https://contexts.example/ex.jsonld"
CTX_DOC = json.dumps({"@context": {"@vocab": "http://example.org/"}})


def predicates(turtle: str) -> set[str]:
    return {str(p) for _, p, _ in Graph().parse(data=turtle, format="turtle")}


def settings_with_fetch(**extra) -> Settings:
    return Settings(jsonld_fetch_contexts=True, **extra)


@pytest.mark.asyncio
async def test_with_fetching_on_a_context_is_fetched_and_inlined():
    seen: list[str] = []

    async def fetch(url):
        seen.append(url)
        return CTX_DOC, "application/ld+json"

    turtle = await jsonld.json_ld_to_turtle(
        json.dumps({"@context": CTX_URL, "@id": "http://example.org/a", "p": "x"}),
        settings=settings_with_fetch(),
        fetch=fetch,
    )
    assert seen == [CTX_URL]
    assert predicates(turtle) == {"http://example.org/p"}


@pytest.mark.asyncio
async def test_a_context_used_twice_is_fetched_once():
    seen: list[str] = []

    async def fetch(url):
        seen.append(url)
        return CTX_DOC, "application/ld+json"

    doc = {
        "@graph": [
            {"@context": CTX_URL, "@id": "http://example.org/a", "p": "x"},
            {"@context": CTX_URL, "@id": "http://example.org/b", "p": "y"},
        ]
    }
    await jsonld.json_ld_to_turtle(json.dumps(doc), settings=settings_with_fetch(), fetch=fetch)
    assert seen == [CTX_URL]


@pytest.mark.asyncio
async def test_a_fetch_failure_is_a_json_ld_error():
    async def fetch(url):
        raise SourceFetchError(url, "refused: not a public address")

    with pytest.raises(jsonld.JsonLdError, match="could not fetch json-ld @context"):
        await jsonld.json_ld_to_turtle(
            json.dumps({"@context": CTX_URL, "p": "x"}), settings=settings_with_fetch(), fetch=fetch
        )


@pytest.mark.asyncio
async def test_the_default_fetcher_goes_through_the_egress_policy():
    """No fetch seam given: the real ``fetch_source`` runs and refuses a private
    address before any connection is made."""
    with pytest.raises(jsonld.JsonLdError, match="refused"):
        await jsonld.json_ld_to_turtle(
            json.dumps({"@context": "http://127.0.0.1:9/ctx", "p": "x"}),
            settings=settings_with_fetch(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ftp://contexts.example/ctx", "file:///etc/passwd", "relative.jsonld"])
async def test_only_absolute_http_contexts_are_fetched(url):
    async def fetch(u):  # pragma: no cover - must not run
        raise AssertionError("fetched a non-http context")

    with pytest.raises(jsonld.JsonLdError, match="absolute http"):
        await jsonld.json_ld_to_turtle(
            json.dumps({"@context": url, "p": "x"}), settings=settings_with_fetch(), fetch=fetch
        )


@pytest.mark.asyncio
async def test_a_fetched_context_that_names_another_remote_context_is_followed_a_few_times():
    async def fetch(url):
        if url == "https://c.example/one":
            return json.dumps({"@context": "https://c.example/two"}), "application/ld+json"
        return CTX_DOC, "application/ld+json"

    turtle = await jsonld.json_ld_to_turtle(
        json.dumps({"@context": "https://c.example/one", "@id": "http://example.org/a", "p": "x"}),
        settings=settings_with_fetch(),
        fetch=fetch,
    )
    assert predicates(turtle) == {"http://example.org/p"}


@pytest.mark.asyncio
async def test_an_endless_chain_of_contexts_is_cut_off():
    async def fetch(url):
        n = int(url.rsplit("/", 1)[1])
        return json.dumps({"@context": f"https://c.example/{n + 1}"}), "application/ld+json"

    with pytest.raises(jsonld.JsonLdError, match="remote @context"):
        await jsonld.json_ld_to_turtle(
            json.dumps({"@context": "https://c.example/0", "p": "x"}),
            settings=settings_with_fetch(),
            fetch=fetch,
        )


@pytest.mark.asyncio
async def test_too_many_distinct_contexts_are_refused():
    async def fetch(url):
        return CTX_DOC, "application/ld+json"

    doc = {"@graph": [{"@context": f"https://c.example/{i}", "p": "x"} for i in range(25)]}
    with pytest.raises(jsonld.JsonLdError, match="more than"):
        await jsonld.json_ld_to_turtle(json.dumps(doc), settings=settings_with_fetch(), fetch=fetch)


@pytest.mark.asyncio
async def test_a_fetched_context_must_be_a_context_document():
    async def fetch(url):
        return json.dumps({"hello": "world"}), "application/json"

    with pytest.raises(jsonld.JsonLdError, match="no top-level @context"):
        await jsonld.json_ld_to_turtle(
            json.dumps({"@context": CTX_URL, "p": "x"}), settings=settings_with_fetch(), fetch=fetch
        )


@pytest.mark.asyncio
async def test_a_fetched_context_over_the_size_cap_is_refused():
    async def fetch(url):
        return json.dumps({"@context": {"@vocab": "http://example.org/" + "x" * 500}}), ""

    with pytest.raises(jsonld.JsonLdTooLarge):
        await jsonld.json_ld_to_turtle(
            json.dumps({"@context": CTX_URL, "p": "x"}),
            settings=settings_with_fetch(jsonld_max_bytes=300),
            fetch=fetch,
        )


@pytest.mark.asyncio
async def test_import_stays_refused_with_fetching_on():
    async def fetch(url):  # pragma: no cover
        return CTX_DOC, ""

    doc = {"@context": {"@import": CTX_URL}, "p": "x"}
    with pytest.raises(jsonld.JsonLdError, match="@import"):
        await jsonld.json_ld_to_turtle(json.dumps(doc), settings=settings_with_fetch(), fetch=fetch)


def test_the_synchronous_helper_never_fetches():
    with pytest.raises(TurtleSyntaxError, match="remote @context"):
        from_json_ld(json.dumps({"@context": CTX_URL, "p": "x"}))


def test_the_synchronous_helper_still_converts_a_plain_document():
    assert predicates(from_json_ld(json.dumps(SIMPLE))) == {"http://example.org/p"}


# --- output: Turtle is unchanged -----------------------------------------------


def test_turtle_output_is_unchanged(client, stub):
    response = construct(client)
    assert response.status_code == 200
    assert response.text == TURTLE
    assert response.headers["content-type"].startswith("text/plain")
    assert "x-jsonld-engine" not in response.headers
    assert stub.calls == [("construct", "text/turtle")]


# --- output: rdflib engine -----------------------------------------------------


def test_compact_json_ld_round_trips_the_same_graph(client):
    response = construct(client, format="json-ld")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")
    assert response.headers["x-jsonld-engine"] == "rdflib"
    again = Graph().parse(data=response.text, format="json-ld")
    assert isomorphic(again, Graph().parse(data=TURTLE, format="turtle"))


def test_compact_output_uses_the_turtles_prefixes_as_its_context(client):
    document = json.loads(construct(client, format="json-ld").text)
    assert document["@context"]["ex"] == "http://example.org/"
    assert "ex:Star" in json.dumps(document)


def test_compact_output_carries_no_prefixes_the_result_did_not_declare(client):
    context = json.loads(construct(client, format="json-ld").text)["@context"]
    assert set(context) <= {"ex", "xsd"}


def test_expanded_output_has_no_context_and_full_iris(client):
    response = construct(client, format="json-ld", jsonld_form="expanded")
    assert response.status_code == 200
    text = response.text
    assert "@context" not in text
    assert "http://example.org/Star" in text
    assert isomorphic(
        Graph().parse(data=text, format="json-ld"), Graph().parse(data=TURTLE, format="turtle")
    )


def test_an_empty_result_is_an_empty_array(client, stub):
    stub.turtle = ""
    response = construct(client, format="json-ld")
    assert response.status_code == 200
    assert json.loads(response.text) == []


def test_a_result_with_rdf12_triple_terms_is_refused_with_a_pointer_to_turtle(client, stub):
    stub.turtle = "@prefix ex: <http://example.org/> .\nex:a ex:says <<( ex:b ex:c ex:d )>> .\n"
    response = construct(client, format="json-ld")
    assert response.status_code == 422
    assert "triple terms" in response.json()["detail"]
    assert "format=turtle" in response.json()["detail"]


def test_the_same_result_is_still_available_as_turtle(client, stub):
    stub.turtle = "@prefix ex: <http://example.org/> .\nex:a ex:says <<( ex:b ex:c ex:d )>> .\n"
    assert construct(client).status_code == 200


def test_an_oversized_result_is_a_413(make_client, stub):
    client = make_client(jsonld_max_bytes=50)
    response = construct(client, format="json-ld")
    assert response.status_code == 413
    assert "format" not in response.json()["detail"] or "Turtle" in response.json()["detail"]


def test_unparseable_turtle_from_the_store_is_reported_not_a_500(client, stub):
    stub.turtle = "this is not turtle at all {{{"
    response = construct(client, format="json-ld")
    assert response.status_code == 400
    assert "could not read the store's Turtle" in response.json()["detail"]


def test_an_unknown_output_format_is_refused(client):
    assert construct(client, format="rdf-xml").status_code == 422


# --- output: jena engine -------------------------------------------------------

JENA_DOC = json.dumps(
    {"@context": {"ex": "http://example.org/"}, "@id": "ex:sun", "@type": "ex:Star"}
)


def test_the_jena_engine_returns_fusekis_json_ld_verbatim(make_client, stub):
    stub.jsonld_reply = JENA_DOC
    response = construct(make_client(jsonld_output_engine="jena"), format="json-ld")
    assert response.status_code == 200
    assert response.text == JENA_DOC
    assert response.headers["x-jsonld-engine"] == "jena"
    assert stub.calls == [("construct", "application/ld+json")]  # no Turtle round trip


@pytest.mark.parametrize("status", [400, 406, 415, 501])
def test_the_jena_engine_falls_back_to_rdflib_when_fuseki_cannot_write_json_ld(
    make_client, stub, status, caplog
):
    stub.jsonld_reply = FusekiError(status, "nope")
    with caplog.at_level(logging.WARNING):
        response = construct(make_client(jsonld_output_engine="jena"), format="json-ld")
    assert response.status_code == 200
    assert response.headers["x-jsonld-engine"] == "rdflib (jena unavailable)"
    assert isomorphic(
        Graph().parse(data=response.text, format="json-ld"), Graph().parse(data=TURTLE, format="turtle")
    )
    assert [c[1] for c in stub.calls] == ["application/ld+json", "text/turtle"]
    assert "using rdflib" in caplog.text


def test_the_jena_engine_does_not_hide_a_real_upstream_failure(make_client, stub):
    stub.jsonld_reply = FusekiError(500, "boom")
    response = construct(make_client(jsonld_output_engine="jena"), format="json-ld")
    assert response.status_code == 500
    assert [c[1] for c in stub.calls] == ["application/ld+json"]  # no silent retry


def test_the_jena_engine_falls_back_when_fuseki_returns_something_that_is_not_json(make_client, stub):
    stub.jsonld_reply = "<html>oops</html>"
    response = construct(make_client(jsonld_output_engine="jena"), format="json-ld")
    assert response.status_code == 200
    assert response.headers["x-jsonld-engine"] == "rdflib (jena unavailable)"


def test_the_jena_engine_is_not_used_for_the_expanded_form(make_client, stub):
    stub.jsonld_reply = JENA_DOC
    response = construct(
        make_client(jsonld_output_engine="jena"), format="json-ld", jsonld_form="expanded"
    )
    assert response.headers["x-jsonld-engine"] == "rdflib"
    assert [c[1] for c in stub.calls] == ["text/turtle"]


def test_an_oversized_jena_result_is_a_413_not_a_fallback(make_client, stub):
    stub.jsonld_reply = json.dumps({"@context": {}, "x": "y" * 500})
    client = make_client(jsonld_output_engine="jena", jsonld_max_bytes=100)
    assert construct(client, format="json-ld").status_code == 413


def test_the_jena_engine_cannot_ship_rdf12_as_json_ld_via_the_fallback(make_client, stub):
    stub.jsonld_reply = FusekiError(400, "triple term not supported")
    stub.turtle = "@prefix ex: <http://example.org/> .\nex:a ex:says <<( ex:b ex:c ex:d )>> .\n"
    response = construct(make_client(jsonld_output_engine="jena"), format="json-ld")
    assert response.status_code == 422


# --- output: GET /graph --------------------------------------------------------


def test_graph_read_can_return_json_ld(client):
    response = client.get("/graph", params={"iri": GROUND, "format": "json-ld"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/ld+json")
    assert isomorphic(
        Graph().parse(data=response.text, format="json-ld"), Graph().parse(data=TURTLE, format="turtle")
    )


def test_graph_read_defaults_to_turtle(client):
    response = client.get("/graph", params={"iri": GROUND})
    assert response.text == TURTLE
    assert response.headers["content-type"].startswith("text/plain")


def test_graph_read_expanded(client):
    response = client.get(
        "/graph", params={"iri": GROUND, "format": "json-ld", "jsonld_form": "expanded"}
    )
    assert "@context" not in response.text


def test_graph_read_with_the_jena_engine(make_client, stub):
    stub.jsonld_reply = JENA_DOC
    client = make_client(jsonld_output_engine="jena")
    response = client.get("/graph", params={"iri": GROUND, "format": "json-ld"})
    assert response.text == JENA_DOC
    assert response.headers["x-jsonld-engine"] == "jena"


# --- output: authorisation comes first -----------------------------------------


def test_a_refused_construct_is_refused_in_json_ld_too_and_never_runs(client, stub):
    response = construct(client, CONSTRUCT_PRIVATE, format="json-ld")
    assert response.status_code == 403
    assert stub.calls == []


def test_a_refused_graph_read_is_refused_in_json_ld_too_and_never_fetched(client, stub):
    response = client.get("/graph", params={"iri": KURT_HOLONS, "format": "json-ld"})
    assert response.status_code == 403
    assert stub.calls == []


def test_an_app_with_no_settings_still_serves_a_turtle_read(stub):
    """The read routes did not need ``app.state.settings`` before this change."""
    app = FastAPI()
    app.include_router(graphs.router)
    conn = Conn(base_url="http://localhost:3030", dataset="example", overridden=False,
                bank_name="local")

    def _conn(request: Request) -> Conn:
        request.state.conn = conn
        return conn

    async def _animus(request: Request) -> Animus:
        animus = Animus("x", "GitHubIdentity", person=JANE, real_person=JANE, acting_as=False)
        request.state.animus = animus
        return animus

    app.dependency_overrides[require_conn] = _conn
    app.dependency_overrides[get_client] = lambda: stub
    app.dependency_overrides[require_animus] = _animus
    response = TestClient(app).get("/graph", params={"iri": GROUND})
    assert response.status_code == 200
    assert response.text == TURTLE


# --- settings ------------------------------------------------------------------


def test_the_defaults_are_the_safe_ones(monkeypatch):
    from holonbridge import config

    for name in ("JSONLD_FETCH_CONTEXTS", "JSONLD_MAX_BYTES", "JSONLD_OUTPUT_ENGINE"):
        monkeypatch.delenv(name, raising=False)
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        assert settings.jsonld_fetch_contexts is False
        assert settings.jsonld_output_engine == "rdflib"
        assert settings.jsonld_max_bytes == 5 * 1024 * 1024
    finally:
        config.get_settings.cache_clear()


def test_the_settings_are_read_from_the_environment(monkeypatch):
    from holonbridge import config

    monkeypatch.setenv("JSONLD_FETCH_CONTEXTS", "true")
    monkeypatch.setenv("JSONLD_MAX_BYTES", "1234")
    monkeypatch.setenv("JSONLD_OUTPUT_ENGINE", " Jena ")
    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        assert settings.jsonld_fetch_contexts is True
        assert settings.jsonld_max_bytes == 1234
        assert settings.jsonld_output_engine == "jena"
    finally:
        config.get_settings.cache_clear()


def test_an_unknown_engine_is_a_startup_error_not_a_silent_default(monkeypatch):
    from holonbridge import config

    monkeypatch.setenv("JSONLD_OUTPUT_ENGINE", "pyld")
    config.get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="JSONLD_OUTPUT_ENGINE"):
            config.get_settings()
    finally:
        config.get_settings.cache_clear()


# --- MCP surface ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mcp_tools_send_what_the_routes_expect(monkeypatch):
    from holonbridge_mcp.tools import core

    sent: list[tuple] = []

    async def fake_call(method, path, **kwargs):
        sent.append((method, path, kwargs.get("json_body")))
        return {"ok": True}

    monkeypatch.setattr(core, "_call", fake_call)
    await core.push_json_ld(json_ld="{}", graph_iri="urn:example:g")
    await core.sparql_construct("CONSTRUCT {} WHERE {}", format="json-ld", jsonld_form="expanded")

    (_, push_path, push_body), (_, construct_path, construct_body) = sent
    assert push_path == "/graph/push"
    assert push_body["format"] == "json-ld" and push_body["data"] == "{}"
    assert "turtle" not in push_body
    assert construct_path == "/sparql/construct"
    assert construct_body["format"] == "json-ld" and construct_body["jsonld_form"] == "expanded"
