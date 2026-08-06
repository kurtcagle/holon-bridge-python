"""Named-query routes.

The registry is cached per connection — keyed by ``(url, dataset)`` — which
is why no ``conn.overridden`` guard appears here. A cache that cannot be
addressed across datasets cannot leak across them either; the guard exists
for process-global state, and this deliberately is not that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..deps import ClientDep, ConnDep, RegistryDep
from ..fuseki import FusekiError
from ..named_queries import apply_query_params, load_named_queries
from ..params import ParameterError
from ..sparql_kind import classify, form

router = APIRouter(tags=["named-queries"])
KIND = "named-queries"


class RunRequest(BaseModel):
    params: dict[str, object] = Field(default_factory=dict)
    dry_run: bool = Field(
        default=False, description="return the bound SPARQL without executing it"
    )
    graph: str | None = None


@router.get("/named-queries")
async def list_named_queries(
    conn: ConnDep,
    client: ClientDep,
    cache: RegistryDep,
    vocabulary: str | None = Query(default=None, pattern="^(hb|hquery)$"),
    filter: str | None = Query(default=None, description="substring match on id or label"),
    refresh: bool = Query(default=False),
) -> dict:
    result = await cache.get(
        client, conn, kind=KIND, loader=load_named_queries, refresh=refresh
    )

    queries = result.queries
    if vocabulary:
        queries = [q for q in queries if q.vocabulary == vocabulary]
    if filter:
        needle = filter.lower()
        queries = [
            q for q in queries if needle in q.id.lower() or needle in q.label.lower()
        ]

    return {
        "dataset": conn.dataset,
        "graph": conn.graph("named-queries"),
        "count": len(queries),
        "queries": [q.summary() for q in queries],
        "warnings": result.warnings,
    }


@router.get("/named-query/{query_id}")
async def get_named_query(
    query_id: str, conn: ConnDep, client: ClientDep, cache: RegistryDep
) -> dict:
    result = await cache.get(client, conn, kind=KIND, loader=load_named_queries)
    query = result.by_id(query_id)
    if query is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_named_query",
                "id": query_id,
                "available": [q.id for q in result.queries],
            },
        )
    return query.detail()


@router.post("/named-query/{query_id}/run")
async def run_named_query(
    query_id: str,
    body: RunRequest,
    conn: ConnDep,
    client: ClientDep,
    cache: RegistryDep,
) -> dict:
    result = await cache.get(client, conn, kind=KIND, loader=load_named_queries)
    query = result.by_id(query_id)
    if query is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_named_query",
                "id": query_id,
                "available": [q.id for q in result.queries],
            },
        )

    try:
        bound = apply_query_params(query, body.params)
    except ParameterError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "parameter_error", "id": query_id, "message": str(exc)},
        ) from exc

    if bound.missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_parameters",
                "id": query_id,
                "missing": bound.missing,
                "strategy": bound.strategy,
            },
        )

    envelope = {
        "id": query.id,
        "vocabulary": query.vocabulary,
        "strategy": bound.strategy,
        "bound": bound.bound,
        "unused": bound.unused,
        "sparql": bound.sparql,
    }

    if body.dry_run:
        return {**envelope, "executed": False}

    graph = body.graph or query.target_graph
    kind = classify(bound.sparql)

    try:
        if kind == "update":
            await client.update(conn, bound.sparql)
            return {**envelope, "executed": True, "ok": True}

        if form(bound.sparql) in {"CONSTRUCT", "DESCRIBE"}:
            turtle = await client.construct(conn, bound.sparql, default_graph=graph)
            return {**envelope, "executed": True, "turtle": turtle}

        results = await client.select(conn, bound.sparql, default_graph=graph)
        return {**envelope, "executed": True, "results": results}
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc


@router.post("/named-queries/reload")
async def reload_named_queries(
    conn: ConnDep, client: ClientDep, cache: RegistryDep
) -> dict:
    cache.invalidate(conn, KIND)
    result = await cache.get(
        client, conn, kind=KIND, loader=load_named_queries, refresh=True
    )
    return {
        "ok": True,
        "dataset": conn.dataset,
        "count": len(result.queries),
        "warnings": result.warnings,
    }
