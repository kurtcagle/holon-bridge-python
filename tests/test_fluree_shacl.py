"""Unit tests for the read-only SHACL merge gate (fluree_shacl) and its wiring in shacl.py.

No Fluree binary or server: a fake client fails on any write, and the validator
is either a patched ``run_validate`` or a small shell script standing in for
``fluree``. The real-engine tests are in test_fluree_shacl_engine.py.
"""

from __future__ import annotations

import os
import stat
import tempfile

import pytest
from rdflib import Graph

from holonbridge import fluree_shacl, shacl
from holonbridge.conn import Conn

PFX = "@prefix ex: <https://example.org/> . @prefix sh: <http://www.w3.org/ns/shacl#> .\n"
SHAPES = PFX + "ex:S a sh:NodeShape ; sh:targetClass ex:P .\n"
OK = PFX + "[] a sh:ValidationReport ; sh:conforms true .\n"

SHAPES_IRI = "urn:led:shacl"
CTX_SHAPES_IRI = "urn:led:shacl-context"
TARGET = "urn:led:holons"


def result(node: str) -> str:
    return (
        f"[ a sh:ValidationResult ; sh:focusNode <https://example.org/{node}> ; sh:sourceShape ex:S ;"
        " sh:resultSeverity sh:Violation ;"
        " sh:sourceConstraintComponent sh:MinCountConstraintComponent ]"
    )


def bad_report(*nodes: str) -> str:
    return (
        PFX + "[] a sh:ValidationReport ; sh:conforms false ; sh:result "
        + ", ".join(result(n) for n in nodes) + " .\n"
    )


def fluree_conn() -> Conn:
    return Conn(base_url="http://fluree.test", dataset="led", overridden=False,
                bank_name="local", store_kind="fluree")


class ReadOnlyClient:
    """Serves graphs by GET; any write, SPARQL or store-side SHACL call fails."""

    def __init__(self, graphs: dict[str, str]) -> None:
        self.graphs = graphs
        self.calls: list[str] = []

    async def get_graph(self, conn, graph_iri):
        self.calls.append(f"get {graph_iri}")
        return self.graphs.get(graph_iri, "")

    async def _forbidden(self, *a, **k):
        raise AssertionError("write/scratch call on the fluree path")

    update = post_graph = put_graph = drop_graph = select = construct = shacl_validate = _forbidden


class Runner:
    """Stands in for run_validate: records inputs, answers from a function."""

    def __init__(self, answer=None) -> None:
        self.answer = answer or (lambda data, shapes: OK)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, data, shapes, *, binary=None, timeout=None):
        self.calls.append((data, shapes))
        return self.answer(data, shapes) if data.strip() else OK


@pytest.fixture
def runner(monkeypatch):
    r = Runner()
    monkeypatch.setattr(fluree_shacl, "run_validate", r)
    return r


# --- merge ---------------------------------------------------------------------


def test_merge_by_rdflib_keeps_blank_nodes_distinct():
    doc = PFX + "ex:a ex:p _:b . _:b ex:q 1 .\n"
    out = fluree_shacl.merge_documents(doc, doc)
    assert len(Graph().parse(data=out, format="nt")) == 4  # two documents, two distinct nodes


R12_TARGET = (
    "<https://example.org/a> <https://example.org/p> <https://example.org/o> "
    "~ _:fdb-1 .\n_:fdb-1 <https://example.org/by> <https://example.org/z> .\n"
)
R12_PAYLOAD = (
    "<https://example.org/b> <https://example.org/p> <https://example.org/o> "
    "~ _:fdb-1 .\n_:fdb-1 <https://example.org/by> <https://example.org/y> .\n"
)


def test_rdflib_cannot_read_the_annotation_syntax_used_below():
    with pytest.raises(Exception):  # noqa: B017 - any parser error
        Graph().parse(data=R12_TARGET, format="turtle")


def test_merge_falls_back_to_text_and_relabels_clashing_fdb_labels():
    out = fluree_shacl.merge_documents(R12_TARGET, R12_PAYLOAD)
    assert "_:fdb-1" not in out
    assert out.count("_:t_fdb-1") == 2 and out.count("_:p_fdb-1") == 2


def test_text_merge_is_deterministic_for_the_same_target():
    a = fluree_shacl.merge_documents(R12_TARGET, "")
    b = fluree_shacl.merge_documents(R12_TARGET, R12_PAYLOAD)
    assert a.strip() in b  # baseline and merged label the target identically


