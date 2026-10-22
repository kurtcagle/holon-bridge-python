"""Tests for context-aware SHACL validation (2026-09-30).

Jena's SHACL service validates one flat graph. A shape that needs the
ontology visible -- a SPARQL constraint over a property *classified* in the
ontology graph, or a ``sh:class`` check against a class declared there -- is
vacuous or wrong when the data graph is validated on its own. The mechanism
under test: ``context_graphs`` are ``ADD``-ed to the scratch copy before
validating, and a dataset's second shapes graph (``urn:{dataset}:shacl-context``)
is run that way alongside the ordinary one.

What these tests pin down, because each is a way this could quietly go wrong:

* with no context nothing changes -- no ``ADD``, baseline still validated in
  place, byte-for-byte the old behaviour;
* context is added AFTER a reduction rule runs (the rule replaces the scratch
  graph's content, which would discard or mis-reduce anything added earlier);
* in delta mode the baseline is built with the context too -- otherwise every
  violation the context itself carries would be blamed on the payload;
* a context graph IRI is checked before any Fuseki call (it is interpolated
  into SPARQL);
* a dataset with no context shapes is validated exactly as before, and a
  caller-named shapes graph is never joined by the dataset's context pack;
* either pack can refuse a write, the refusal says which, and a refused write
  never reaches the real graph.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from holonbridge import ingest
from holonbridge import shacl as shacl_mod
from holonbridge.conn import GRAPH_ROLES, Conn

PAYLOAD = "<urn:a> <urn:b> <urn:c> ."

LOCAL_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:TemperatureSensorShape a sh:NodeShape ;
    sh:targetClass ex:TemperatureSensor ;
    sh:property [ sh:path ex:temperature ; sh:minCount 1 ; sh:datatype xsd:decimal ] .
"""

CONTEXT_SHAPES = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <https://example.org/> .

ex:PositiveBalanceShape a sh:NodeShape ;
    sh:targetClass ex:Account ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "balance must be non-negative" ;
        sh:select \"\"\"SELECT $this WHERE { $this ex:balance ?b . FILTER (?b < 0) }\"\"\" ] .
"""

CONFORMS = (
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n[] a sh:ValidationReport ; sh:conforms true .\n"
)


def violation(focus: str = "urn:x:a", message: str = "bad") -> str:
    return f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
[] a sh:ValidationReport ; sh:conforms false ;
   sh:result [ a sh:ValidationResult ; sh:focusNode <{focus}> ;
               sh:resultSeverity sh:Violation ; sh:resultMessage "{message}" ;
               sh:sourceShape <urn:x:shape> ] .
"""


def _conn(dataset: str = "ds") -> Conn:
    return Conn(base_url="http://fuseki.test", dataset=dataset, overridden=False, bank_name="local")


class FakeClient:
    """An in-memory stand-in that keeps an ordered log of everything done to
    it and tracks which graphs hold content, so ASK and COPY/ADD behave."""

    def __init__(self, *, shapes: dict[str, str], populated=(), validator=None) -> None:
        self.shapes = shapes
        self.populated = set(populated)
        self.validator = validator or (lambda target, shapes_turtle: CONFORMS)
        self.events: list[tuple[str, str]] = []
        self.validated: list[tuple[str, str]] = []

    # -- Fuseki surface used by shacl.py / ingest.py --
    async def get_graph(self, conn, graph_iri):
        return self.shapes.get(graph_iri, "")

    async def update(self, conn, sparql):
        self.events.append(("update", sparql))
        m = re.match(r"(?:COPY|ADD) SILENT GRAPH <([^>]+)> TO <([^>]+)>", sparql)
        if m and m.group(1) in self.populated:
            self.populated.add(m.group(2))

    async def drop_graph(self, conn, graph_iri):
        self.events.append(("drop", graph_iri))
        self.populated.discard(graph_iri)

    async def post_graph(self, conn, graph_iri, turtle):
        self.events.append(("post", graph_iri))
        self.populated.add(graph_iri)

    async def put_graph(self, conn, graph_iri, turtle):
        self.events.append(("put", graph_iri))
        self.populated.add(graph_iri)

    async def select(self, conn, query):
        m = re.search(r"GRAPH <([^>]+)>", query)
        return {"boolean": bool(m) and m.group(1) in self.populated}

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        self.validated.append((target_graph, shapes_turtle))
        return self.validator(target_graph, shapes_turtle)

    # -- helpers for assertions --
    def adds(self) -> list[tuple[str, str]]:
        """(context graph, scratch graph) for every ADD issued."""
        out = []
        for kind, text in self.events:
            m = re.match(r"ADD SILENT GRAPH <([^>]+)> TO <([^>]+)>", text) if kind == "update" else None
            if m:
                out.append((m.group(1), m.group(2)))
        return out


