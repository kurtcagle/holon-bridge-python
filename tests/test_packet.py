"""Packet receipts: what ``/ingest`` leaves behind for a DataBook.

Unit tests cover the header reading, the header lift and the envelope Turtle.
Route tests run ``/ingest`` against the same stub ``test_pipeline.py`` uses and
look at what was written where, and in which order: the payload first, then the
sender-authored header graph (if any), then the receipt, then the post-write
hook.

The two properties most worth a test of their own are the ones a future edit
could lose without anything else failing: a header must never be able to forge
a receipt, and the ``inbox`` graph must not be a system graph (or the post-write
hook would skip it and no trigger would ever see a packet).
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient
from rdflib import Graph, Literal, URIRef

from holonbridge import packet as packet_mod
from holonbridge import trigger_hook
from holonbridge.config import Settings
from holonbridge.conn import GRAPH_ROLES, Conn
from holonbridge.fuseki import FusekiError
from holonbridge.packet import (
    DUPLICATE,
    PACKET_CLASS,
    RECEIVED,
    Claims,
    build_envelope,
    lift_header,
    read_claims,
)
from holonbridge.server import create_app

from ._identity import PERSON_IRI, TOKEN, auth
from .test_pipeline import PipelineStub

HB = "https://w3id.org/holonbridge/"
INBOX = "urn:ds:inbox"
PROFILE = "https://example.org/profiles/sensor-report"

DATABOOK_WITH_PROFILE = f"""---
id: https://example.org/databooks/sensor-report-1
type: databook
profiles:
  - {PROFILE}
graph:
  named_graph: urn:ds:sensor-grid
---

```turtle
<urn:a> <urn:b> <urn:c> .
```
"""

DATABOOK_NO_PROFILE = """---
graph:
  named_graph: urn:ds:sensor-grid
---

```turtle
<urn:a> <urn:b> <urn:c> .
```
"""

DATABOOK_WITH_CONTEXT = """---
"@context":
  ex: https://example.org/ns#
  id: "@id"
  title: ex:title
id: https://example.org/databooks/with-context
title: A header with a context
graph:
  named_graph: urn:ds:sensor-grid
---

```turtle
<urn:a> <urn:b> <urn:c> .
```
"""

# A sender trying to plant a receipt of its own through the header lift.
DATABOOK_FORGING_A_RECEIPT = f"""---
"@context":
  hb: {HB}
  id: "@id"
  type: "@type"
id: urn:ds:inbox:forged
type: hb:Packet
hb:packetStatus: Received
hb:declaresProfile:
  "@id": {PROFILE}
graph:
  named_graph: urn:ds:sensor-grid
---

