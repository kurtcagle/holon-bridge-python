"""P1 core tools: endpoint/bank info, SPARQL, graph push, holon read/create.

``get_endpoint`` needs read access to the dataset/bank override state, so
this module imports ``session`` itself (not just ``mcp``/``_call``) --
see ``session.py``'s own docstring for why that has to be a qualified
``session.X`` access rather than a plain name import, and for the
2026-09-23 change from one override per process to one per caller
identity (``get_endpoint`` now reports your own override, not a value
every caller shares).
"""

from __future__ import annotations

from .. import session
from ..session import mcp, _call


@mcp.tool()
async def get_endpoint() -> dict:
    """Show the active bank, dataset, and canonical graph IRIs.

    Also reports the MCP layer's own dataset override state — deliberately
    prominent, because a wrong-dataset write with no warning is the specific
    failure this has caused before. ``datasetOverride`` is the switch *you*
    currently apply (empty if none); ``datasetOverrideSource`` is ``env``
    (from ``HOLONBRIDGE_DATASET``), ``persisted`` (restored from a prior
    session of yours after a restart), ``explicit`` (set by your own
    ``switch_dataset`` call in this session), or ``none``. Worth checking
    with this tool before a write, and especially right after any restart —
    a persisted override means your switch survived, but it is still worth
    confirming rather than assuming.

    Since 2026-09-23 this override is scoped to your own identity: a
    colleague reaching this same running process under a different identity
    can correctly get a different answer from this same call, made at the
    same moment. Before that change every caller through one process shared
    a single answer, which meant one person's earlier switch could silently
    be the reason a completely different person's write landed somewhere
    unexpected.

    ``bankOverride`` and ``bankOverrideSource`` report the same thing for the
    bank — the named backend connection your own calls are pointed at, same
    per-identity scoping as the dataset override. Both are reported because
    they fail the same silent way: the call succeeds, the data is simply not
    where you thought it was.

    Also reports ``personaOverride``/``personaOverrideSource`` — your
    active persona for the current dataset, same values as ``whoami``.
    This route now requires a resolved identity to answer that; a caller
    that used to reach it with only a bearer token needs to start sending
    an animus identity like every other identity-gated tool.
    """
    result = await _call("GET", "/endpoint")
    if isinstance(result, dict) and not result.get("error"):
        result["datasetOverride"] = session.current_dataset_override()
        result["datasetOverrideSource"] = session.current_dataset_override_source()
        result["bankOverride"] = session.current_bank_override()
        result["bankOverrideSource"] = session.current_bank_override_source()
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
async def sparql_construct(
    query: str,
    graph: str | None = None,
    format: str = "turtle",
    jsonld_form: str = "compact",
) -> str | dict:
    """Run a SPARQL CONSTRUCT or DESCRIBE. Returns Turtle, or JSON-LD.

    ``format`` is ``turtle`` (default) or ``json-ld``. For JSON-LD,
    ``jsonld_form`` is ``compact`` (the result's prefixes as the ``@context``)
    or ``expanded`` (full IRIs, no context). JSON-LD cannot express RDF 1.2
    triple terms; a result that contains one is refused -- use ``turtle``.

    On a refusal (for example a 403 for a graph you may not read) this
    returns the bridge's error object instead of Turtle, hence the
    ``str | dict`` return type -- annotated as ``str`` alone, a refusal
    failed FastMCP's output validation and hid the bridge's reason.
    """
    return await _call(
        "POST",
        "/sparql/construct",
        json_body={
            "query": query,
            "graph": graph,
            "format": format,
            "jsonld_form": jsonld_form,
        },
        text=True,
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
async def push_json_ld(
    json_ld: str,
    graph_iri: str,
    shapes_graph: str | None = None,
    mode: str = "merge",
    reduction_rule_id: str | None = None,
) -> dict:
    """Push a JSON-LD document into a named graph.

    The bridge converts it to Turtle and writes it exactly as ``push_turtle``
    does, so ``mode``, ``shapes_graph`` and ``reduction_rule_id`` mean the same
    thing. The document must be a default-graph document: one that contains
    named graphs is refused rather than partly written, and a ``@context`` given
    as a URL is refused unless the operator has enabled context fetching --
    inline the context instead.
    """
    return await _call(
        "POST",
        "/graph/push",
        json_body={
            "data": json_ld,
            "format": "json-ld",
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
async def get_holon(
    holon_iri: str,
    projection_mode: str = "immersive",
    on_behalf_of: str | None = None,
    observed_at: str | None = None,
) -> str | dict:
    """Retrieve a holon as a DataBook.

    Projection modes: immersive, cinematic, active_inference, exploded_view.

    ``observed_at`` (ISO 8601, e.g. ``2026-09-01T12:00:00Z``) reads what the
    holon's fluents were worth at that moment, from the events ledger, instead
    of current state. A timestamp with no timezone is read as UTC. Be aware of
    what this is: a valid-time read over *ground truth only* -- your persona's
    private graphs are not applied to state, and the DataBook frontmatter's
    ``scope`` says which graphs were actually read. A holon that has a
    list-mode fluent is refused, because its history cannot yet be replayed
    into the set as it stood; omit ``observed_at`` for those. There is no
    transaction-time (``inserted_at``) parameter: the time an assertion was
    recorded is not yet consulted, so it would be inert.

    ``on_behalf_of`` reads as another Person's scope (a short slug or full
    Person IRI). It works only if that Person has an active read delegation
    to you (see ``create_delegation``); the result is their view, limited to
    the graphs they delegated, and is refused otherwise. Omit it to read
    your own scope as usual.

    Returns the DataBook text on success, or the bridge's error object
    (``{"error": true, "status": ..., "detail": ...}``) on a refusal such as
    a 403 -- hence the ``str | dict`` return type: annotated as ``str``
    alone, a refusal failed FastMCP's output validation and surfaced as an
    opaque validation error instead of the bridge's reason.
    """
    params: dict = {"iri": holon_iri, "projection_mode": projection_mode}
    if on_behalf_of:
        params["on_behalf_of"] = on_behalf_of
    if observed_at:
        params["observed_at"] = observed_at
    return await _call("GET", "/holon", params=params, text=True)


@mcp.tool()
async def list_graphs(filter: str | None = None) -> dict:
    """List named graphs with triple counts, optionally filtered by substring."""
    return await _call("GET", "/graphs", params={"filter": filter} if filter else None)
