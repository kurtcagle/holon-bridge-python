"""Read-only SHACL merge gate for the Fluree store kind.

Fluree has no SHACL endpoint over a graph, every scratch write is a commit, and
it validates each named graph on its own at transaction time, so it never checks
a rule that crosses graphs. The Fuseki gate (scratch graph, COPY/POST/ADD, ask
the store, DROP) therefore must not run against it. This module does the same
job without writing anything:

1. read the target graph, the context graphs and the shapes over the Graph
   Store endpoint (``get_graph``; GET only);
2. merge them with the payload in memory;
3. validate the merged Turtle with the Fluree CLI's offline file mode
   (``fluree --direct validate <data> --shacl <shapes> --format turtle``), which
   builds an ephemeral in-memory ledger, persists nothing and commits nothing.

The Turtle SHACL report it prints is what :func:`holonbridge.shacl.parse_report`
already consumes, so ``shacl.py`` only has to choose this path
(``conn.store_kind == "fluree"``) and keep its own guards.

Merging. When every document parses with rdflib the merge is a graph union
serialised as N-Triples (each document is parsed separately, so blank nodes
stay distinct). rdflib cannot read RDF 1.2 syntax (``~`` reifiers,
``<<( s p o )>>``), which Fluree's export uses for annotations; when any
document fails to parse the documents are joined as text instead, which is
valid Turtle 1.2 because the export uses full IRIs and labelled blank nodes.
Text joining would merge identically labelled blank nodes across documents, so
in that mode every document's ``_:labels`` are first given a per-document
prefix (see :func:`relabel_blank_nodes`). Anonymous ``[]`` nodes need no care:
the parser makes each one fresh.

Not supported here: reduction rules (``reduction_rule_id``), which need a
store-side graph to run a CONSTRUCT over. They are refused with a clear error.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from rdflib import Graph

from .conn import Conn

#: Environment variable naming the ``fluree`` binary.
FLUREE_BIN_ENV = "FLUREE_BIN"
#: Install path used when ``FLUREE_BIN`` is unset. ``fluree`` on PATH is not
#: acceptable: a launchd job has a minimal PATH.
DEFAULT_FLUREE_BIN = "/Users/ben/.local/opt/fluree/4.2.4/fluree"
#: Seconds one ``fluree validate`` call may take.
FLUREE_TIMEOUT_ENV = "FLUREE_VALIDATE_TIMEOUT"
DEFAULT_TIMEOUT = 60.0

_EMPTY_REPORT = (
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n\n"
    "[] a sh:ValidationReport ;\n    sh:conforms true .\n"
)


class FlureeShaclError(RuntimeError):
    """The offline validator could not produce a report."""


class ReductionUnsupportedError(ValueError):
    """``reduction_rule_id`` was given on the Fluree path."""


def is_fluree(conn: Conn) -> bool:
    return getattr(conn, "store_kind", "fuseki") == "fluree"


def reject_reduction(reduction_rule_id: str | None) -> None:
    if reduction_rule_id:
        raise ReductionUnsupportedError(
            f"reduction_rule_id {reduction_rule_id!r} is not supported for the fluree "
            "store: the read-only SHACL merge gate has no store-side scratch graph to "
            "run a reduction rule over. Validate without a reduction rule, or use fuseki."
        )


def resolve_binary(binary: str | None = None) -> str:
    """Explicit path, else ``FLUREE_BIN``, else the install path. Never PATH."""
    path = binary or os.environ.get(FLUREE_BIN_ENV, "").strip() or DEFAULT_FLUREE_BIN
    if not os.path.isabs(path):
        raise FlureeShaclError(
            f"{FLUREE_BIN_ENV} must be an absolute path to the fluree binary, got {path!r}"
        )
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise FlureeShaclError(f"fluree binary not found or not executable: {path}")
    return path


# --- blank-node relabelling --------------------------------------------------

# Strings, IRIs and comments are matched first so a ``_:x`` inside one is left
# alone; only the last alternative (a blank node label) is rewritten.
_TOKEN_RE = re.compile(
    r'"""(?:[^"\\]|\\.|"(?!""))*"""'
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"
    r'|"(?:[^"\\\n]|\\.)*"'
    r"|'(?:[^'\\\n]|\\.)*'"
    r'|<[^<>"{}|^`\\\s]*>'
    r"|#[^\n]*"
    r"|(?P<bnode>_:[A-Za-z0-9_](?:[A-Za-z0-9_.\-]*[A-Za-z0-9_\-])?)",
    re.DOTALL,
)


def relabel_blank_nodes(turtle: str, prefix: str) -> str:
    """Prefix every ``_:label`` in ``turtle`` so it cannot equal another document's.

    Limits: a textual rewrite, not a parse. It covers labelled blank nodes in
    any position, reifier labels (``~ _:r``) and labels inside triple terms. It
    does not rename ``[]`` nodes (not needed: each is fresh per parse). Unusual
    escapes inside a label are not handled.
    """

    def swap(match: re.Match[str]) -> str:
        label = match.group("bnode")
        if label is None:
            return match.group(0)
        return f"_:{prefix}{label[2:]}"

    return _TOKEN_RE.sub(swap, turtle)


def merge_documents(
    target: str = "", payload: str = "", contexts: Sequence[str] = ()
) -> str:
    """Merge Turtle documents into one document. Empty ones are skipped.

    rdflib union (N-Triples out) if every document parses, otherwise a text
    join with per-document blank-node prefixes. The prefixes are deterministic
    so the same target relabels identically in a baseline and a merged run.
    """
    labelled = [("t_", target), *((f"c{i}_", c) for i, c in enumerate(contexts)), ("p_", payload)]
    present = [(p, d) for p, d in labelled if d.strip()]
    if not present:
        return ""
    try:
        graph = Graph()
        for _, doc in present:
            graph.parse(data=doc, format="turtle")
        return graph.serialize(format="nt")
    except Exception:  # noqa: BLE001 - rdflib raises several parser types
        return "\n".join(relabel_blank_nodes(d, p).rstrip("\n") + "\n" for p, d in present)


# --- running the validator -----------------------------------------------------


async def run_validate(
    data_turtle: str,
    shapes_turtle: str,
    *,
    binary: str | None = None,
    timeout: float | None = None,
) -> str:
    """Validate ``data_turtle`` against ``shapes_turtle``; return the report Turtle.

    Temp files live in a private directory removed on every exit path. The
    child runs with ``--direct`` (never routed through a local server), with
    that directory as working directory and as HOME/XDG roots, so it cannot
    see a real project or data directory.
    """
    if not data_turtle.strip():
        return _EMPTY_REPORT
    exe = resolve_binary(binary)
    if timeout is None:
        timeout = float(os.environ.get(FLUREE_TIMEOUT_ENV, "") or DEFAULT_TIMEOUT)
    workdir = tempfile.mkdtemp(prefix="hb-fluree-shacl-")
    try:
        os.chmod(workdir, 0o700)
        data_path, shapes_path = Path(workdir) / "data.ttl", Path(workdir) / "shapes.ttl"
        data_path.write_text(data_turtle, encoding="utf-8")
        shapes_path.write_text(shapes_turtle, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": workdir,
            "XDG_DATA_HOME": workdir,
            "XDG_CONFIG_HOME": workdir,
            "XDG_CACHE_HOME": workdir,
            "NO_COLOR": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            exe, "--direct", "validate", str(data_path),
            "--shacl", str(shapes_path), "--format", "turtle",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=workdir, env=env,
        )  # fmt: skip
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:  # timeout or cancellation: stop our own child only
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
    except asyncio.TimeoutError as exc:
        raise FlureeShaclError(f"fluree validate timed out after {timeout:g}s") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    text = out.decode("utf-8", errors="replace")
    # Exit code 1 means "results exist", not failure: trust the report text.
    if "ValidationReport" not in text:
        detail = " ".join(err.decode("utf-8", errors="replace").split())[:300]
        raise FlureeShaclError(
            f"fluree validate exited {proc.returncode} without a report: {detail}"
        )
    return text


# --- reading and validating ----------------------------------------------------


async def read_context(client, conn: Conn, graphs: Sequence[str]) -> list[str]:
    """Context graphs as Turtle; an absent graph reads as empty (GET only)."""
    return [await client.get_graph(conn, g) for g in graphs]


async def full_report(
    client,
    conn: Conn,
    *,
    turtle: str,
    shapes: str,
    target_graph: str | None,
    write_mode: str,
    context_graphs: Sequence[str],
    binary: str | None = None,
) -> str:
    """Turtle report for the payload (plus target unless replace, plus context)."""
    base = ""
    if target_graph and write_mode != "replace":
        base = await client.get_graph(conn, target_graph)
    contexts = await read_context(client, conn, context_graphs)
    merged = merge_documents(base, turtle, contexts)
    return await run_validate(merged, shapes, binary=binary)


async def delta_reports(
    client,
    conn: Conn,
    *,
    turtle: str,
    shapes: str,
    target_graph: str,
    write_mode: str,
    context_graphs: Sequence[str],
    binary: str | None = None,
) -> tuple[str | None, str]:
    """``(baseline_report, merged_report)``; baseline is ``None`` when the target
    plus context hold nothing (nothing to be blamed on the payload)."""
    base = await client.get_graph(conn, target_graph)
    contexts = await read_context(client, conn, context_graphs)
    baseline_doc = merge_documents(base, "", contexts)
    merged_doc = merge_documents("" if write_mode == "replace" else base, turtle, contexts)
    if not baseline_doc.strip():
        return None, await run_validate(merged_doc, shapes, binary=binary)
    baseline, merged = await asyncio.gather(
        run_validate(baseline_doc, shapes, binary=binary),
        run_validate(merged_doc, shapes, binary=binary),
    )
    return baseline, merged