```turtle
<urn:a> <urn:b> <urn:c> .
```
"""


def conn() -> Conn:
    return Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")


def triples(turtle: str) -> Graph:
    g = Graph()
    g.parse(data=turtle, format="turtle")
    return g


@pytest.fixture
def stub() -> PipelineStub:
    return PipelineStub()


@pytest.fixture
def client(stub: PipelineStub):
    app = create_app(Settings(bearer_token=TOKEN, named_query_ttl=0.0))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        yield test_client


@pytest.fixture
def hook_calls(monkeypatch):
    """Record calls to the post-write hook instead of evaluating triggers."""
    calls: list[dict] = []

    async def fake_after_write(client, conn, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(packet_mod, "after_write", fake_after_write)
    return calls


def ingest(client, databook: str, **extra):
    return client.post("/ingest", json={"databook": databook, **extra}, headers=auth())


# --- graph role ---------------------------------------------------------------


def test_inbox_is_a_graph_role():
    assert "inbox" in GRAPH_ROLES
    assert conn().graph("inbox") == INBOX
    assert conn().scoped("inbox", "abc") == "urn:ds:inbox:abc"


def test_inbox_is_not_a_system_graph():
    """If it were, the post-write hook would skip every write to it and no
    trigger could ever see a packet."""
    assert not trigger_hook.only_system_graphs(conn(), [conn().graph("inbox")])


# --- reading claims -----------------------------------------------------------


def test_claims_from_a_plain_header():
    claims = read_claims(
        {"id": "https://example.org/d1", "type": "databook", "profiles": [PROFILE]}
    )
    assert claims.document_id == "https://example.org/d1"
    assert claims.document_type == "databook"
    assert claims.profiles == (PROFILE,)
    assert claims.notes == ()


def test_profile_entries_may_be_mappings():
    claims = read_claims({"profiles": [{"id": PROFILE}, {"iri": "https://example.org/p2"}]})
    assert claims.profiles == (PROFILE, "https://example.org/p2")


def test_a_single_profile_string_is_accepted():
    assert read_claims({"profiles": PROFILE}).profiles == (PROFILE,)


def test_a_profile_that_is_not_an_iri_is_dropped_and_noted():
    claims = read_claims({"profiles": ["not an iri", PROFILE, "has>bracket:x"]})
    assert claims.profiles == (PROFILE,)
    assert len(claims.notes) == 2


def test_duplicate_profiles_collapse():
    assert read_claims({"profiles": [PROFILE, PROFILE]}).profiles == (PROFILE,)


def test_a_header_with_no_claims_has_none():
    claims = read_claims({})
    assert (claims.document_id, claims.document_type, claims.profiles) == (None, None, ())


# --- lifting the header -------------------------------------------------------


def test_no_context_means_nothing_to_lift():
    assert lift_header({"id": "x"}) == (None, "none")


def test_a_header_with_an_inline_context_lifts_to_rdf():
    turtle, outcome = lift_header(
        {
            "@context": {"ex": "https://example.org/ns#", "id": "@id", "title": "ex:title"},
            "id": "https://example.org/d1",
            "title": "Hello",
        }
    )
    assert outcome == "lifted"
    g = triples(turtle)
    assert (
        URIRef("https://example.org/d1"),
        URIRef("https://example.org/ns#title"),
        Literal("Hello"),
    ) in g


def test_a_remote_context_is_refused_not_fetched_and_does_not_raise():
    turtle, outcome = lift_header(
        {"@context": "https://example.org/context.jsonld", "id": "https://example.org/d1"}
    )
    assert turtle is None
    assert outcome.startswith("not lifted:")


def test_yaml_dates_in_a_header_do_not_break_the_lift():
    import datetime

    turtle, outcome = lift_header(
        {
            "@context": {"ex": "https://example.org/ns#", "id": "@id", "created": "ex:created"},
            "id": "https://example.org/d1",
            "created": datetime.date(2026, 11, 17),
        }
    )
    assert outcome == "lifted"
    assert "2026-11-17" in turtle


# --- the envelope -------------------------------------------------------------


def envelope(**overrides) -> str:
    kwargs = {
        "conn": conn(),
        "packet_id": "m1",
        "status": RECEIVED,
        "claims": Claims(
            document_id="https://example.org/d1", document_type="databook", profiles=(PROFILE,)
        ),
        "payload_graph": "urn:ds:sensor-grid",
        "payload_hash": "sha256:abc",
        "payload_bytes": 21,
        "received_from": PERSON_IRI,
        "message_id": "m1",
        "header_graph": None,
        "header_lift": "none",
        "received_at": "2026-11-17T22:00:00+00:00",
    }
    kwargs.update(overrides)
    return build_envelope(**kwargs)


def test_envelope_records_facts_and_claims_under_different_predicates():
    g = triples(envelope())
    node = URIRef("urn:ds:inbox:m1")
    assert (
        node,
        URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
        URIRef(PACKET_CLASS),
    ) in g
    assert (node, URIRef(f"{HB}packetStatus"), Literal(RECEIVED)) in g
    assert (node, URIRef(f"{HB}receivedFrom"), URIRef(PERSON_IRI)) in g
    assert (node, URIRef(f"{HB}payloadGraph"), URIRef("urn:ds:sensor-grid")) in g
    assert (node, URIRef(f"{HB}payloadHash"), Literal("sha256:abc")) in g
    assert (node, URIRef(f"{HB}ingestMessage"), URIRef("urn:ds:message:m1")) in g
    # What the document says about itself is a claim, and is named as one.
    assert (node, URIRef(f"{HB}declaresProfile"), URIRef(PROFILE)) in g
    assert (node, URIRef(f"{HB}declaredId"), Literal("https://example.org/d1")) in g
    assert (node, URIRef(f"{HB}declaredType"), Literal("databook")) in g


def test_envelope_without_claims_is_still_valid_turtle():
    g = triples(envelope(claims=Claims(), received_from=None))
    node = URIRef("urn:ds:inbox:m1")
    assert not list(g.objects(node, URIRef(f"{HB}declaresProfile")))
    assert not list(g.objects(node, URIRef(f"{HB}receivedFrom")))


def test_envelope_escapes_awkward_claim_text():
    g = triples(envelope(claims=Claims(document_id='has "quotes" and \\ slash\nand newline')))
    node = URIRef("urn:ds:inbox:m1")
    assert list(g.objects(node, URIRef(f"{HB}declaredId")))


def test_envelope_points_at_the_header_graph_when_there_is_one():
    g = triples(envelope(header_graph="urn:ds:inbox:m1:header", header_lift="lifted"))
    node = URIRef("urn:ds:inbox:m1")
    assert (node, URIRef(f"{HB}headerGraph"), URIRef("urn:ds:inbox:m1:header")) in g


# --- /ingest ------------------------------------------------------------------


def test_ingesting_a_databook_records_a_packet(client, stub, hook_calls):
    body = ingest(client, DATABOOK_WITH_PROFILE).json()

    assert body["status"] == "Completed"
    packet = body["packet"]
    assert packet["status"] == RECEIVED
    assert packet["declaresProfile"] == [PROFILE]
    assert packet["payloadGraph"] == "urn:ds:sensor-grid"
    assert packet["inboxGraph"] == INBOX

    graphs = [g for g, _ in stub.pushed]
    assert graphs == ["urn:ds:sensor-grid", INBOX]  # payload first, then the receipt

    inbox_turtle = dict(stub.pushed)[INBOX]
    g = triples(inbox_turtle)
    node = URIRef(packet["iri"])
    assert (node, URIRef(f"{HB}receivedFrom"), URIRef(PERSON_IRI)) in g
    assert (node, URIRef(f"{HB}declaresProfile"), URIRef(PROFILE)) in g

    stages = {s["name"]: s for s in body["stages"]}
    assert stages["packet"]["status"] == "Completed"


def test_the_post_write_hook_is_told_about_the_inbox(client, stub, hook_calls):
    ingest(client, DATABOOK_WITH_PROFILE)
    assert len(hook_calls) == 1
    assert hook_calls[0]["graphs"] == [INBOX]
    touched = hook_calls[0]["touched"]()
    assert f"{HB}packetStatus" in touched and f"{HB}declaresProfile" in touched


def test_a_databook_with_no_claims_still_gets_a_receipt(client, stub, hook_calls):
    packet = ingest(client, DATABOOK_NO_PROFILE).json()["packet"]
    assert packet["status"] == RECEIVED
    assert "declaresProfile" not in packet


def test_raw_turtle_has_no_header_and_so_no_packet(client, stub, hook_calls):
    body = client.post(
        "/ingest",
        json={"turtle": "<urn:a> <urn:b> <urn:c> .", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    ).json()
    assert "packet" not in body
    assert [g for g, _ in stub.pushed] == ["urn:ds:holons"]
    assert hook_calls == []


def test_a_graph_to_graph_ingest_has_no_packet(client, stub, hook_calls):
    stub.graphs["urn:ds:staging"] = "<urn:a> <urn:b> <urn:c> .\n"
    body = client.post(
        "/ingest",
        json={"source_graph": "urn:ds:staging", "graph_iri": "urn:ds:holons"},
        headers=auth(),
    ).json()
    assert "packet" not in body
    assert hook_calls == []


def test_a_refused_payload_leaves_no_receipt(client, stub, hook_calls):
    stub.write_grant = False
    response = ingest(client, DATABOOK_WITH_PROFILE)
    assert response.status_code == 403
    assert stub.pushed == []
    assert hook_calls == []


def test_a_fetched_databook_also_gets_a_receipt(client, stub, hook_calls, monkeypatch):
    from holonbridge.routes import pipeline as pipeline_routes

    async def fake_fetch(url, *, timeout=30.0):
        return DATABOOK_WITH_PROFILE, "text/markdown"

    monkeypatch.setattr(pipeline_routes, "fetch_source", fake_fetch)
    body = client.post(
        "/ingest", json={"source_url": "https://example.org/r.databook.md"}, headers=auth()
    ).json()
    assert body["packet"]["declaresProfile"] == [PROFILE]
    stages = {s["name"]: s for s in body["stages"]}
    assert stages["packet"]["order"] == stages["ingest"]["order"] + 1


def test_the_packet_id_is_the_ingest_message_id(client, stub, hook_calls):
    body = ingest(client, DATABOOK_WITH_PROFILE).json()
    assert body["packet"]["iri"] == f"urn:ds:inbox:{body['messageId']}"


def test_the_receipt_survives_a_pipeline_run(client, stub, hook_calls):
    body = ingest(client, DATABOOK_WITH_PROFILE, pipeline="build", wait=True).json()
    assert body["packet"]["status"] == RECEIVED
    assert body["stages"][0]["name"] == "ingest"


# --- duplicates ---------------------------------------------------------------


def test_the_same_document_and_payload_twice_is_a_duplicate(client, stub, hook_calls):
    stub.nonempty_graphs.add(INBOX)  # an earlier packet matches the duplicate check
    packet = ingest(client, DATABOOK_WITH_PROFILE).json()["packet"]
    assert packet["status"] == DUPLICATE
    inbox_turtle = dict(stub.pushed)[INBOX]
    assert f'"{DUPLICATE}"' in inbox_turtle
    assert hook_calls == []  # a duplicate is recorded, never dispatched


def test_a_document_without_an_id_is_never_a_duplicate(client, stub, hook_calls):
    stub.nonempty_graphs.add(INBOX)
    packet = ingest(client, DATABOOK_NO_PROFILE).json()["packet"]
    assert packet["status"] == RECEIVED
    assert len(hook_calls) == 1


# --- the header's own RDF -----------------------------------------------------


def test_a_header_context_is_lifted_into_its_own_graph(client, stub, hook_calls):
    packet = ingest(client, DATABOOK_WITH_CONTEXT).json()["packet"]
    assert packet["headerLift"] == "lifted"
    header_graph = packet["headerGraph"]
    assert header_graph.endswith(":header") and header_graph != INBOX

    pushed = dict(stub.pushed)
    assert "A header with a context" in pushed[header_graph]
    assert "A header with a context" not in pushed[INBOX]
    order = [g for g, _ in stub.pushed]
    assert order == ["urn:ds:sensor-grid", header_graph, INBOX]


def test_a_header_cannot_forge_a_receipt(client, stub, hook_calls):
    """The header names a node of type hb:Packet with status Received. Those
    triples are the sender's, so they must land in the per-packet header graph
    and never in the inbox graph a trigger's condition reads."""
    packet = ingest(client, DATABOOK_FORGING_A_RECEIPT).json()["packet"]
    pushed = dict(stub.pushed)

    inbox = triples(pushed[INBOX])
    forged = URIRef("urn:ds:inbox:forged")
    assert not list(inbox.triples((forged, None, None)))
    receipts = list(
        inbox.subjects(
            URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef(PACKET_CLASS)
        )
    )
    assert receipts == [URIRef(packet["iri"])]  # exactly one: the bridge's own

    lifted = triples(pushed[packet["headerGraph"]])
    assert list(lifted.triples((forged, None, None)))  # it is there, but quarantined