def test_relabel_leaves_strings_iris_and_comments_alone():
    doc = (
        '<https://example.org/_:x> <https://example.org/p> "keep _:x" ~ _:x . # _:x\n'
        '_:x <https://example.org/q> """a\n_:x""" .\n'
    )
    out = fluree_shacl.relabel_blank_nodes(doc, "p_")
    assert out.count("_:p_x") == 2
    assert "<https://example.org/_:x>" in out and '"keep _:x"' in out
    assert "# _:x" in out and '"""a\n_:x"""' in out


def test_relabel_handles_triple_terms_and_trailing_dot():
    doc = (
        "_:a <https://example.org/p> <<( _:b <https://example.org/q> _:c )>> .\n"
        "_:c <https://example.org/r> _:a.\n"
    )
    out = fluree_shacl.relabel_blank_nodes(doc, "p_")
    assert "_:p_b" in out and "_:p_c" in out and "_:p_a." in out
    assert out.replace("_:p_", "").count("_:") == 0


def test_merge_with_empty_parts():
    assert fluree_shacl.merge_documents("", "") == ""
    assert "example.org/a" in fluree_shacl.merge_documents("", PFX + "ex:a ex:p 1 .\n")
    ctx_only = fluree_shacl.merge_documents("  \n", "", [PFX + "ex:k a ex:K .\n"])
    assert "example.org/k" in ctx_only


def test_text_merge_ends_each_document_with_a_newline():
    out = fluree_shacl.merge_documents(R12_TARGET.rstrip("\n"), R12_PAYLOAD.rstrip("\n"))
    assert out.endswith("\n") and "\n_:p_fdb-1" in out


# --- the three entry points on the fluree path ----------------------------------


async def test_full_reads_only_and_merges_target_payload_context(runner):
    client = ReadOnlyClient({
        SHAPES_IRI: SHAPES,
        TARGET: PFX + "ex:old a ex:P .\n",
        "urn:led:ontology": PFX + "ex:k a ex:K .\n",
    })
    report = await shacl.validate_full(
        client, fluree_conn(), turtle=PFX + "ex:new a ex:P .\n", shapes_graph=SHAPES_IRI,
        target_graph=TARGET, context_graphs=["urn:led:ontology", "urn:led:absent"],
    )
    assert report.conforms and report.mode == "full"
    assert len(runner.calls) == 1
    data, shapes = runner.calls[0]
    assert all(x in data for x in ("example.org/old", "example.org/new", "example.org/k"))
    assert shapes == SHAPES
    assert client.calls and all(c.startswith("get ") for c in client.calls)


async def test_full_replace_leaves_the_target_out(runner):
    client = ReadOnlyClient({SHAPES_IRI: SHAPES, TARGET: PFX + "ex:old a ex:P .\n"})
    await shacl.validate_full(client, fluree_conn(), turtle=PFX + "ex:new a ex:P .\n",
                              shapes_graph=SHAPES_IRI, target_graph=TARGET, write_mode="replace")
    assert "example.org/old" not in runner.calls[0][0]
    assert f"get {TARGET}" not in client.calls


async def test_full_reports_a_violation(monkeypatch):
    monkeypatch.setattr(fluree_shacl, "run_validate", Runner(lambda d, s: bad_report("new")))
    client = ReadOnlyClient({SHAPES_IRI: SHAPES})
    r = await shacl.validate_full(client, fluree_conn(), turtle=PFX + "ex:new a ex:P .\n",
                                  shapes_graph=SHAPES_IRI)
    assert not r.conforms and r.blocking[0]["focusNode"].endswith("/new")


def _by_content(data: str, shapes: str) -> str:
    nodes = [n for n in ("old", "new") if f"example.org/{n}" in data]
    return bad_report(*nodes) if nodes else OK


async def test_delta_only_reports_what_the_payload_introduces(monkeypatch):
    runner = Runner(_by_content)
    monkeypatch.setattr(fluree_shacl, "run_validate", runner)
    client = ReadOnlyClient({SHAPES_IRI: SHAPES, TARGET: PFX + "ex:old a ex:P .\n"})
    r = await shacl.validate_delta(client, fluree_conn(), turtle=PFX + "ex:new a ex:P .\n",
                                   shapes_graph=SHAPES_IRI, target_graph=TARGET)
    assert r.mode == "delta" and len(runner.calls) == 2
    assert [x["focusNode"] for x in r.blocking] == ["https://example.org/new"]
    assert not r.conforms


