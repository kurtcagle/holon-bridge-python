"""P1 core tools: endpoint/bank info, SPARQL, graph push, holon read/create.

``get_endpoint`` needs read access to the dataset/bank override state, so
this module imports ``session`` itself (not just ``mcp``/``_call``) --
see ``session.py``'s own docstring for why that has to be a qualified
``session.X`` access rather than a plain name import.
"""

from __future__ import annotations

from .. import session
from ..session import mcp, _call


@mcp.tool()
async def get_endpoint() -> dict:
    """Show the active bank, dataset, and canonical graph IRIs.

    Also reports the MCP layer's own dataset override state — deliberately
    prominent, because a wrong-dataset write with no warning is the specific
    failure this has caused before. ``datasetOverride`` is the switch this
    MCP process currently applies (empty if none); ``datasetOverrideSource``
    is ``env`` (from ``HOLONBRIDGE_DATASET``), ``persisted`` (restored from a
    prior session after a restart), ``explicit`` (set by ``switch_dataset``
    in this session), or ``none``. Worth checking with this tool before a
    write, and especially right after any restart — a persisted override
    means the switch survived, but it is still worth confirming rather than
    assuming.

    ``bankOverride`` and ``bankOverrideSource`` report the same thing for the
    bank — the named backend connection this process is pointed at. Both are
    reported because they fail the same silent way: the call succeeds, the
    data is simply not where you thought it was.

    Also reports ``personaOverride``/``personaOverrideSource`` — your
    active persona for the current dataset, same values as ``whoami``.
    This route now requires a resolved identity to answer that; a caller
    that used to reach it with only a bearer token needs to start sending
    an animus identity like every other identity-gated tool.
    """
    result = await _call("GET", "/endpoint")
    if isinstance(result, dict) and not result.get("error"):
        result["datasetOverride"] = session._dataset_override
        result["datasetOverrideSource"] = session._dataset_override_source
        result["bankOverride"] = session._bank_override
        result["bankOverrideSource"] = session._bank_override_source
    return result


@mcp.tool()
async def list_endpoints() -> dict:
    """List all named banks and which one is active, as the bridge sees it."""
    return await _call("GET", "/endpoints")


@mcp.tool()
async def set_endpoint(name: str) -> dict:
    """Switch the bridge's own active bank by name (server-side, affects every client)."""
    return await _call("POST", "/endpoint", json_body={"name": name})


@mcp.tool()
async def sparql_select(query: str, graph: str | None = None) -> dict:
    """Run a SPARQL SELECT or ASK. Returns SPARQL JSON results."""
    return await _call(
        "POST", "/sparql/select", json_body={"query": query, "graph": graph}
    )


@mcp.tool()
async def sparql_construct(query: str, graph: str | None = None) -> str:
    """Run a SPARQL CONSTRUCT or DESCRIBE. Returns Turtle."""
    return await _call(
        "POST", "/sparql/construct", json_body={"query": query, "graph": graph}, text=True
    )


@mcp.tool()
async def sparql_update(update: str) -> dict:
    """Run a SPARQL UPDATE (INSERT DATA, DELETE, CLEAR, DROP, COPY)."""
    return await _call("POST", "/sparql/update", json_body={"update": update})


@mcp.tool()
async def push_turtle(
    turtle: str,
    graph_iri: str,
    shapes_graph: str | None = None,
    mode: str = "merge",
    reduction_rule_id: str | None = None,
) -> dict:
    """Push Turtle into a named graph.

    ``mode`` is ``merge`` (GSP POST) or ``replace`` (GSP PUT). Supplying
    ``shapes_graph`` validates before the write and rejects on new violations.

    ``reduction_rule_id`` names a registered named rule that reduces the
    candidate write to its current state before validating — mechanism for
    fluent-style data, where a shape's cardinality constraints only make
    sense against "what's current," not the full history a bitemporal graph
    accumulates. The rule itself defines what "current" means.
    """
    return await _call(
        "POST",
        "/graph/push",
        json_body={
            "turtle": turtle,
            "graph_iri": graph_iri,
            "shapes_graph": shapes_graph,
            "mode": mode,
            "reduction_rule_id": reduction_rule_id,
        },
    )


@mcp.tool()
async def create_holon(
    databook: str,
    block_id: str | None = None,
    graph_iri: str | None = None,
    shapes_graph: str | None = None,
    mode: str = "merge",
    reduction_rule_id: str | None = None,
) -> dict:
    """Create or merge into a holon from a DataBook message.

    Unlike ``push_turtle``, which takes raw Turtle and an explicit
    ``graph_iri``, this takes a full DataBook (frontmatter plus one or more
    fenced blocks) and extracts the RDF for you -- the first turtle,
    turtle12, or json-ld block, or the one named by ``block_id``. A
    json-ld block is converted to Turtle before it's written; Fuseki only
    ever receives Turtle either way.

    ``graph_iri`` overrides the DataBook's own ``graph.named_graph``
    frontmatter if both are given; one of the two is required, there is
    no default target graph here. Everything else -- ``shapes_graph``,
    ``mode``, ``reduction_rule_id`` -- means exactly what it means on
    ``push_turtle``, because both call the same gated write path on the
    bridge.
    """
    return await _call(
        "POST",
        "/holon",
        json_body={
            "databook": databook,
            "block_id": block_id,
            "graph_iri": graph_iri,
            "shapes_graph": shapes_graph,
            "mode": mode,
            "reduction_rule_id": reduction_rule_id,
        },
    )


@mcp.tool()
async def get_holon(holon_iri: str, projection_mode: str = "immersive") -> str:
    """Retrieve a holon as a DataBook.

    Projection modes: immersive, cinematic, active_inference, exploded_view.
    """
    return await _call(
        "GET",
        "/holon",
        params={"iri": holon_iri, "projection_mode": projection_mode},
        text=True,
    )


@mcp.tool()
async def list_graphs(filter: str | None = None) -> dict:
    """List named graphs with triple counts, optionally filtered by substring."""
    return await _call("GET", "/graphs", params={"filter": filter} if filter else None)