def test_a_header_that_will_not_lift_does_not_fail_the_ingest(client, stub, hook_calls):
    text = DATABOOK_WITH_PROFILE.replace(
        "type: databook", 'type: databook\n"@context": https://example.org/context.jsonld'
    )
    body = ingest(client, text).json()
    assert body["status"] == "Completed"
    assert body["packet"]["headerLift"].startswith("not lifted:")
    assert "headerGraph" not in body["packet"]
    assert [g for g, _ in stub.pushed] == ["urn:ds:sensor-grid", INBOX]


# --- failure ------------------------------------------------------------------


class InboxDownStub(PipelineStub):
    async def post_graph(self, conn, graph_iri, turtle):
        if graph_iri == INBOX:
            raise FusekiError(503, "inbox unavailable")
        await super().post_graph(conn, graph_iri, turtle)


def test_a_receipt_that_cannot_be_written_is_reported_as_such(hook_calls):
    stub = InboxDownStub()
    app = create_app(Settings(bearer_token=TOKEN, named_query_ttl=0.0))
    with TestClient(app) as client:
        app.state.fuseki = stub
        response = ingest(client, DATABOOK_WITH_PROFILE)
        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail["error"] == "packet_record_failed"
        assert "payload landed" in detail["message"]
        # The payload did land, and the message record says what happened.
        assert stub.pushed[0][0] == "urn:ds:sensor-grid"
        assert hook_calls == []