def _client(conn: Conn, *, with_context_pack: bool = False, validator=None, **extra) -> FakeClient:
    shapes = {conn.shapes_graph: LOCAL_SHAPES}
    populated = {conn.holons_graph, conn.ontology_graph, conn.shapes_graph}
    if with_context_pack:
        shapes[conn.context_shapes_graph] = CONTEXT_SHAPES
        populated.add(conn.context_shapes_graph)
    return FakeClient(shapes=shapes, populated=populated, validator=validator, **extra)


# --- Conn -----------------------------------------------------------------------


def test_context_shapes_graph_follows_the_naming_convention():
    assert _conn().context_shapes_graph == "urn:ds:shacl-context"


def test_context_graph_names_follow_bank_scoping():
    conn = Conn(
        base_url="http://x",
        dataset="worldtest",
        overridden=False,
        bank_name="holongraph",
        bank_scoped_datasets=frozenset({"worldtest"}),
    )
    assert conn.context_shapes_graph == "urn:holongraph:worldtest:shacl-context"
    assert conn.taxonomy_graph == "urn:holongraph:worldtest:taxonomy"
    assert conn.validation_context_graphs == (
        "urn:holongraph:worldtest:ontology",
        "urn:holongraph:worldtest:taxonomy",
    )


def test_shacl_context_is_a_declared_role_and_is_described():
    assert "shacl-context" in GRAPH_ROLES
    assert _conn().describe()["graphs"]["shacl-context"] == "urn:ds:shacl-context"


def test_taxonomy_is_a_declared_role_and_is_described():
    assert "taxonomy" in GRAPH_ROLES
    assert _conn().taxonomy_graph == "urn:ds:taxonomy"
    assert _conn().describe()["graphs"]["taxonomy"] == "urn:ds:taxonomy"


def test_the_validation_context_is_the_ontology_and_the_taxonomy_and_nothing_else():
    conn = _conn()
    assert conn.validation_context_graphs == (conn.ontology_graph, conn.taxonomy_graph)
    # the context pack and every other graph stay out of the context
    assert conn.shapes_graph not in conn.validation_context_graphs
    assert conn.context_shapes_graph not in conn.validation_context_graphs
    assert conn.holons_graph not in conn.validation_context_graphs


# --- validate_full ----------------------------------------------------------------


async def test_full_without_context_adds_nothing():
    conn = _conn()
    client = _client(conn)
    await shacl_mod.validate_full(
        client, conn, turtle=PAYLOAD, shapes_graph=conn.shapes_graph, target_graph=conn.holons_graph
    )
    assert client.adds() == []


async def test_full_with_context_adds_each_graph_to_the_scratch_after_the_payload():
    conn = _conn()
    client = _client(conn)
    await shacl_mod.validate_full(
        client,
        conn,
        turtle=PAYLOAD,
        shapes_graph=conn.shapes_graph,
        target_graph=conn.holons_graph,
        context_graphs=(conn.ontology_graph, "urn:ds:extra"),
    )
    scratch = client.validated[0][0]
    assert scratch not in (conn.holons_graph, conn.ontology_graph)  # validated a copy
    assert client.adds() == [(conn.ontology_graph, scratch), ("urn:ds:extra", scratch)]
    # order: target copied in, payload posted, then context added
    order = [kind + ":" + (text if kind == "post" else text.split()[0]) for kind, text in client.events]
    assert order.index("update:COPY") < order.index(f"post:{scratch}") < order.index("update:ADD")
    # a context graph is read, never written or dropped
    assert ("drop", conn.ontology_graph) not in client.events
    assert ("post", conn.ontology_graph) not in client.events


