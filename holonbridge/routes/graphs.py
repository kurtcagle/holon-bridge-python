"""Named-graph routes: listing, retrieval, push, and drop.

``push`` is the ingestion path and the only place the SHACL gate is armed.
When ``SHACL_REQUIRED`` is on, the shapes graph defaults to the dataset's own
``urn:{dataset}:shacl`` — which is precisely why the naming convention has to
hold. Delta mode is on by default so an existing violation elsewhere in the
target graph cannot reject an unrelated write.

CHANGED 2026-08-17: ``push`` now depends on ``AnimusDep`` and gates every
call -- ``mode=merge`` requires ``check_write`` (an explicit
``holon:grantsWrite``), ``mode=replace`` requires the stricter, independent
``check_replace`` (``holon:grantsReplace``). Before this the route had no
ACL check of any kind, unlike ``/sparql/*`` (gated since 2026-08-15).
``grantsWrite`` does NOT imply ``grantsReplace`` -- appends are the norm,
wholesale graph overwrite is the deliberately-rare exception; see
``holonbridge.acl.check_replace`` for the reasoning. ``/ingest`` was gated
2026-08-29 (see ``routes/pipeline.py``'s docstring); ``/graph`` (DROP) was
gated 2026-08-31 -- see the CHANGED note below.

CHANGED 2026-08-28: the ACL check, SHACL gate, and GSP write themselves
moved into ``holonbridge.ingest.write_turtle_to_graph``, shared with
``POST /holon`` (create_holon) and ``POST /message/create``
(create_message) -- see that module's docstring. This route now only owns
the parts specific to a raw Turtle push: the optional local pre-parse and
unpacking the request body. Behaviour is unchanged; this is a pure
extraction.

CHANGED 2026-08-31: ``DELETE /graph`` (``drop_graph``) now depends on
``AnimusDep`` and requires ``check_replace`` on the graph being dropped --
the other half of the ``HB_ACL_REMAINING_ENDPOINTS-01`` finding (the
``/graph-op`` half was fixed the same day; see ``routes/named_rules.py``,
whose ``clear``/``drop``/``copy`` operations check the identical grant for
the identical reason: DROP is unambiguously destructive and wholesale, the
same shape ``drop_pipeline`` and ``check_replace`` everywhere else in this
codebase already treat that way). Before this, ``drop_graph`` had zero ACL
check of any kind.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..acl import check_replace
from ..deps import AnimusDep, ClientDep, ConnDep, SettingsDep
from ..fuseki import FusekiError
from ..ingest import write_turtle_to_graph
from ..turtle import TurtleSyntaxError, parse

router = APIRouter(tags=["graphs"])

_LIST_QUERY = """SELECT ?g (COUNT(*) AS ?triples)
WHERE { GRAPH ?g { ?s ?p ?o } }
GROUP BY ?g
ORDER BY ?g"""


class PushRequest(BaseModel):
    turtle: str = Field(..., min_length=1)
    graph_iri: str = Field(..., min_length=1)
    shapes_graph: str | None = None
    mode: str = Field(default="merge", pattern="^(merge|replace)$")
    reduction_rule_id: str | None = Field(
        default=None,
        description=(
            "Reduce the candidate write to its current state, via this "
            "registered named rule, before validating it. Mechanism only — "
            "see holonbridge.shacl._apply_reduction for what the rule's "
            "CONSTRUCT needs to look like."
        ),
    )


@router.get("/graphs")
async def list_graphs(
    conn: ConnDep,
    client: ClientDep,
    filter: str | None = Query(default=None, description="substring match on graph IRI"),
) -> dict:
    try:
        results = await client.select(conn, _LIST_QUERY)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    graphs = [
        {"graph": row["g"]["value"], "triples": int(row["triples"]["value"])}
        for row in results.get("results", {}).get("bindings", [])
    ]
    if filter:
        graphs = [g for g in graphs if filter in g["graph"]]

    return {
        "dataset": conn.dataset,
        "overridden": conn.overridden,
        "count": len(graphs),
        "graphs": graphs,
    }


@router.get("/graph", response_class=PlainTextResponse)
async def get_graph(conn: ConnDep, client: ClientDep, iri: str = Query(...)) -> str:
    try:
        return await client.get_graph(conn, iri)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc


@router.post("/graph/push")
async def push_turtle(
    body: PushRequest, conn: ConnDep, client: ClientDep, settings: SettingsDep, animus: AnimusDep
) -> dict:
    # Optional local pre-parse. Off by default: rdflib cannot read Turtle 1.2,
    # so Jena stays the syntax authority unless the operator opts in.
    if settings.parse_mode == "local":
        try:
            parse(body.turtle)
        except TurtleSyntaxError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return await write_turtle_to_graph(
        turtle=body.turtle,
        graph_iri=body.graph_iri,
        mode=body.mode,
        shapes_graph=body.shapes_graph,
        reduction_rule_id=body.reduction_rule_id,
        conn=conn,
        client=client,
        shacl_required=settings.shacl_required,
        shacl_delta=settings.shacl_delta,
        animus=animus,
    )


@router.delete("/graph")
async def drop_graph(
    conn: ConnDep, client: ClientDep, animus: AnimusDep, iri: str = Query(...)
) -> dict:
    """Drop a named graph entirely.

    Gated by ``check_replace`` -- DROP is unambiguously destructive and
    wholesale, the same reasoning that makes ``grantsReplace`` (not
    ``grantsWrite``) the grant every other DROP-shaped operation in this
    codebase requires: ``drop_pipeline`` (routes/pipeline.py) and the
    ``drop``/``clear``/``copy`` operations of ``/graph-op``
    (routes/named_rules.py) all check the identical grant, against the
    identical kind of target, for the identical reason. Same fail-closed,
    admin-bypass-only default as ``check_replace`` everywhere else:
    absence of a matching grant is a 403, never a silent allow.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    async def _query_fn(q: str) -> dict:
        return await client.select(conn, q)

    decision = await check_replace(
        _query_fn, conn.holons_graph, person=animus.person, target=iri
    )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"{decision.reason} (graph: {iri})"
        )

    try:
        await client.drop_graph(conn, iri)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "dropped": iri, "dataset": conn.dataset}
