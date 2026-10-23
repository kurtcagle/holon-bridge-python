"""Real-engine tests for the Fluree SHACL merge gate: the production code path
(``shacl.validate_full`` / ``validate_delta`` / ``validate_context_pack`` with a
``store_kind="fluree"`` Conn) and the real ``fluree validate`` binary.

Skipped unless HOLONBRIDGE_TEST_FLUREE_BIN names the binary (marker ``fluree``).
Two stores, same assertions:

* ``memory``: graphs served from a dict through a GET-only client. Needs only
  the binary (offline file mode, no server, no port).
* ``server``: a throwaway Fluree server from ``fluree_support`` and the real
  ``FlureeClient``. Also needs HOLONBRIDGE_TEST_FLUREE_SERVER=1, because it
  starts a server.

Both shape styles (anonymous and named property shapes) run for each store.
"""

from __future__ import annotations

import os

import pytest

from holonbridge import fluree_shacl, shacl
from holonbridge.conn import Conn
from holonbridge.fluree import FlureeClient

from . import fluree_support as fs

pytestmark = pytest.mark.fluree

EX = "https://example.org/"
PFX = f"""@prefix sh: <http://www.w3.org/ns/shacl#> . @prefix ex: <{EX}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> ."""

SHAPES = PFX + """
ex:PersonShape a sh:NodeShape ; sh:targetClass ex:Person ;
  sh:property [ sh:path ex:name ; sh:minCount 1 ; sh:maxCount 1 ; sh:message "name: exactly one" ] ;
  sh:property [ sh:path ex:nick ; sh:maxCount 1 ; sh:severity sh:Warning ; sh:message "nick: at most one" ] ;
  sh:sparql [ a sh:SPARQLConstraint ; sh:message "banned name" ;
     sh:select \"\"\"SELECT $this WHERE { $this <https://example.org/name> "Banned" }\"\"\" ] .
"""
SHAPES_NAMED = PFX + """
ex:PersonShape a sh:NodeShape ; sh:targetClass ex:Person ; sh:property ex:PersonNameShape, ex:PersonNickShape ;
  sh:sparql [ a sh:SPARQLConstraint ; sh:message "banned name" ;
     sh:select \"\"\"SELECT $this WHERE { $this <https://example.org/name> "Banned" }\"\"\" ] .
ex:PersonNameShape a sh:PropertyShape ; sh:path ex:name ; sh:minCount 1 ; sh:maxCount 1 ; sh:message "name: exactly one" .
ex:PersonNickShape a sh:PropertyShape ; sh:path ex:nick ; sh:maxCount 1 ; sh:severity sh:Warning ; sh:message "nick: at most one" .
"""
CONTEXT_SHAPES = PFX + """
ex:KindShape a sh:NodeShape ; sh:targetClass ex:Thing ; sh:property ex:KindPropShape .
ex:KindPropShape a sh:PropertyShape ; sh:path ex:kind ; sh:class ex:Kind ; sh:message "kind must be a declared Kind" .
"""
ONTOLOGY = PFX + "ex:k1 a ex:Kind .\n"


def person(n: str, name: str | None = "N", extra: str = "") -> str:
    nm = f'; ex:name "{name}"' if name is not None else ""
    return f"{PFX}\nex:{n} a ex:Person {nm} {extra} .\n"


class MemoryClient:
    """GET-only store: any write is a test failure."""

    def __init__(self) -> None:
        self.graphs: dict[str, str] = {}

    async def get_graph(self, conn, graph_iri):
        return self.graphs.get(graph_iri, "")

    async def _no(self, *a, **k):
        raise AssertionError("the merge gate must not write")

    update = post_graph = put_graph = drop_graph = select = construct = shacl_validate = _no

    async def aclose(self):
        pass


class Env:
    def __init__(self, conn, client, seed_fn) -> None:
        self.conn, self.client, self._seed = conn, client, seed_fn

    def seed(self, graph: str, ttl: str) -> None:
        self._seed(graph, ttl)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not fs.configured():
        pytest.skip(fs.skip_reason())
    if os.environ.get("HOLONBRIDGE_TEST_FLUREE_SERVER") != "1":
        pytest.skip("server flavour needs HOLONBRIDGE_TEST_FLUREE_SERVER=1 (starts a server)")
    srv = fs.FlureeServer(tmp_path_factory.mktemp("fluree-shacl")).start()
    yield srv
    srv.stop()


@pytest.fixture(autouse=True)
def _binary(monkeypatch):
    if not fs.configured():
        pytest.skip(fs.skip_reason())
    monkeypatch.setenv(fluree_shacl.FLUREE_BIN_ENV, fs.FLUREE_BIN)