async def test_delta_with_nothing_in_the_target_has_no_baseline_run(runner):
    client = ReadOnlyClient({SHAPES_IRI: SHAPES})
    r = await shacl.validate_delta(client, fluree_conn(), turtle=PFX + "ex:new a ex:P .\n",
                                   shapes_graph=SHAPES_IRI, target_graph="urn:led:absent")
    assert r.conforms and len(runner.calls) == 1


async def test_delta_replace_validates_the_payload_alone(runner):
    client = ReadOnlyClient({SHAPES_IRI: SHAPES, TARGET: PFX + "ex:old a ex:P .\n"})
    await shacl.validate_delta(client, fluree_conn(), turtle=PFX + "ex:new a ex:P .\n",
                               shapes_graph=SHAPES_IRI, target_graph=TARGET, write_mode="replace")
    (baseline, _), (merged, _) = runner.calls
    assert "example.org/old" in baseline and "example.org/old" not in merged


async def test_delta_with_an_empty_payload(runner):
    client = ReadOnlyClient({SHAPES_IRI: SHAPES, TARGET: PFX + "ex:old a ex:P .\n"})
    r = await shacl.validate_delta(client, fluree_conn(), turtle="", shapes_graph=SHAPES_IRI,
                                   target_graph=TARGET)
    assert r.conforms and r.results == []


async def test_context_pack_delta_and_full(runner):
    client = ReadOnlyClient({
        CTX_SHAPES_IRI: SHAPES,
        TARGET: PFX + "ex:old a ex:P .\n",
        "urn:led:ontology": PFX + "ex:k a ex:K .\n",
    })
    conn = fluree_conn()
    delta = await shacl.validate_context_pack(client, conn, turtle=PFX + "ex:n a ex:P .\n",
                                              target_graph=TARGET, delta=True)
    assert delta is not None and delta.mode == "delta"
    full = await shacl.validate_context_pack(client, conn, turtle=PFX + "ex:n a ex:P .\n",
                                             target_graph=TARGET, delta=False)
    assert full is not None and full.mode == "full"
    assert runner.calls and all("example.org/k" in d for d, _ in runner.calls)
    assert all(c.startswith("get ") for c in client.calls)


async def test_context_pack_absent_returns_none(runner):
    client = ReadOnlyClient({})
    assert await shacl.validate_context_pack(
        client, fluree_conn(), turtle=PFX + "ex:n a ex:P .\n", target_graph=TARGET, delta=False
    ) is None
    assert runner.calls == []


async def test_shapes_graph_empty_is_refused(runner):
    client = ReadOnlyClient({})
    with pytest.raises(ValueError, match="empty or absent"):
        await shacl.validate_full(client, fluree_conn(), turtle=PFX, shapes_graph=SHAPES_IRI)
    assert runner.calls == []


async def test_existing_guards_still_apply(runner):
    crossing = PFX + (
        'ex:X a sh:NodeShape ; sh:sparql [ sh:select "SELECT $this WHERE { GRAPH ?g { $this ?p ?o } }" ] .\n'
    )
    with pytest.raises(ValueError, match="GRAPH"):
        await shacl.validate_full(ReadOnlyClient({SHAPES_IRI: crossing}), fluree_conn(),
                                  turtle=PFX, shapes_graph=SHAPES_IRI)
    r12 = SHAPES + R12_TARGET
    with pytest.raises(shacl.UnreadableShapesError):
        await shacl.validate_full(ReadOnlyClient({SHAPES_IRI: r12}), fluree_conn(), turtle=PFX,
                                  shapes_graph=SHAPES_IRI)
    with pytest.raises(ValueError, match="context graph"):
        await shacl.validate_full(ReadOnlyClient({SHAPES_IRI: SHAPES}), fluree_conn(), turtle=PFX,
                                  shapes_graph=SHAPES_IRI, context_graphs=["urn:x> bad"])
    assert runner.calls == []