async def test_context_is_added_after_a_reduction_rule_has_run(monkeypatch):
    """A reduction rule replaces the scratch graph's content with its
    CONSTRUCT result. Anything added before it would be discarded, or reduced
    as if it were candidate state."""
    conn = _conn()
    client = _client(conn)

    async def fake_reduce(client_, conn_, rule_id, scratch):
        client_.events.append(("reduce", scratch))

    monkeypatch.setattr(shacl_mod, "_apply_reduction", fake_reduce)
    await shacl_mod.validate_full(
        client,
        conn,
        turtle=PAYLOAD,
        shapes_graph=conn.shapes_graph,
        target_graph=conn.holons_graph,
        reduction_rule_id="some-rule",
        context_graphs=(conn.ontology_graph,),
    )
    kinds = [k for k, t in client.events if k == "reduce" or (k == "update" and t.startswith("ADD"))]
    assert kinds == ["reduce", "update"]


# --- validate_delta ---------------------------------------------------------------


async def test_delta_without_context_still_validates_the_baseline_in_place():
    conn = _conn()
    client = _client(conn)
    await shacl_mod.validate_delta(
        client, conn, turtle=PAYLOAD, shapes_graph=conn.shapes_graph, target_graph=conn.holons_graph
    )
    assert client.validated[0][0] == conn.holons_graph  # baseline = the live target
    assert len(client.validated) == 2
    assert client.adds() == []


async def test_delta_with_context_builds_the_baseline_in_its_own_scratch_graph():
    conn = _conn()
    client = _client(conn)
    await shacl_mod.validate_delta(
        client,
        conn,
        turtle=PAYLOAD,
        shapes_graph=conn.shapes_graph,
        target_graph=conn.holons_graph,
        context_graphs=(conn.ontology_graph,),
    )
    targets = [t for t, _ in client.validated]
    assert conn.holons_graph not in targets  # the live target is never validated bare
    assert len(set(targets)) == 2  # baseline and merged are different scratch graphs
    assert sorted(s for _, s in client.adds()) == sorted(targets)  # context went into BOTH
    dropped = {g for k, g in client.events if k == "drop"}
    assert set(targets) <= dropped  # and both were cleaned up


async def test_a_violation_carried_by_the_context_is_not_blamed_on_the_payload():
    """The reason the baseline needs the context too: the same violation is
    present before and after, so nothing is introduced."""
    conn = _conn()
    client = _client(conn, validator=lambda t, s: violation("urn:ontology:node"))
    report = await shacl_mod.validate_delta(
        client,
        conn,
        turtle=PAYLOAD,
        shapes_graph=conn.shapes_graph,
        target_graph=conn.holons_graph,
        context_graphs=(conn.ontology_graph,),
    )
    assert report.conforms is True
    assert report.results == []


async def test_a_violation_the_payload_introduces_is_reported_with_context():
    conn = _conn()
    reports = iter([CONFORMS, violation("urn:new:node", "introduced")])
    client = _client(conn, validator=lambda t, s: next(reports))  # baseline first, then merged
    report = await shacl_mod.validate_delta(
        client,
        conn,
        turtle=PAYLOAD,
        shapes_graph=conn.shapes_graph,
        target_graph=conn.holons_graph,
        context_graphs=(conn.ontology_graph,),
    )
    assert report.conforms is False
    assert [r["focusNode"] for r in report.results] == ["urn:new:node"]


