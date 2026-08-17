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
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .. import shacl as shacl_mod
from ..acl import check_replace, check_write
from ..deps import AnimusDep, ClientDep, ConnDep, SettingsDep
from ..fuseki import FusekiError
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

    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    async def _query_fn(q: str) -> dict:
        return await client.select(conn, q)

    # Replace is the stricter, independent check -- grantsWrite never
    # substitutes for it. See holonbridge.acl.check_replace.
    if body.mode == "replace":
        decision = await check_replace(
            _query_fn, conn.holons_graph, person=animus.person, target=body.graph_iri
        )
    else:
        decision = await check_write(
            _query_fn, conn.holons_graph, person=animus.person, target=body.graph_iri
        )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"{decision.reason} (graph: {body.graph_iri})",
        )

    shapes_graph = body.shapes_graph
    if shapes_graph is None and settings.shacl_required:
        shapes_graph = conn.shapes_graph

    report_payload = None
    if shapes_graph:
        try:
            if settings.shacl_delta:
                report = await shacl_mod.validate_delta(
                    client,
                    conn,
                    turtle=body.turtle,
                    shapes_graph=shapes_graph,
                    target_graph=body.graph_iri,
                    write_mode=body.mode,
                    reduction_rule_id=body.reduction_rule_id,
                )
            else:
                report = await shacl_mod.validate_full(
                    client,
                    conn,
                    turtle=body.turtle,
                    shapes_graph=shapes_graph,
                    target_graph=body.graph_iri,
                    write_mode=body.mode,
                    reduction_rule_id=body.reduction_rule_id,
                )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except FusekiError as exc:
            raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

        report_payload = report.as_dict()
        if not report.conforms:
            raise HTTPException(
                422,
                detail={"error": "shacl_violation", **report_payload},
            )

    try:
        if body.mode == "replace":
            await client.put_graph(conn, body.graph_iri, body.turtle)
        else:
            await client.post_graph(conn, body.graph_iri, body.turtle)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "graph": body.graph_iri,
        "dataset": conn.dataset,
        "mode": body.mode,
        "validated": bool(shapes_graph),
        "validation": report_payload,
    }


@router.delete("/graph")
async def drop_graph(conn: ConnDep, client: ClientDep, iri: str = Query(...)) -> dict:
    try:
        await client.drop_graph(conn, iri)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "dropped": iri, "dataset": conn.dataset}