# --- the data model, against real SPARQL ---------------------------------------

#: The condition a profile's trigger registers. The same text appears in
#: docs/architecture/packet-ingestion.databook.md; if one changes, so must the other.
CONDITION = f"""PREFIX hb: <{HB}>
SELECT ?focus WHERE {{
  GRAPH <{INBOX}> {{
    ?focus a hb:Packet ;
           hb:packetStatus "Received" ;
           hb:declaresProfile <{PROFILE}> .
  }}
}}"""


def _real_store():
    from .test_agents import RdfStore  # the rdflib-backed store the agent tests use

    return RdfStore()


def _focus_nodes(store) -> set[str]:
    return {str(row.focus) for row in store.ds.query(CONDITION)}


def test_the_documented_condition_selects_exactly_the_received_packets():
    """Run through ``/ingest`` into a real (rdflib) dataset, not a canned stub:
    a packet of the profile is selected once, a repeat is not selected again,
    and a header that claims to be a receipt is not selected at all."""
    store = _real_store()
    app = create_app(Settings(bearer_token=TOKEN, named_query_ttl=0.0))
    with TestClient(app) as client:
        app.state.fuseki = store

        first = ingest(client, DATABOOK_WITH_PROFILE).json()
        assert first["packet"]["status"] == RECEIVED
        assert _focus_nodes(store) == {first["packet"]["iri"]}

        repeat = ingest(client, DATABOOK_WITH_PROFILE).json()
        assert repeat["packet"]["status"] == DUPLICATE
        assert _focus_nodes(store) == {first["packet"]["iri"]}  # not selected again

        # This header claims the profile and a Received receipt in its own RDF,
        # but its ``profiles`` key is empty. The bridge's receipt records only
        # the ``profiles`` claim, and the header's triples stay in their own
        # graph, so neither the forged node nor the real receipt is selected.
        forged = ingest(client, DATABOOK_FORGING_A_RECEIPT).json()
        assert forged["packet"]["status"] == RECEIVED
        assert _focus_nodes(store) == {first["packet"]["iri"]}
        assert "urn:ds:inbox:forged" not in _focus_nodes(store)


