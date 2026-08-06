"""SPARQL routes.

SELECT and CONSTRUCT go to the query endpoint; UPDATE goes to the update
endpoint. They are deliberately separate routes rather than one sniffing
handler — routing an UPDATE to ``/query`` is the usual cause of an
otherwise inexplicable 400.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..deps import ClientDep, ConnDep
from ..fuseki import FusekiError
from ..sparql_kind import classify

router = APIRouter(prefix="/sparql", tags=["sparql"])


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    graph: str | None = Field(
        default=None, description="restrict to this named graph as default graph"
    )


class UpdateRequest(BaseModel):
    update: str = Field(..., min_length=1)


def _guard(text: str, *, expect_update: bool) -> None:
    """Refuse the endpoint mix-up that produces an unhelpful 400 from Jena."""
    kind = classify(text)
    if kind == "unknown":
        return  # let Jena be the authority on anything this cannot read
    if expect_update and kind == "read":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="this is a read query; use /sparql/select or /sparql/construct",
        )
    if not expect_update and kind == "update":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="this is an update; use /sparql/update",
        )


@router.post("/select")
async def select(body: QueryRequest, conn: ConnDep, client: ClientDep) -> dict:
    _guard(body.query, expect_update=False)
    try:
        return await client.select(conn, body.query, default_graph=body.graph)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc


@router.post("/construct", response_class=PlainTextResponse)
async def construct(body: QueryRequest, conn: ConnDep, client: ClientDep) -> str:
    _guard(body.query, expect_update=False)
    try:
        return await client.construct(conn, body.query, default_graph=body.graph)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc


@router.post("/update")
async def update(body: UpdateRequest, conn: ConnDep, client: ClientDep) -> dict:
    _guard(body.update, expect_update=True)
    try:
        await client.update(conn, body.update)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "dataset": conn.dataset}
