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