async def test_delta_with_context_survives_a_target_that_does_not_exist_yet():
    conn = _conn()
    client = _client(conn)
    client.populated.discard(conn.holons_graph)
    report = await shacl_mod.validate_delta(
        client,
        conn,
        turtle=PAYLOAD,
        shapes_graph=conn.shapes_graph,
        target_graph=conn.holons_graph,
        context_graphs=(conn.ontology_graph,),
    )
    assert report.conforms is True


# --- IRI safety -------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["urn:x> } ; DROP ALL ; #", "urn:a b", 'urn:a"b', "", "urn:x\n", "urn:x{y}", "urn:x\\y"],
)
async def test_an_unsafe_context_graph_is_refused_before_any_fuseki_update(bad):
    conn = _conn()
    client = _client(conn)
    for fn in (shacl_mod.validate_full, shacl_mod.validate_delta):
        with pytest.raises(ValueError, match="cannot be used as a validation context graph"):
            await fn(
                client,
                conn,
                turtle=PAYLOAD,
                shapes_graph=conn.shapes_graph,
                target_graph=conn.holons_graph,
                context_graphs=(bad,),
            )
    assert client.events == []


async def test_a_bare_string_for_context_graphs_is_refused_not_iterated_by_character():
    conn = _conn()
    client = _client(conn)
    with pytest.raises(TypeError):
        await shacl_mod.validate_full(
            client,
            conn,
            turtle=PAYLOAD,
            shapes_graph=conn.shapes_graph,
            target_graph=conn.holons_graph,
            context_graphs="urn:ds:ontology",  # type: ignore[arg-type]
        )
    assert client.events == []


# --- validate_context_pack --------------------------------------------------------


async def test_the_context_pack_is_none_when_the_dataset_has_none():
    conn = _conn()
    client = _client(conn)  # no context shapes graph
    result = await shacl_mod.validate_context_pack(
        client, conn, turtle=PAYLOAD, target_graph=conn.holons_graph, delta=True
    )
    assert result is None
    assert client.validated == []


async def test_the_context_pack_uses_the_context_shapes_with_the_ontology_merged():
    conn = _conn()
    client = _client(conn, with_context_pack=True)
    result = await shacl_mod.validate_context_pack(
        client, conn, turtle=PAYLOAD, target_graph=conn.holons_graph, delta=False
    )
    assert result is not None and result.conforms
    assert "PositiveBalanceShape" in client.validated[0][1]  # the context shapes, not the ordinary ones
    assert [g for g, _ in client.adds()] == [conn.ontology_graph, conn.taxonomy_graph]


async def test_a_dataset_with_no_taxonomy_graph_is_validated_as_before():
    """The taxonomy is optional: ``ADD SILENT`` of an absent graph adds
    nothing, so a dataset that never created one sees only its ontology."""
    conn = _conn()
    client = _client(conn)  # populated: holons, ontology, shacl -- no taxonomy
    assert conn.taxonomy_graph not in client.populated
    await shacl_mod.validate_full(
        client,
        conn,
        turtle=PAYLOAD,
        shapes_graph=conn.shapes_graph,
        target_graph=conn.holons_graph,
        context_graphs=conn.validation_context_graphs,
    )
    scratch = client.validated[0][0]
    assert client.adds() == [(conn.ontology_graph, scratch), (conn.taxonomy_graph, scratch)]
    # the absent taxonomy contributed nothing, and was neither created nor written
    assert conn.taxonomy_graph not in client.populated
    assert ("post", conn.taxonomy_graph) not in client.events
    assert ("put", conn.taxonomy_graph) not in client.events


async def test_the_taxonomy_is_read_as_context_and_never_written_or_dropped():
    conn = _conn()
    client = _client(conn)
    client.populated.add(conn.taxonomy_graph)
    await shacl_mod.validate_delta(
        client,
        conn,
        turtle=PAYLOAD,
        shapes_graph=conn.shapes_graph,
        target_graph=conn.holons_graph,
        context_graphs=conn.validation_context_graphs,
    )
    # added to both the baseline and the merged scratch graphs...
    taxonomy_adds = [scratch for graph, scratch in client.adds() if graph == conn.taxonomy_graph]
    assert sorted(taxonomy_adds) == sorted(t for t, _ in client.validated)
    # ...and left exactly as it was
    assert conn.taxonomy_graph in client.populated
    assert ("drop", conn.taxonomy_graph) not in client.events
    assert ("post", conn.taxonomy_graph) not in client.events