# --- the design DataBook stays true to the code ---------------------------------

DOC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "docs"
    / "architecture"
    / "packet-ingestion.databook.md"
)


def test_the_design_databook_quotes_the_condition_the_suite_runs():
    assert CONDITION in DOC.read_text()


def test_the_design_databooks_receipt_is_what_the_builder_produces():
    """The example is generated output, not prose. If the vocabulary changes and
    the document does not, this fails."""
    from rdflib.compare import isomorphic

    from holonbridge import audit
    from holonbridge.databook import DataBook

    book = DataBook.parse(DOC.read_text())
    example = book.block("example-receipt")
    assert example.lang == "turtle"

    payload = "<urn:example:r1> a <urn:example:Reading> ; <urn:example:value> 21.4 ."
    digest, size = audit.payload_digest(payload)
    expected = build_envelope(
        conn=Conn(base_url="http://x", dataset="example", overridden=False, bank_name="local"),
        packet_id="7c1e9a52d0b34f6e8a21b5c9e4d3f012",
        status=RECEIVED,
        claims=Claims(
            document_id="https://example.org/databooks/sensor-report-1",
            document_type="databook",
            profiles=(PROFILE,),
        ),
        payload_graph="urn:example:sensor-grid",
        payload_hash=digest,
        payload_bytes=size,
        received_from="urn:example:person:jane-doe",
        message_id="7c1e9a52d0b34f6e8a21b5c9e4d3f012",
        header_graph="urn:example:inbox:7c1e9a52d0b34f6e8a21b5c9e4d3f012:header",
        header_lift="lifted",
        received_at="2026-11-17T22:14:00+00:00",
    )
    shown = triples(example.body)
    assert isomorphic(shown, triples(expected))
    assert len(shown) == book.frontmatter["graph"]["triple_count"]