@pytest.fixture(params=["memory", "server"])
def flavour(request):
    return request.param


@pytest.fixture(params=["anon", "named"])
async def env(request, flavour):
    shapes_ttl = SHAPES if request.param == "anon" else SHAPES_NAMED
    if flavour == "memory":
        client = MemoryClient()
        conn = Conn(base_url="http://none.test", dataset="led", overridden=False,
                    bank_name="local", store_kind="fluree")
        e = Env(conn, client, lambda g, t: client.graphs.__setitem__(
            g, client.graphs.get(g, "") + "\n" + t))
        closer = client.aclose
    else:
        server = request.getfixturevalue("server")
        server.reset()
        ds = fs.FlureeDataset(server)
        conn = fs.conn_for(ds)
        client = FlureeClient()
        e = Env(conn, client, lambda g, t: ds.get_context(g).parse(t))
        closer = client.aclose
    e.shape_style = request.param
    e.flavour = flavour
    e.seed(conn.graph("shacl"), shapes_ttl)
    e.seed(conn.context_shapes_graph, CONTEXT_SHAPES)
    e.seed(conn.ontology_graph, ONTOLOGY)
    yield e
    await closer()


def anon_delta_gap(env, request):
    """Delta fingerprints include sh:sourceShape. For an anonymous property shape that
    is a blank node whose label changes on every run, so a pre-existing violation is
    reported as introduced. Needs fix/delta-blank-node-fingerprint."""
    if env.shape_style == "anon":
        request.applymarker(pytest.mark.xfail(
            reason="needs fix/delta-blank-node-fingerprint (anonymous property shape)",
            strict=False))


async def test_full_conforming_payload(env):
    r = await shacl.validate_full(env.client, env.conn, turtle=person("a"),
                                  shapes_graph=env.conn.graph("shacl"))
    assert r.conforms and not r.results


async def test_full_violating_payload(env):
    r = await shacl.validate_full(env.client, env.conn, turtle=person("a", name=None),
                                  shapes_graph=env.conn.graph("shacl"))
    assert not r.conforms
    assert [x["focusNode"] for x in r.blocking] == [f"{EX}a"]


async def test_sparql_constraint_fires(env):
    r = await shacl.validate_full(env.client, env.conn, turtle=person("a", name="Banned"),
                                  shapes_graph=env.conn.graph("shacl"))
    assert not r.conforms and any("SPARQL" in x["sourceConstraintComponent"] for x in r.blocking)


async def test_full_merges_the_target_only_when_asked(env):
    env.seed(env.conn.holons_graph, person("p1", "A"))
    payload = f'{PFX}\nex:p1 ex:name "B" .\n'
    shapes = env.conn.graph("shacl")
    alone = await shacl.validate_full(env.client, env.conn, turtle=payload, shapes_graph=shapes)
    merged = await shacl.validate_full(env.client, env.conn, turtle=payload, shapes_graph=shapes,
                                       target_graph=env.conn.holons_graph)
    assert alone.conforms and not merged.conforms


async def test_warning_is_reported_but_not_blocking(env):
    r = await shacl.validate_full(env.client, env.conn,
                                  turtle=person("a", extra='; ex:nick "x", "y"'),
                                  shapes_graph=env.conn.graph("shacl"))
    assert not r.blocking and len(r.advisory) == 1


async def test_delta_ignores_a_violation_that_was_already_there(env, request):
    anon_delta_gap(env, request)
    env.seed(env.conn.holons_graph, person("old", name=None))
    r = await shacl.validate_delta(env.client, env.conn, turtle=person("new"),
                                   shapes_graph=env.conn.graph("shacl"),
                                   target_graph=env.conn.holons_graph)
    assert r.conforms and r.results == []


async def test_delta_reports_a_violation_the_payload_introduces(env, request):
    anon_delta_gap(env, request)
    env.seed(env.conn.holons_graph, person("old", name=None))
    r = await shacl.validate_delta(env.client, env.conn, turtle=person("bad", name=None),
                                   shapes_graph=env.conn.graph("shacl"),
                                   target_graph=env.conn.holons_graph)
    assert not r.conforms
    assert [x["focusNode"] for x in r.blocking] == [f"{EX}bad"]


