"""Audit routes: read the audit log, and get/set the dataset's audit level.

Admin-only, and the only supported way to read the audit graph over HTTP:
``GET /graph?iri=<audit graph>`` refuses it (see ``routes/graphs.py``) and
raw SPARQL against it is admin-only (see ``acl.authorize_query``). The
audit graph cannot be written through the API by anyone -- entries are
written by :class:`holonbridge.audit.AuditMiddleware` alone.

The admin check is against ``animus.real_person``, like ``identity_admin``,
so an admin who is currently ``act_as`` someone else can still read it.

Turning auditing down (CHANGED 2026-09-30): ``POST /audit/level`` accepts only
``mutations``, ``standard`` or ``default``. A quieter setting is a time-boxed
``POST /audit/suspend`` (reason required, reverts by itself, start and end both
recorded) or an operator decision in the bridge's environment -- see
``holonbridge.audit``'s docstring for the reasoning. Every change through
these routes is recorded, whatever level it leaves audit at.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import audit
from .. import delegation as dg
from ..acl import is_admin
from ..deps import AnimusDep, ClientDep, ConnDep
from ..fuseki import FusekiError

router = APIRouter(prefix="/audit", tags=["audit"])

_OUTCOMES = ("allowed", "denied", "error")


async def _require_admin(conn, client, animus) -> None:
    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    if animus.real_person is None or not await is_admin(
        query_fn, conn.holons_graph, person=animus.real_person
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin role required on this dataset")


@router.get("")
async def list_entries(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    actor: str | None = Query(default=None, description="Person slug or IRI."),
    outcome: str | None = Query(default=None, description="allowed, denied or error."),
    since: datetime | None = Query(default=None, description="Only entries at or after this time."),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Newest first. An entry that touched several graphs is one item with a
    ``graphs`` list."""
    await _require_admin(conn, client, animus)

    filters = []
    try:
        if actor:
            iri = actor if ":" in actor else conn.person_iri(actor)
            filters.append(f"FILTER(?actor = <{dg.safe_iri(iri)}>)")
    except (dg.DelegationError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if outcome:
        if outcome not in _OUTCOMES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"outcome must be one of {list(_OUTCOMES)}"
            )
        filters.append(f'FILTER(?outcome = "{outcome}")')
    if since:
        filters.append(f'FILTER(?at >= "{dg._iso(since)}"^^<{audit.XSD}dateTime>)')

    query = f"""PREFIX hb: <{audit.HB}>
SELECT ?e ?at ?actor ?realActor ?outcome ?operation ?status ?graph ?by ?reason ?note ?security ?hash ?tokenKind ?agentCall ?agentEndpoint
WHERE {{ GRAPH <{audit.audit_graph(conn.holons_graph)}> {{
  ?e a hb:AuditEntry ; hb:at ?at ; hb:outcome ?outcome ; hb:operation ?operation ; hb:status ?status .
  OPTIONAL {{ ?e hb:actor ?actor }}
  OPTIONAL {{ ?e hb:realActor ?realActor }}
  OPTIONAL {{ ?e hb:targetGraph ?graph }}
  OPTIONAL {{ ?e hb:authorisedBy ?by }}
  OPTIONAL {{ ?e hb:authorisedReason ?reason }}
  OPTIONAL {{ ?e hb:reason ?note }}
  OPTIONAL {{ ?e hb:securityRead ?security }}
  OPTIONAL {{ ?e hb:payloadHash ?hash }}
  OPTIONAL {{ ?e hb:tokenKind ?tokenKind }}
  OPTIONAL {{ ?e hb:agentCall ?agentCall }}
  OPTIONAL {{ ?e hb:agentEndpoint ?agentEndpoint }}
  {' '.join(filters)}
}} }} ORDER BY DESC(?at) LIMIT {limit * 10}"""

    try:
        rows = (await client.select(conn, query)).get("results", {}).get("bindings", [])
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    entries: dict[str, dict] = {}
    for row in rows:
        iri = row["e"]["value"]
        item = entries.get(iri)
        if item is None:
            if len(entries) >= limit:
                continue
            item = entries[iri] = {
                "entry": iri,
                "at": row["at"]["value"],
                "outcome": row["outcome"]["value"],
                "operation": row["operation"]["value"],
                "status": int(row["status"]["value"]),
                "actor": row.get("actor", {}).get("value"),
                "real_actor": row.get("realActor", {}).get("value"),
                "authorised_by": row.get("by", {}).get("value"),
                "authorised_reason": row.get("reason", {}).get("value"),
                "note": row.get("note", {}).get("value"),
                "security_read": "security" in row,
                "payload_hash": row.get("hash", {}).get("value"),
                "token_kind": row.get("tokenKind", {}).get("value"),
                "agent_call": row.get("agentCall", {}).get("value"),
                "agent_endpoint": row.get("agentEndpoint", {}).get("value"),
                "graphs": [],
            }
        graph = row.get("graph", {}).get("value")
        if graph and graph not in item["graphs"]:
            item["graphs"].append(graph)

    listing = list(entries.values())
    return {"dataset": conn.dataset, "count": len(listing), "entries": listing}


@router.get("/level")
async def get_level(conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    """The effective level, and where it comes from: an active suspension,
    the API-set level, or the operator's."""
    await _require_admin(conn, client, animus)
    return {"dataset": conn.dataset, **await audit.describe(client, conn)}


class SetLevelRequest(BaseModel):
    level: str


@router.post("/level")
async def set_level(
    body: SetLevelRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Set the API-level override: ``mutations`` or ``standard``, or
    ``default`` to clear it and fall back to the operator's level. ``off``
    and ``denials`` are refused here -- see the module docstring."""
    await _require_admin(conn, client, animus)
    previous = await audit.level_for(client, conn)
    audit.annotate(reason=f"audit level {previous} -> {body.level}")
    try:
        await audit.set_level(client, conn, body.level)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "dataset": conn.dataset, **await audit.describe(client, conn)}


class SuspendRequest(BaseModel):
    level: str = Field(description="'denials' or 'off'.")
    minutes: int = Field(description="1 to the maximum reported by GET /audit/level.")
    reason: str = Field(description="Why. Mandatory, and recorded.")


@router.post("/suspend")
async def suspend(
    body: SuspendRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Lower auditing to ``denials`` or ``off`` for a limited time. It
    reverts on its own; the start (with the reason) and the end are both
    recorded. Replaces a suspension already running."""
    await _require_admin(conn, client, animus)
    audit.annotate(
        reason=f"audit suspended to {body.level} for {body.minutes} min: {body.reason.strip()[:200]}"
    )
    try:
        until = await audit.suspend(
            client, conn, level=body.level, minutes=body.minutes,
            reason=body.reason, by=animus.real_person,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "dataset": conn.dataset, "until": dg._iso(until),
            **await audit.describe(client, conn)}


@router.post("/resume")
async def resume(conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    """End a running suspension early."""
    await _require_admin(conn, client, animus)
    try:
        ended = await audit.resume(client, conn)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    audit.annotate(
        reason=(
            f"audit suspension to {ended.suspended_to} ended early"
            if ended else "no suspension was running"
        )
    )
    return {"ok": True, "dataset": conn.dataset, "was_suspended": ended is not None,
            **await audit.describe(client, conn)}
