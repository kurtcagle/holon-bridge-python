"""Pipeline and ingestion tools, plus async run-status lookup."""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def list_pipelines() -> dict:
    """List registered pipeline manifests."""
    return await _call("GET", "/pipelines")


@mcp.tool()
async def get_pipeline(pipeline_id: str) -> dict:
    """One manifest: its nodes, resolved run order, and any warnings.

    ``runnable`` is false when the dependency graph has a cycle; the error
    names the stages involved.
    """
    return await _call("GET", f"/pipeline/{pipeline_id}")


@mcp.tool()
async def register_pipeline(
    pipeline_id: str, manifest: str, label: str | None = None
) -> dict:
    """Register a manifest (Turtle in the build: vocabulary) into its own graph."""
    return await _call(
        "POST",
        "/pipeline",
        json_body={"id": pipeline_id, "manifest": manifest, "label": label},
    )


@mcp.tool()
async def run_pipeline(
    pipeline_id: str,
    params: dict | None = None,
    wait: bool = False,
    stop_on_error: bool = True,
) -> dict:
    """Run a pipeline in dependency order.

    Returns a message id immediately; poll ``get_message``. Set ``wait`` to
    run inline and get the finished message instead. Stages with ``llm``,
    ``human``, or ``external`` transformers are recorded as Deferred — the
    bridge does not execute them.
    """
    return await _call(
        "POST",
        "/pipeline-run",
        json_body={
            "pipeline": pipeline_id,
            "params": params or {},
            "wait": wait,
            "stop_on_error": stop_on_error,
        },
    )


@mcp.tool()
async def ingest(
    graph_iri: str | None = None,
    turtle: str | None = None,
    databook: str | None = None,
    source_graph: str | None = None,
    source_url: str | None = None,
    shapes_graph: str | None = None,
    mode: str = "merge",
    reduction_rule_id: str | None = None,
    pipeline: str | None = None,
    wait: bool = False,
) -> dict:
    """Land a payload in a named graph, then optionally run a pipeline.

    Supply exactly one of ``turtle`` (inline Turtle), ``databook`` (inline
    DataBook document — extracts the primary ``turtle``/``turtle12`` block),
    ``source_graph`` (already in the store), or ``source_url`` (fetched once,
    then sniffed as a DataBook or raw Turtle). Validation runs under the same
    gate as push, so ingestion is not a way around it — including
    ``reduction_rule_id``, which reduces the candidate write to its current
    state before validating, the same as on ``push_turtle``.

    ``graph_iri`` is optional for ``databook`` and ``source_url`` when the
    DataBook itself declares ``graph.named_graph`` in its frontmatter; an
    explicit ``graph_iri`` always wins over that declaration. It stays
    required in practice for ``turtle`` and ``source_graph``, which have no
    frontmatter to supply it from.
    """
    return await _call(
        "POST",
        "/ingest",
        json_body={
            "turtle": turtle,
            "databook": databook,
            "source_graph": source_graph,
            "source_url": source_url,
            "graph_iri": graph_iri,
            "shapes_graph": shapes_graph,
            "mode": mode,
            "reduction_rule_id": reduction_rule_id,
            "pipeline": pipeline,
            "wait": wait,
        },
    )


@mcp.tool()
async def get_message(message_id: str) -> dict:
    """Status of an asynchronous run, including per-stage outcomes."""
    return await _call("GET", f"/message/{message_id}")


@mcp.tool()
async def list_messages(limit: int = 20) -> dict:
    """Recent run records, most recent first."""
    return await _call("GET", "/messages", params={"limit": limit})