async def test_replace_catches_a_violation_caused_by_removal(env):
    env.seed(env.conn.holons_graph, person("p1", "A"))
    nameless = person("p1", name=None)
    kw = {"turtle": nameless, "shapes_graph": env.conn.graph("shacl"),
          "target_graph": env.conn.holons_graph}
    merge = await shacl.validate_delta(env.client, env.conn, write_mode="merge", **kw)
    repl = await shacl.validate_delta(env.client, env.conn, write_mode="replace", **kw)
    assert merge.conforms and not repl.conforms


async def test_delta_with_a_target_that_does_not_exist_yet(env):
    r = await shacl.validate_delta(env.client, env.conn, turtle=person("a"),
                                   shapes_graph=env.conn.graph("shacl"),
                                   target_graph="urn:acl:holons:absent")
    assert r.conforms


CTX_OK = f"{PFX}\nex:t1 a ex:Thing ; ex:kind ex:k1 .\n"
CTX_BAD = f"{PFX}\nex:t1 a ex:Thing ; ex:kind ex:zz .\n"


async def test_context_pack_sees_the_ontology(env):
    kw = {"target_graph": env.conn.holons_graph, "delta": False}
    ok = await shacl.validate_context_pack(env.client, env.conn, turtle=CTX_OK, **kw)
    bad = await shacl.validate_context_pack(env.client, env.conn, turtle=CTX_BAD, **kw)
    assert ok.conforms and not bad.conforms


async def test_context_pack_delta_does_not_blame_the_payload_for_old_violations(env):
    env.seed(env.conn.holons_graph, f"{PFX}\nex:old a ex:Thing ; ex:kind ex:zz .\n")
    r = await shacl.validate_context_pack(env.client, env.conn, turtle=CTX_OK,
                                          target_graph=env.conn.holons_graph, delta=True)
    assert r.conforms and r.results == []


async def test_graph_crossing_shapes_are_refused_before_validating(env):
    if env.flavour != "memory":
        pytest.skip("replacing a stored shapes graph is covered by the memory flavour")
    env.client.graphs[env.conn.graph("shacl")] = PFX + """
ex:CrossShape a sh:NodeShape ; sh:targetClass ex:Person ;
  sh:sparql [ a sh:SPARQLConstraint ; sh:message "x" ;
     sh:select \"\"\"SELECT $this WHERE { GRAPH ?g { $this a ex:Person } }\"\"\" ] ."""
    with pytest.raises(ValueError, match="GRAPH"):
        await shacl.validate_full(env.client, env.conn, turtle=person("a"),
                                  shapes_graph=env.conn.graph("shacl"))


async def test_no_scratch_graph_and_no_write(env):
    """The gate leaves the store as it found it: nothing named scratch, no write."""
    env.seed(env.conn.holons_graph, person("a"))
    await shacl.validate_delta(env.client, env.conn, turtle=person("b", name=None),
                               shapes_graph=env.conn.graph("shacl"),
                               target_graph=env.conn.holons_graph)
    await shacl.validate_context_pack(env.client, env.conn, turtle=CTX_BAD,
                                      target_graph=env.conn.holons_graph, delta=True)
    if env.flavour == "memory":
        assert not [g for g in env.client.graphs if ":scratch:" in g]  # MemoryClient raises on writes


# --- annotated targets (RDF 1.2): memory flavour only, since seeding needs raw text ---------

ANNOTATED_TARGET = (
    f"<{EX}old> a <{EX}Person> ; <{EX}name> \"Old\" .\n"
    f"<{EX}old> <{EX}name> \"Old\" ~ _:fdb-1 .\n_:fdb-1 <{EX}by> <{EX}someone> .\n"
)


async def test_annotated_target_merges_by_text_and_relabels_clashing_labels(env):
    if env.flavour != "memory":
        pytest.skip("raw RDF 1.2 text is seeded directly in the memory flavour")
    env.client.graphs[env.conn.holons_graph] = ANNOTATED_TARGET
    payload = (
        f"<{EX}new> a <{EX}Person> ; <{EX}name> \"New\" .\n"
        f"<{EX}new> <{EX}name> \"New\" ~ _:fdb-1 .\n_:fdb-1 <{EX}by> <{EX}other> .\n"
    )
    r = await shacl.validate_full(env.client, env.conn, turtle=payload,
                                  shapes_graph=env.conn.graph("shacl"),
                                  target_graph=env.conn.holons_graph)
    assert r.conforms
    bad = await shacl.validate_full(env.client, env.conn, turtle=person("x", name=None),
                                    shapes_graph=env.conn.graph("shacl"),
                                    target_graph=env.conn.holons_graph)
    assert not bad.conforms and [x["focusNode"] for x in bad.blocking] == [f"{EX}x"]
