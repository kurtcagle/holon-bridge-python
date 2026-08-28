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
``holonbridge.acl.check_replace`` for the reasoning. ``/graph`` (DROP) and
``/ingest`` remain ungated, same as before.

CHANGED 2026-08-28: the ACL check, SHACL gate, and GSP write themselves
moved into ``holonbridge.ingest.write_turtle_to_graph``, shared with
``POST /holon`` (create_holon) and ``POST /message/create``
(create_message) -- see that module's docstring. This route now only owns
the parts specific to a raw Turtle push: the optional local pre-parse and
unpacking the request body. Behaviour is unchanged; this is a pure
extraction.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

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
async def drop_graph(conn: ConnDep, client: ClientDep, iri: str = Query(...)) -> dict:
    try:
        await client.drop_graph(conn, iri)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "dropped": iri, "dataset": conn.dataset}
