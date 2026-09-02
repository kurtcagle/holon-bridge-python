"""Named-query tools."""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def list_named_queries(
    vocabulary: str | None = None, filter: str | None = None
) -> dict:
    """List registered named queries with their parameters.

    Each entry reports its vocabulary: ``hquery`` queries take ordinary SPARQL
    variables bound through a VALUES clause, ``hb`` queries take
    ``{{placeholder}}`` substitution. Filter by ``hb`` or ``hquery``.
    """
    params = {k: v for k, v in {"vocabulary": vocabulary, "filter": filter}.items() if v}
    return await _call("GET", "/named-queries", params=params or None)


@mcp.tool()
async def get_named_query(query_id: str) -> dict:
    """Full definition of one named query, including its SPARQL body."""
    return await _call("GET", f"/named-query/{query_id}")


@mcp.tool()
async def get_named_query_schema(query_id: str) -> dict:
    """SHACL shape describing one named query's parameters.

    Derived fresh from the same parameter declarations ``run_named_query``
    binds against -- name, datatype-or-IRI, required, default,
    description -- as both a plain parameter list and a ``sh:NodeShape``
    in Turtle, for a client that wants to introspect or auto-generate a
    form without parsing the query body or any ``databook:param``-style
    comments. Gated the same way as ``get_named_query``/``run_named_query``:
    a query outside your reachable set for the current persona comes back
    as unknown, not as a visible-but-forbidden query.
    """
    return await _call("GET", f"/named-query/{query_id}/schema")


@mcp.tool()
async def run_named_query(
    query_id: str,
    params: dict | None = None,
    dry_run: bool = False,
    graph: str | None = None,
) -> dict:
    """Run a named query with parameters.

    Parameter datatypes come from the registry, so supply plain values and let
    the bridge render them. Unsupplied optional parameters stay unbound, which
    for hquery: queries means "match all". Set ``dry_run`` to see the bound
    SPARQL without executing it.

    A named query's body may itself be a SPARQL UPDATE (INSERT/DELETE/CLEAR/
    etc, not just SELECT/CONSTRUCT/ASK/DESCRIBE) -- there is no separate
    "named update" concept or tool. The bridge classifies the bound SPARQL
    at run time and dispatches to the update endpoint automatically when it
    is one. See ``register_named_query``'s docstring for how that shapes
    what gets checked at registration versus at run time.
    """
    return await _call(
        "POST",
        f"/named-query/{query_id}/run",
        json_body={"params": params or {}, "dry_run": dry_run, "graph": graph},
    )


@mcp.tool()
async def reload_named_queries() -> dict:
    """Re-read the named-query registry, discarding the cached copy."""
    return await _call("POST", "/named-queries/reload")


@mcp.tool()
async def register_named_query(
    id: str,
    sparql: str,
    label: str | None = None,
    description: str | None = None,
    target_graph: str | None = None,
    params: list[dict] | None = None,
) -> dict:
    """Register (or overwrite) a named query in the hb: vocabulary.

    ``sparql`` may be a read query (SELECT/CONSTRUCT/ASK/DESCRIBE) or a
    SPARQL UPDATE (INSERT/DELETE/CLEAR/etc) -- both are ordinary
    ``hb:NamedQuery`` entries, distinguished only at run time by what the
    body actually is. There is no separate "named update" registration
    path.

    Use ``{{paramName}}`` placeholders in ``sparql`` for substitutable
    values; declare each one in ``params`` as
    ``{"name": ..., "datatype"?: ..., "description"?: ..., "required"?:
    ..., "default"?: ...}``. Callers of ``run_named_query`` supply values
    by name.

    Registering an update-form query requires write access to every graph
    the SPARQL body itself references, not just to the named-queries
    registry -- checked once, here, at registration. Once registered,
    who can *run* it is governed by Toolset reachability, not by the
    runner's own graph-write grants -- a stored-procedure shape, not a
    proxy for the runner's own permissions. This is why registration is
    the point that needs the caller to actually hold write access to what
    the query touches: a looser check here would let anyone who can add a
    label to the registry install something that later runs with more
    reach than they personally have.

    hb: vocabulary only. An hquery: entry (typed Parameter nodes, VALUES-
    clause binding) still needs to be hand-authored as Turtle and pushed
    directly -- see the holon-named-queries skill.
    """
    return await _call(
        "POST",
        "/named-query",
        json_body={
            "id": id,
            "sparql": sparql,
            "label": label,
            "description": description,
            "target_graph": target_graph,
            "params": params or [],
        },
    )


@mcp.tool()
async def delete_named_query(query_id: str) -> dict:
    """Remove a registered named query by id.

    Requires replace-level access to the named-queries registry graph --
    the same tier as dropping a pipeline or dropping any other graph
    outright, not an ordinary write.
    """
    return await _call("DELETE", f"/named-query/{query_id}")
