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

CHANGED 2026-08-28: added ``GET /named-query/{id}/schema``
(named_query_schema). Renders the query's declared ``Parameter`` list as a
SHACL NodeShape via ``holonbridge.params.shacl_shape_for_query`` -- derived
from the exact same declarations ``apply_query_params`` binds against, so
there is one source of truth for "what does this query accept" rather than
a hand-authored shape that can drift from the real {{placeholder}}/VALUES
bindings. Goes through ``_load_and_authorise`` like get/run, so a query
outside the caller's reachable set 404s here too -- the same
indistinguishability the module docstring above already establishes for
get and run applies to the schema as well: a restricted query's parameter
contract is exactly the kind of thing that would differentially confirm
its existence to someone who can't reach it, so it gets the same treatment,
not an exception.

CHANGED 2026-09-02: added ``POST /named-query`` (register_named_query) and
``DELETE /named-query/{id}`` (delete_named_query). Every route above this
change is read-only against the registry; nothing in this file, or
anywhere else in the bridge, could previously *write* one -- named
queries had to be hand-authored as Turtle and pushed through
``/graph/push`` or a raw ``sparql_update``, with no gate specific to "may
this caller install a named query" as distinct from "may this caller
write to this graph".

There is deliberately no separate "named update" concept or route.
``sparql_kind.classify`` already tells ``run_named_query`` below whether a
bound query's body is a read or an update, and dispatches to
``client.select``/``client.construct`` or ``client.update`` accordingly --
a registered ``hb:NamedQuery`` whose ``hb:sparql`` happens to be an
INSERT/DELETE already runs correctly today. What was missing was purely
the write path, not a second read path for a different body shape. See
the ``holon-named-queries`` skill for the full design writeup, including
why this is a *definer's-rights* shape (the sensitive check happens once,
at registration, not on every invocation) and what that implies for how
carefully registration itself has to be gated.

Registration is gated more heavily than an ordinary registry write for
exactly that reason: every registration requires ``check_write`` on the
named-queries graph itself (installing *something* is an ordinary content
write), but a body that ``classify()`` calls an update ADDITIONALLY
requires ``check_write`` on every graph ``extract_graph_refs`` finds
referenced in the body -- the same gate ``routes/sparql.py``'s
``_authorize_write`` applies to a raw ``POST /sparql/update``. Once
installed, ``run_named_query`` gates invocation by Toolset reachability
alone, not by the invoker's own graph-write grants, so if registration
didn't check the referenced graphs, any caller who could write so much as
a label into the named-queries graph could install a query that clears
someone else's graph, and any Toolset member could then run it. Deletion
is gated by ``check_replace`` instead, matching ``drop_pipeline``/
``DELETE /graph``: removing already-registered content is destructive,
not an append.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..acl import check_replace, check_write, extract_graph_refs
from ..deps import AnimusDep, ClientDep, ConnDep, PersonasDep, RegistryDep
from ..fuseki import FusekiError
from ..named_queries import apply_query_params, load_named_queries
from ..params import ParameterError, shacl_shape_for_query
from ..sparql_kind import classify, form
from ..toolset import bind_persona_param, resolve_reachable
from ..turtle import literal

router = APIRouter(tags=["named-queries"])
KIND = "named-queries"

#: Namespace this route registers new hb: queries under. Matches the
#: existing hb: scheme's own namespace (holonbridge/named_queries.py's
#: HB_NAMESPACE) and the per-parameter synthetic path convention
#: holonbridge/params.py already uses (_PARAM_PATH_NS).
HB = "https://w3id.org/holonbridge/"


def _query_node(query_id: str) -> str:
    return f"{HB}query/{query_id}"


class RunRequest(BaseModel):
    params: dict[str, object] = Field(default_factory=dict)
    dry_run: bool = Field(
        default=False, description="return the bound SPARQL without executing it"
    )
    graph: str | None = None


class ParameterSpec(BaseModel):
    name: str = Field(..., min_length=1)
    datatype: str | None = None
    description: str = ""
    required: bool = False
    default: str | None = None


class RegisterNamedQueryRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    sparql: str = Field(..., min_length=1)
    label: str | None = None
    description: str | None = None
    target_graph: str | None = None
    params: list[ParameterSpec] = Field(default_factory=list)


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
    404. Centralised so get/run/schema can't drift on what "reachable"
    means.
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


@router.get("/named-query/{query_id}/schema")
async def named_query_schema(
    query_id: str,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
) -> dict:
    """SHACL parameter shape for a named query.

    Intended for a form/widget-generating client that needs to know a
    query's parameter contract (name, datatype-or-IRI, required, default,
    description) without parsing ``databook:param``-style directives or
    the query body itself. The shape is generated fresh from the
    registry's own ``Parameter`` declarations on every call, not stored —
    so it can never drift from what ``run`` actually binds against.
    """
    query, _persona = await _load_and_authorise(
        query_id, conn, client, animus, personas, cache
    )
    return {
        "id": query.id,
        "iri": query.iri,
        "parameters": query.summary()["parameters"],
        "shapesTurtle": shacl_shape_for_query(query.id, query.iri, query.params),
    }


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


# --- registration ---------------------------------------------------------


