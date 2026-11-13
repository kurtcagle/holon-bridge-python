"""``get_holon``'s as-of path: what it reads, what it reports, what it refuses.

Added 2026-09-30 alongside exposing ``observed_at`` on the MCP ``get_holon``
tool. Until then only the REST route reached the as-of path, and nothing
tested it. Three things in it were wrong or misleading and are pinned here:

- the frontmatter ``scope`` listed the caller's persona scope although the
  as-of query never reads it;
- a list-mode (``holon:ListAccumulator``) fluent came back as its last
  member touched rather than the set as it stood;
- a timezone-naive timestamp compared against the stored, timezone-aware
  ``hevt:assertedDateTime`` values, which SPARQL treats as an error and
  ``FILTER`` turns into "no rows" -- an empty projection with no warning.

``ClosedWorld`` is a real in-memory rdflib store, so the actual
``as_of_query`` and the actual guard query run against it; nothing about the
query text is mocked.

One limit, found while writing these: rdflib mis-evaluates the nested
``FILTER`` inside ``as_of_query``'s ``FILTER NOT EXISTS`` (it does not
substitute the outer ``?assertedAt`` into it), so it returns a superseded
value alongside the current one. Jena gets it right. The fixture below
therefore holds a single assertion visible at the times these tests read, and
the supersession behaviour and the silent-empty naive-timestamp case are
pinned against real Fuseki in ``test_get_holon_asof_live.py`` instead.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Dataset, Graph, Namespace, URIRef

from holonbridge.conn import Conn
from holonbridge.deps import get_client, get_personas, require_animus, require_conn
from holonbridge.acl import Animus
from holonbridge.holon import AsOfUnsupportedError, get_holon
from holonbridge.persona_scope import resolve_scope_graphs
from holonbridge.persona_state import PersonaStore
from holonbridge.routes import holon_routes

HOLON = Namespace("https://w3id.org/holon/")
CONN = Conn(base_url="http://fuseki.test", dataset="example", overridden=False, bank_name="local")
HOLONS = CONN.holons_graph
ONTOLOGY = CONN.ontology_graph
EVENTS = CONN.graph("events")
SCENE = CONN.scene_graph

ACCOUNT = "urn:example:account"
BALANCE = "urn:example:account:balance"
CLUB = "urn:example:club"
MEMBERS = "urn:example:club:members"

HOLONS_TTL = f"""
@prefix holon: <https://w3id.org/holon/> .
@prefix ex: <https://example.org/> .
<{ACCOUNT}> a ex:Account ; ex:balance <{BALANCE}> .
<{BALANCE}> a holon:Fluent ; holon:fluentProperty ex:BalanceProperty .
<{CLUB}> a ex:Club ; ex:members <{MEMBERS}> .
<{MEMBERS}> a holon:Fluent ; holon:fluentProperty ex:MembersProperty .
"""

ONTOLOGY_TTL = """
@prefix holon: <https://w3id.org/holon/> .
@prefix ex: <https://example.org/> .
ex:BalanceProperty holon:fluentOperationMode holon:NumericAccumulator .
ex:MembersProperty holon:fluentOperationMode holon:ListAccumulator .
"""

# Two balance assertions, exactly as fluent.update_fluent writes them:
# timezone-aware assertedDateTime, chained by supersedes.
EVENTS_TTL = f"""
@prefix hevt: <https://w3id.org/holon/event/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<urn:example:event:1> a hevt:StateAssertion ;
    hevt:forFluent <{BALANCE}> ;
    hevt:hasValue "100"^^xsd:integer ;
    hevt:assertedDateTime "2026-09-01T00:00:00+00:00"^^xsd:dateTime .
<urn:example:event:2> a hevt:StateAssertion ;
    hevt:forFluent <{BALANCE}> ;
    hevt:hasValue "250"^^xsd:integer ;
    hevt:assertedDateTime "2026-09-10T00:00:00+00:00"^^xsd:dateTime ;
    hevt:supersedes <urn:example:event:1> .
