"""Named-query routes.

The registry is cached per connection — keyed by ``(url, dataset)`` — which
is why no ``conn.overridden`` guard appears here. A cache that cannot be
addressed across datasets cannot leak across them either; the guard exists
for process-global state, and this deliberately is not that.

CHANGED 2026-08-18: every route here now requires a resolved identity
(``AnimusDep``), which none of them did before -- same shape as the
``/endpoint`` and ``/holon`` breaking changes already shipped. Toolset
reachability (``toolset.resolve_reachable``) is per-persona, so listing,
reading, or running a named query can't be answered without knowing who's
asking and what they're currently switched to. A caller that was hitting
any of these with only a bearer token will start getting a 401 and needs
to send ``X-Holon-Animus-Id`` like every other identity-gated route.

Toolset filtering gates listing AND running, not just listing — see the
design doc's §2 for why a boundary that hides a tool from the list but
still executes it on request isn't really a boundary. A caller asking for
a query outside their reachable set gets the same 404 shape as an unknown
id, so a restricted tool never differentially confirms its own existence
to someone who can't reach it.

``personas.get()`` returns a short persona name ("carlo"), the same shape
``resolve_reachable`` itself takes (see toolset.py). ``bind_persona_param``
needs the full Persona IRI instead — converted once, right before that
call, via ``conn.persona_graph`` — since it has no ``Conn`` to convert
with itself.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..deps import AnimusDep, ClientDep, ConnDep, PersonasDep, RegistryDep
from ..fuseki import FusekiError
from ..named_queries import apply_query_params, load_named_queries
from ..params import ParameterError
from ..sparql_kind import classify, form
from ..toolset import bind_persona_param, resolve_reachable

router = APIRouter(tags=["named-queries"])
KIND = "named-queries"


class RunRequest(BaseModel):
    params: dict[str, object] = Field(default_factory=dict)
    dry_run: bool = Field(
        default=False, description="return the bound SPARQL without executing it"
    )
    graph: str | None = None


def _not_found(query_id: str, available_ids: list[str]) -> HTTPException:
    """Same 404 shape whether the id is genuinely unknown or just outside
    this caller's reachable set -- see this module's docstring for why
    that indistinguishability is the point, not an accident."""
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={
            "error": "unknown_named_query",
            "id": query_id,
            "available": available_ids,
        },
    )


async def _reachable_ids(result, conn, client, persona: str | None) -> set[str]:
    """The subset of `result.queries` (by id) this persona can reach.
    Shared by list/get/run so all three agree on exactly the same set —
    resolved once per request, not cached, since it depends on the
    caller's current persona switch, not just the dataset. `persona` is
    the short name, passed straight through to resolve_reachable, which
    does its own conversion to the full Persona IRI.
    """

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    id_by_iri = {q.iri: q.id for q in result.queries}
    reachable_iris = await resolve_reachable(
        query_fn, conn, persona=persona, candidate_iris=list(id_by_iri)
    )
    return {id_by_iri[iri] for iri in reachable_iris}


@router.get("/named-queries")
async def list_named_queries(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
    vocabulary: str | None = Query(default=None, pattern="^(hb|hquery)$"),
    filter: str | None = Query(default=None, description="substring match on id or label"),
    refresh: bool = Query(default=False),
) -> dict:
    result = await cache.get(
        client, conn, kind=KIND, loader=load_named_queries, refresh=refresh
    )

    persona, _source = personas.get(person_id=animus.person, dataset=conn.dataset)
    reachable_ids = await _reachable_ids(result, conn, client, persona)

    queries = [q for q in result.queries if q.id in reachable_ids]
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


async def _load_and_authorise(query_id: str, conn, client, animus, personas, cache):
    """Load the registry, resolve this caller's reachable set, and return
    the query if both known and reachable — otherwise raise the shared
    404. Centralised so get/run can't drift on what "reachable" means.
    """
    result = await cache.get(client, conn, kind=KIND, loader=load_named_queries)
    query = result.by_id(query_id)
    available_ids = [q.id for q in result.queries]
    if query is None:
        raise _not_found(query_id, available_ids)

    persona, _source = personas.get(person_id=animus.person, dataset=conn.dataset)
    reachable_ids = await _reachable_ids(result, conn, client, persona)

    if query.id not in reachable_ids:
        raise _not_found(query_id, sorted(reachable_ids))

    return query, persona


@router.get("/named-query/{query_id}")
async def get_named_query(
    query_id: str,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
) -> dict:
    query, _persona = await _load_and_authorise(
        query_id, conn, client, animus, personas, cache
    )
    return query.detail()


@router.post("/named-query/{query_id}/run")
async def run_named_query(
    query_id: str,
    body: RunRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
) -> dict:
    query, persona = await _load_and_authorise(
        query_id, conn, client, animus, personas, cache
    )

    persona_iri = conn.persona_graph(persona) if persona else None
    params = bind_persona_param(
        body.params,
        persona_iri=persona_iri,
        declares_persona="persona" in query.declared,
    )

    try:
        bound = apply_query_params(query, params)
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
    """Registry cache maintenance, not tool access — deliberately left
    ungated by identity/Toolset, same as before this change. Reloading
    doesn't reveal or run anything; it just discards a cached copy.
    """
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