async def _authorize_update_body_refs(
    sparql: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> None:
    """For a body that classifies as an update: require check_write on
    every graph the body itself references, exactly like
    routes/sparql.py's _authorize_write does for a raw POST
    /sparql/update. See this module's docstring for why registration, not
    just invocation, is where this has to be checked.
    """
    refs = extract_graph_refs(sparql)
    if refs is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=(
                "sparql could not be parsed for graph references; denied, not "
                "assumed safe (an update-form named query requires write "
                "access to every graph it touches, checked at registration)"
            ),
        )

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    for graph_iri in refs:
        decision = await check_write(
            query_fn, conn.holons_graph, person=animus.person, target=graph_iri
        )
        if not decision.allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=(
                    f"{decision.reason} (graph: {graph_iri}, referenced by "
                    "the sparql body)"
                ),
            )


def _render_registration_turtle(body: RegisterNamedQueryRequest, registered_at: str) -> str:
    """Turtle for one hb:NamedQuery resource plus its hb:parameter blank
    nodes. Property local names match holonbridge/named_queries.py's
    _QUERY_FIELDS/_PARAM_FIELDS precedence-first aliases exactly, so the
    loader reads back exactly what this writes.
    """
    node = f"<{_query_node(body.id)}>"

    lines = [
        f"{node} a <{HB}NamedQuery> ;",
        f"  <{HB}id> {literal(body.id)} ;",
        f"  <{HB}sparql> {literal(body.sparql)} ;",
    ]
    if body.label:
        lines.append(f"  <{HB}label> {literal(body.label)} ;")
    if body.description:
        lines.append(f"  <{HB}description> {literal(body.description)} ;")
    if body.target_graph:
        lines.append(f"  <{HB}targetGraph> <{body.target_graph}> ;")
    lines.append(
        "  <" + HB + "registeredAt> "
        + literal(registered_at, datatype="<http://www.w3.org/2001/XMLSchema#dateTime>")
        + " ."
    )

    param_links: list[str] = []
    param_blocks: list[str] = []
    for i, p in enumerate(body.params):
        pnode = f"_:param{i}"
        param_links.append(f"{node} <{HB}parameter> {pnode} .")
        fields = [f"{pnode} <{HB}name> {literal(p.name)} ;"]
        if p.datatype:
            fields.append(f"  <{HB}datatype> {literal(p.datatype)} ;")
        if p.description:
            fields.append(f"  <{HB}description> {literal(p.description)} ;")
        fields.append(f"  <{HB}required> {'true' if p.required else 'false'} ;")
        if p.default is not None:
            fields.append(f"  <{HB}default> {literal(p.default)} ;")
        fields[-1] = fields[-1][:-1] + "."  # trailing " ;" -> " ."
        param_blocks.append("\n".join(fields))

    return "\n".join(lines + param_links + param_blocks)


@router.post("/named-query")
async def register_named_query(
    body: RegisterNamedQueryRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
) -> dict:
    """Register (or overwrite) a named query in the hb: vocabulary.

    See this module's docstring for the full ACL reasoning. Short version:
    check_write on the named-queries graph always; a body that classifies
    as an update additionally needs check_write on every graph it itself
    references. hb: only -- an hquery: entry's richer typed-Parameter
    shape still needs hand-authored Turtle pushed directly (e.g. via
    /graph/push); see the holon-named-queries skill.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    registry = conn.graph("named-queries")

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    decision = await check_write(
        query_fn, conn.holons_graph, person=animus.person, target=registry
    )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"{decision.reason} (graph: {registry})"
        )

    if classify(body.sparql) == "update":
        await _authorize_update_body_refs(body.sparql, conn, client, animus)

    registered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    insert_body = _render_registration_turtle(body, registered_at)
    node = f"<{_query_node(body.id)}>"

    update = f"""PREFIX hb: <{HB}>
DELETE WHERE {{ GRAPH <{registry}> {{ {node} ?p ?o }} }} ;
DELETE WHERE {{ GRAPH <{registry}> {{ {node} hb:parameter ?param . ?param ?pp ?oo }} }} ;
INSERT DATA {{ GRAPH <{registry}> {{
{insert_body}
}} }}"""

    try:
        await client.update(conn, update)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    cache.invalidate(conn, KIND)
    return {
        "ok": True,
        "id": body.id,
        "iri": _query_node(body.id),
        "graph": registry,
        "kind": classify(body.sparql),
    }


@router.delete("/named-query/{query_id}")
async def delete_named_query(
    query_id: str,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
) -> dict:
    """Remove a registered named query. check_replace on the registry
    graph -- removing already-registered content is destructive, wholesale
    on that entry, the same tier as drop_pipeline / DELETE /graph, not an
    ordinary append-shaped write.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    registry = conn.graph("named-queries")

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    decision = await check_replace(
        query_fn, conn.holons_graph, person=animus.person, target=registry
    )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"{decision.reason} (graph: {registry})"
        )

    result = await cache.get(client, conn, kind=KIND, loader=load_named_queries)
    query = result.by_id(query_id)
    if query is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_named_query", "id": query_id},
        )

    node = f"<{query.iri}>"
    update = f"""PREFIX hb: <{HB}>
DELETE WHERE {{ GRAPH <{registry}> {{ {node} ?p ?o }} }} ;
DELETE WHERE {{ GRAPH <{registry}> {{ {node} hb:parameter ?param . ?param ?pp ?oo }} }}"""

    try:
        await client.update(conn, update)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    cache.invalidate(conn, KIND)
    return {"ok": True, "id": query_id, "graph": registry}