async def test_delta_validation_of_the_context_pack_requires_a_target():
    conn = _conn()
    client = _client(conn, with_context_pack=True)
    with pytest.raises(ValueError, match="requires a target_graph"):
        await shacl_mod.validate_context_pack(
            client, conn, turtle=PAYLOAD, target_graph=None, delta=True
        )


# --- combine_reports --------------------------------------------------------------


def test_combining_with_no_context_report_is_the_ordinary_report_unchanged():
    local = shacl_mod.ValidationReport(conforms=True, turtle=CONFORMS)
    assert shacl_mod.combine_reports(local, None) == local.as_dict()


def test_combining_counts_and_tags_both_packs_and_keeps_their_turtle_apart():
    local = shacl_mod.parse_report(violation("urn:l"), mode="delta")
    context = shacl_mod.parse_report(violation("urn:c"), mode="delta")
    combined = shacl_mod.combine_reports(
        local, context, context_shapes_graph="urn:ds:shacl-context", context_graphs=("urn:ds:ontology",)
    )
    assert combined["conforms"] is False
    assert combined["violations"] == 2
    assert [(r["pack"], r["focusNode"]) for r in combined["results"]] == [
        ("local", "urn:l"),
        ("context", "urn:c"),
    ]
    assert combined["report"] == local.turtle  # not merged: blank nodes would collide
    assert combined["packs"]["context"]["report"] == context.turtle
    assert combined["packs"]["context"]["context_graphs"] == ["urn:ds:ontology"]


def test_a_context_warning_is_reported_but_does_not_fail_the_combined_report():
    local = shacl_mod.ValidationReport(conforms=True, turtle=CONFORMS)
    warning = {
        "focusNode": "urn:c",
        "resultSeverity": "http://www.w3.org/ns/shacl#Warning",
        "resultMessage": "note",
    }
    context = shacl_mod.ValidationReport(conforms=True, results=[warning], turtle=CONFORMS)
    combined = shacl_mod.combine_reports(local, context)
    assert combined["conforms"] is True
    assert combined["violations"] == 0
    assert combined["warnings"] == 1


# --- the write path ---------------------------------------------------------------

ANIMUS = SimpleNamespace(person="urn:ds:person:test")


@pytest.fixture
def allow_writes(monkeypatch):
    async def allowed(*_a, **_k):
        return SimpleNamespace(allowed=True, matched_grant=None, reason="test")

    monkeypatch.setattr(ingest, "check_write", allowed)
    monkeypatch.setattr(ingest, "check_replace", allowed)


async def _write(client, conn, **overrides):
    args = dict(
        turtle=PAYLOAD,
        graph_iri=conn.holons_graph,
        mode="merge",
        shapes_graph=None,
        reduction_rule_id=None,
        conn=conn,
        client=client,
        shacl_required=True,
        shacl_delta=True,
        animus=ANIMUS,
    )
    args.update(overrides)
    return await ingest.write_turtle_to_graph(**args)


def _introduces_violation(marker: str, focus: str, message: str):
    """A validator that passes the baseline and fails the merged state, for
    the shapes whose text contains ``marker`` -- i.e. a violation the payload
    itself introduces. Delta mode calls baseline first, then merged."""
    calls = {"n": 0}

    def validator(target, shapes_turtle):
        if marker not in shapes_turtle:
            return CONFORMS
        calls["n"] += 1
        return violation(focus, message) if calls["n"] == 2 else CONFORMS

    return validator


def _always_violates(marker: str):
    """A violation that is there before the payload and after it."""
    return lambda target, shapes_turtle: violation("urn:old") if marker in shapes_turtle else CONFORMS