"""

SCENE_TTL = f"""
@prefix holon: <https://w3id.org/holon/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{BALANCE}> holon:currentValue "250"^^xsd:integer .
"""


class ClosedWorld:
    """rdflib-backed stand-in for FusekiClient. Records CONSTRUCT calls so a
    test can assert that a refused read never got as far as running one."""

    def __init__(self) -> None:
        self.ds = Dataset()
        for iri, ttl in (
            (HOLONS, HOLONS_TTL),
            (ONTOLOGY, ONTOLOGY_TTL),
            (EVENTS, EVENTS_TTL),
            (SCENE, SCENE_TTL),
        ):
            self.ds.get_context(URIRef(iri)).parse(data=ttl, format="turtle")
        self.constructs: list[str] = []

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

    async def construct(self, conn, query, *, default_graph=None):
        self.constructs.append(query)
        result = self.ds.query(query)
        return result.graph.serialize(format="turtle")


@pytest.fixture
def world() -> ClosedWorld:
    return ClosedWorld()


def _state(book) -> Graph:
    block = next(b for b in book.blocks if b.id == "holon-state")
    return Graph().parse(data=block.body, format="turtle")


def _current_value(book, fluent: str = BALANCE) -> str | None:
    values = list(_state(book).objects(URIRef(fluent), HOLON.currentValue))
    return str(values[0]) if values else None


def _ground_truth():
    return resolve_scope_graphs(CONN, person_id=None, persona=None)


def _persona_scope():
    return resolve_scope_graphs(CONN, person_id="urn:example:person:kurt", persona="phoebe")


# --- what an as-of read returns -------------------------------------------


async def test_asof_reads_the_value_at_that_moment(world):
    """Before the second assertion, so only one is visible -- see the module
    docstring for why supersession is tested against real Fuseki."""
    book = await get_holon(
        world, CONN, holon_iri=ACCOUNT, scope=_ground_truth(),
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert _current_value(book) == "100"
    assert "250" not in _state(book).serialize(format="turtle")


async def test_naive_timestamp_is_read_as_utc_not_silently_empty(world):
    """The regression the normalisation exists for: a naive datetime against
    the stored aware ones is a SPARQL comparison error, so without it this
    returns no fluent value at all instead of 100."""
    book = await get_holon(
        world, CONN, holon_iri=ACCOUNT, scope=_ground_truth(),
        observed_at=datetime(2026, 9, 5),  # no tzinfo
    )
    assert _current_value(book) == "100"
    assert book.frontmatter["asOf"]["observedAt"].endswith("+00:00")


# --- what an as-of read reports about itself ------------------------------


async def test_asof_scope_names_the_graphs_actually_read(world):
    scope = _persona_scope()
    book = await get_holon(
        world, CONN, holon_iri=ACCOUNT, scope=scope,
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    fm = book.frontmatter
    assert fm["scope"] == [HOLONS, EVENTS]
    assert fm["neighbourScope"] == [g.iri for g in scope]
    assert "ground truth only" in fm["asOf"]["note"]
    assert "insertedAt" in fm["asOf"]["note"]


# --- transaction time ----------------------------------------------------------
#
# event:1 is made a late entry: true from 2026-09-01 but only recorded on the
# 20th. It is the only assertion visible at the observed time, so rdflib's
# nested-FILTER limit (module docstring) does not come into it.


@pytest.fixture
def late_world(world) -> ClosedWorld:
    world.ds.get_context(URIRef(EVENTS)).parse(
        format="turtle",
        data="""@prefix hevt: <https://w3id.org/holon/event/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<urn:example:event:1> hevt:insertedAt "2026-09-20T00:00:00+00:00"^^xsd:dateTime .""",
    )
    return world


