"""SPARQL routes.

SELECT and CONSTRUCT go to the query endpoint; UPDATE goes to the update
endpoint. They are deliberately separate routes rather than one sniffing
handler -- routing an UPDATE to ``/query`` is the usual cause of an
otherwise inexplicable 400.

CHANGED 2026-08-15: every handler now depends on ``AnimusDep`` and gates on
every graph the query or update actually references, not on the ``graph``
field alone -- that field is a routing hint (it becomes
``default-graph-uri`` for Fuseki), never an access boundary. A request
whose ``GRAPH <...>`` clauses live entirely inside the query text was
already reaching graphs the ``graph`` field said nothing about; the check
below is against ``extract_graph_refs``, which reads the same thing Fuseki
itself would execute. A query that cannot be parsed is denied, not treated
as though it referenced nothing -- see ``holonbridge.acl.authorize_query``.

Reads and writes are checked differently on purpose, matching the
read/write asymmetry in the ACL architecture DataBook: a read may be
covered by a Role's ReadGrant; a write is never covered by a Role at all,
regardless of what that Role grants for reads, and needs its own explicit
``holon:grantsWrite`` on every graph the update touches.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..acl import authorize_query, check_write, extract_graph_refs
from ..deps import AnimusDep, ClientDep, ConnDep
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


async def _authorize_read(text: str, conn: ConnDep, client: ClientDep, animus: AnimusDep) -> None:
    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    decision = await authorize_query(
        query_fn,
        conn.holons_graph,
        person=animus.person,
        sparql_text=text,
        persona_of_graph=conn.persona_for_graph,
    )
    if not decision.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=decision.reason)


async def _authorize_write(text: str, conn: ConnDep, client: ClientDep, animus: AnimusDep) -> None:
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    refs = extract_graph_refs(text)
    if refs is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="update could not be parsed for graph references; denied, not assumed safe",
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
                detail=f"{decision.reason} (graph: {graph_iri})",
            )


@router.post("/select")
async def select(
    body: QueryRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    _guard(body.query, expect_update=False)
    await _authorize_read(body.query, conn, client, animus)
    try:
        return await client.select(conn, body.query, default_graph=body.graph)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc


@router.post("/construct", response_class=PlainTextResponse)
async def construct(
    body: QueryRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> str:
    _guard(body.query, expect_update=False)
    await _authorize_read(body.query, conn, client, animus)
    try:
        return await client.construct(conn, body.query, default_graph=body.graph)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc


@router.post("/update")
async def update(
    body: UpdateRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    _guard(body.update, expect_update=True)
    await _authorize_write(body.update, conn, client, animus)
    try:
        await client.update(conn, body.update)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "dataset": conn.dataset}