_INTRODUCES_CONTEXT = _introduces_violation("PositiveBalanceShape", "urn:acct", "balance must be non-negative")
_INTRODUCES_LOCAL = _introduces_violation("TemperatureSensorShape", "urn:sensor", "no temperature")


async def test_a_dataset_with_no_context_pack_is_written_exactly_as_before(allow_writes):
    conn = _conn()
    client = _client(conn)
    result = await _write(client, conn)
    assert ("post", conn.holons_graph) in client.events
    assert result["validated"] is True
    assert "packs" not in result["validation"]  # the response shape is unchanged


async def test_a_conforming_context_pack_lets_the_write_through_and_is_reported(allow_writes):
    conn = _conn()
    client = _client(conn, with_context_pack=True)
    result = await _write(client, conn)
    assert ("post", conn.holons_graph) in client.events
    assert result["validation"]["packs"]["context"]["conforms"] is True
    assert result["validation"]["packs"]["context"]["context_graphs"] == [
        conn.ontology_graph,
        conn.taxonomy_graph,
    ]


async def test_a_context_pack_violation_refuses_the_write_and_nothing_reaches_the_real_graph(allow_writes):
    conn = _conn()
    client = _client(conn, with_context_pack=True, validator=_INTRODUCES_CONTEXT)
    with pytest.raises(HTTPException) as err:
        await _write(client, conn)
    assert err.value.status_code == 422
    detail = err.value.detail
    assert detail["error"] == "shacl_violation"
    assert detail["conforms"] is False
    assert detail["violations"] == 1
    assert [r["pack"] for r in detail["results"]] == ["context"]  # says which pack refused it
    assert ("post", conn.holons_graph) not in client.events
    assert ("put", conn.holons_graph) not in client.events


async def test_an_ordinary_shapes_violation_still_refuses_the_write(allow_writes):
    conn = _conn()
    client = _client(conn, with_context_pack=True, validator=_INTRODUCES_LOCAL)
    with pytest.raises(HTTPException) as err:
        await _write(client, conn)
    assert err.value.status_code == 422
    assert [r["pack"] for r in err.value.detail["results"]] == ["local"]
    assert ("post", conn.holons_graph) not in client.events


async def test_a_caller_named_shapes_graph_is_not_joined_by_the_context_pack(allow_writes):
    """An explicit shapes_graph means validate against exactly this. The
    dataset's context pack would fail this write, and must not be consulted."""
    conn = _conn()
    client = _client(conn, with_context_pack=True, validator=_INTRODUCES_CONTEXT)
    result = await _write(client, conn, shapes_graph=conn.shapes_graph)
    assert ("post", conn.holons_graph) in client.events
    assert "packs" not in result["validation"]
    assert all("PositiveBalanceShape" not in shapes for _, shapes in client.validated)


async def test_with_shacl_off_neither_pack_is_consulted(allow_writes):
    conn = _conn()
    client = _client(conn, with_context_pack=True, validator=_INTRODUCES_CONTEXT)
    result = await _write(client, conn, shacl_required=False)
    assert result["validated"] is False
    assert client.validated == []
    assert ("post", conn.holons_graph) in client.events


async def test_a_context_violation_that_was_already_there_does_not_block_an_unrelated_write(allow_writes):
    """Delta mode's whole point, and it must hold for the context pack too:
    one pre-existing violation must not reject every later write."""
    conn = _conn()
    client = _client(conn, with_context_pack=True, validator=_always_violates("PositiveBalanceShape"))
    result = await _write(client, conn)
    assert ("post", conn.holons_graph) in client.events
    assert result["validation"]["packs"]["context"]["violations"] == 0


async def test_full_mode_reports_a_context_violation_whether_or_not_the_payload_caused_it(allow_writes):
    conn = _conn()
    client = _client(conn, with_context_pack=True, validator=_always_violates("PositiveBalanceShape"))
    with pytest.raises(HTTPException) as err:
        await _write(client, conn, shacl_delta=False)
    assert err.value.status_code == 422
    assert [r["pack"] for r in err.value.detail["results"]] == ["context"]
    assert ("post", conn.holons_graph) not in client.events