async def test_reduction_rule_is_refused_clearly(runner):
    client = ReadOnlyClient({SHAPES_IRI: SHAPES, CTX_SHAPES_IRI: SHAPES})
    conn = fluree_conn()
    for call in (
        shacl.validate_full(client, conn, turtle=PFX, shapes_graph=SHAPES_IRI,
                            reduction_rule_id="r"),
        shacl.validate_delta(client, conn, turtle=PFX, shapes_graph=SHAPES_IRI,
                             target_graph=TARGET, reduction_rule_id="r"),
        shacl.validate_context_pack(client, conn, turtle=PFX, target_graph=TARGET,
                                    delta=False, reduction_rule_id="r"),
    ):
        with pytest.raises(fluree_shacl.ReductionUnsupportedError, match="not supported"):
            await call
    assert runner.calls == []


async def test_fuseki_conn_does_not_take_the_fluree_path(runner):
    """A fuseki Conn must reach the scratch-graph code, never run_validate."""

    class Scratch(ReadOnlyClient):
        async def _ok(self, *a, **k):
            self.calls.append("write")

        update = post_graph = drop_graph = _ok

        async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
            self.calls.append("validate")
            return OK

    client = Scratch({SHAPES_IRI: SHAPES})
    conn = Conn(base_url="http://f.test", dataset="led", overridden=False, bank_name="local")
    r = await shacl.validate_full(client, conn, turtle=PFX + "ex:a a ex:P .\n",
                                  shapes_graph=SHAPES_IRI)
    assert r.conforms and runner.calls == [] and "validate" in client.calls


# --- the subprocess runner ------------------------------------------------------


def fake_bin(tmp_path, body: str) -> str:
    path = tmp_path / "fluree"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def made_dirs(monkeypatch):
    made: list[str] = []
    real = tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        made.append(d)
        return d

    monkeypatch.setattr(fluree_shacl.tempfile, "mkdtemp", spy)
    return made


async def test_run_validate_command_line_isolation_and_cleanup(tmp_path, made_dirs):
    exe = fake_bin(tmp_path, (
        '[ "$1" = "--direct" ] && [ "$2" = "validate" ] || exit 3\n'
        '[ -f "$3" ] && [ "$4" = "--shacl" ] && [ -f "$5" ] || exit 4\n'
        '[ "$6" = "--format" ] && [ "$7" = "turtle" ] || exit 4\n'
        '[ "$(cd "$HOME" && pwd -P)" = "$(pwd -P)" ] || exit 5\n'
        'echo "[] a <http://www.w3.org/ns/shacl#ValidationReport> ."\n'
        "exit 1\n"
    ))
    out = await fluree_shacl.run_validate(PFX + "ex:a a ex:P .\n", SHAPES, binary=exe)
    assert "ValidationReport" in out  # exit code 1 with a report is not an error
    assert len(made_dirs) == 1 and not os.path.exists(made_dirs[0])


async def test_run_validate_cleans_up_when_the_binary_fails(tmp_path, made_dirs):
    exe = fake_bin(tmp_path, "echo boom >&2\nexit 2\n")
    with pytest.raises(fluree_shacl.FlureeShaclError, match="boom"):
        await fluree_shacl.run_validate(PFX + "ex:a a ex:P .\n", SHAPES, binary=exe)
    assert len(made_dirs) == 1 and not os.path.exists(made_dirs[0])


async def test_run_validate_timeout_kills_child_and_cleans_up(tmp_path, made_dirs):
    exe = fake_bin(tmp_path, "exec sleep 30\n")
    with pytest.raises(fluree_shacl.FlureeShaclError, match="timed out"):
        await fluree_shacl.run_validate(PFX + "ex:a a ex:P .\n", SHAPES, binary=exe, timeout=0.5)
    assert len(made_dirs) == 1 and not os.path.exists(made_dirs[0])


async def test_run_validate_empty_data_needs_no_binary():
    out = await fluree_shacl.run_validate("  \n", SHAPES, binary="/nonexistent/fluree")
    assert shacl.parse_report(out).conforms


def test_binary_resolution(tmp_path, monkeypatch):
    exe = fake_bin(tmp_path, "exit 0\n")
    monkeypatch.setenv("FLUREE_BIN", exe)
    assert fluree_shacl.resolve_binary() == exe
    monkeypatch.setenv("FLUREE_BIN", "fluree")  # bare name: PATH lookup refused
    with pytest.raises(fluree_shacl.FlureeShaclError, match="absolute"):
        fluree_shacl.resolve_binary()
    monkeypatch.setenv("FLUREE_BIN", str(tmp_path / "missing"))
    with pytest.raises(fluree_shacl.FlureeShaclError, match="not found"):
        fluree_shacl.resolve_binary()
