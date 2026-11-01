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

CHANGED 2026-09-30 (audit): each handler also hands the graphs it touched,
a digest of an update's text (never the text), and the reason for any
refusal to ``holonbridge.audit.annotate``. A read of *another person's*
private persona graph -- whether allowed by a delegation, an admin bypass
or a persona-wide ReadGrant -- is flagged as a security read, so it is
recorded at the ``standard`` audit level.

CHANGED 2026-09-30 (graph-reference hardening): update authorisation moved
into ``acl.authorize_update`` and the graph analysis behind both checks was
rewritten. The old check saw only ``GRAPH <iri>`` and INSERT/DELETE quads, so
an update or query reached "references no graph" -- and was allowed -- through
``GRAPH ?g`` (however ``?g`` was bound), FROM/FROM NAMED, WITH, USING, and
DROP/CLEAR/CREATE/COPY/MOVE/ADD/LOAD. A live test wrote to a graph with no
grant on it through ``INSERT { GRAPH ?g ... } WHERE { VALUES ?g { <g> } }``.
See ``acl.analyse_graph_refs`` and ``acl.authorize_update`` for the rules.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .. import audit
from ..acl import analyse_graph_refs, authorize_query, authorize_update
from ..deps import AnimusDep, ClientDep, ConnDep
from ..fuseki import FusekiError
from ..trigger_hook import after_write_update
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
    analysis = analyse_graph_refs(text)
    refs = analysis.all_refs if analysis else frozenset()
    audit.annotate(graphs=sorted(refs))
    if not decision.allowed:
        audit.annotate(reason=decision.reason)
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=decision.reason)
    if animus.person and audit.foreign_user_graphs(refs, conn.person_slug(animus.person)):
        audit.annotate(
            security_read=True, reason="read of another person's private persona graph"
        )


async def _authorize_write(text: str, conn: ConnDep, client: ClientDep, animus: AnimusDep) -> None:
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    analysis = analyse_graph_refs(text)
    if analysis is not None:
        audit.annotate(graphs=sorted(analysis.all_refs))

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    decision = await authorize_update(
        query_fn, conn.holons_graph, person=animus.person, sparql_text=text
    )
    if not decision.allowed:
        audit.annotate(reason=decision.reason)
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=decision.reason)


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
    digest, size = audit.payload_digest(body.update)
    audit.annotate(payload_hash=digest, payload_bytes=size)
    await _authorize_write(body.update, conn, client, animus)
    try:
        await client.update(conn, body.update)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    await after_write_update(client, conn, body.update, source="POST /sparql/update")
    return {"ok": True, "dataset": conn.dataset}