async def test_without_inserted_at_a_late_entry_still_shows(late_world):
    """An ordinary valid-time read must not lose entries written since."""
    book = await get_holon(
        late_world, CONN, holon_iri=ACCOUNT, scope=_ground_truth(),
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert _current_value(book) == "100"
    assert book.frontmatter["asOf"]["insertedAt"] is None


async def test_inserted_at_before_the_entry_was_recorded_does_not_see_it(late_world):
    book = await get_holon(
        late_world, CONN, holon_iri=ACCOUNT, scope=_ground_truth(),
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        inserted_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    assert _current_value(book) is None


async def test_inserted_at_after_the_entry_was_recorded_sees_it(late_world):
    book = await get_holon(
        late_world, CONN, holon_iri=ACCOUNT, scope=_ground_truth(),
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        inserted_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )
    assert _current_value(book) == "100"


async def test_an_entry_with_no_insertion_time_counts_as_known(world):
    """Ledgers written before insertedAt was stamped must keep reading."""
    book = await get_holon(
        world, CONN, holon_iri=ACCOUNT, scope=_ground_truth(),
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        inserted_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    assert _current_value(book) == "100"


async def test_current_state_scope_is_unchanged(world):
    scope = _persona_scope()
    book = await get_holon(world, CONN, holon_iri=ACCOUNT, scope=scope)
    fm = book.frontmatter
    assert fm["scope"] == [g.iri for g in scope]
    assert "neighbourScope" not in fm
    assert "asOf" not in fm


# --- what an as-of read refuses -------------------------------------------


async def test_list_fluent_holon_is_refused_before_any_state_query(world):
    with pytest.raises(AsOfUnsupportedError) as excinfo:
        await get_holon(
            world, CONN, holon_iri=CLUB, scope=_ground_truth(),
            observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        )
    assert excinfo.value.fluents == [MEMBERS]
    assert MEMBERS in str(excinfo.value)
    assert world.constructs == []  # refused early, no ledger walk


async def test_inserted_at_alone_also_triggers_the_guard(world):
    with pytest.raises(AsOfUnsupportedError):
        await get_holon(
            world, CONN, holon_iri=CLUB, scope=_ground_truth(),
            inserted_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        )


async def test_list_fluent_holon_still_reads_current_state(world):
    """The guard is about reconstructing the past, not about list fluents."""
    book = await get_holon(world, CONN, holon_iri=CLUB, scope=_ground_truth())
    assert "asOf" not in book.frontmatter


async def test_guard_is_per_holon_not_per_dataset(world):
    """A list-mode property exists in the ontology, but this holon has none."""
    book = await get_holon(
        world, CONN, holon_iri=ACCOUNT, scope=_ground_truth(),
        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    assert _current_value(book) == "100"


# --- REST route -------------------------------------------------------------


@pytest.fixture
def http(world, tmp_path):
    app = FastAPI()
    app.include_router(holon_routes.router)

    async def _animus() -> Animus:
        return Animus("x", "GitHubIdentity", person="urn:example:person:kurt",
                      real_person="urn:example:person:kurt")

    app.dependency_overrides[require_conn] = lambda: CONN
    app.dependency_overrides[get_client] = lambda: world
    app.dependency_overrides[require_animus] = _animus
    app.dependency_overrides[get_personas] = lambda: PersonaStore(tmp_path / "personas.json")
    return TestClient(app)


def test_route_maps_list_fluent_refusal_to_400(http):
    resp = http.get("/holon", params={"iri": CLUB, "observed_at": "2026-09-05T00:00:00Z"})
    assert resp.status_code == 400
    assert "list-mode" in resp.json()["detail"]
    assert MEMBERS in resp.json()["detail"]


def test_route_accepts_a_naive_observed_at(http):
    resp = http.get("/holon", params={"iri": ACCOUNT, "observed_at": "2026-09-05T00:00:00"})
    assert resp.status_code == 200
    assert re.search(r"currentValue\s+100\b", resp.text)


# --- MCP tool ---------------------------------------------------------------


@pytest.fixture
def mcp_call(monkeypatch):
    from holonbridge_mcp.tools import core

    calls: list[dict] = []

    async def fake_call(method, path, *, params=None, text=False, **kw):
        calls.append({"method": method, "path": path, "params": params, "text": text})
        return "ok"

    monkeypatch.setattr(core, "_call", fake_call)
    return core, calls


async def test_mcp_get_holon_forwards_observed_at(mcp_call):
    core, calls = mcp_call
    await core.get_holon("urn:example:account", observed_at="2026-09-05T00:00:00Z")
    assert calls[0]["path"] == "/holon"
    assert calls[0]["params"]["observed_at"] == "2026-09-05T00:00:00Z"


async def test_mcp_get_holon_omits_observed_at_by_default(mcp_call):
    core, calls = mcp_call
    await core.get_holon("urn:example:account")
    assert "observed_at" not in calls[0]["params"]


async def test_mcp_get_holon_forwards_inserted_at(mcp_call):
    core, calls = mcp_call
    await core.get_holon(
        "urn:example:account", observed_at="2026-09-05T00:00:00Z", inserted_at="2026-09-06T00:00:00Z"
    )
    assert calls[0]["params"]["inserted_at"] == "2026-09-06T00:00:00Z"


async def test_mcp_get_holon_omits_inserted_at_by_default(mcp_call):
    core, calls = mcp_call
    await core.get_holon("urn:example:account", observed_at="2026-09-05T00:00:00Z")
    assert "inserted_at" not in calls[0]["params"]


async def test_mcp_update_fluent_forwards_asserted_datetime(monkeypatch):
    from holonbridge_mcp.tools import fluents

    seen: list[dict] = []

    async def fake_call(method, path, *, json_body=None, **kw):
        seen.append({"path": path, "body": json_body})
        return {}

    monkeypatch.setattr(fluents, "_call", fake_call)
    await fluents.update_fluent("urn:f", "Set", 1, asserted_datetime="2026-11-12T22:41:00Z")
    assert seen[0]["path"] == "/fluent/update"
    assert seen[0]["body"]["asserted_datetime"] == "2026-11-12T22:41:00Z"
