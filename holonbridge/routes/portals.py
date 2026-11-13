"""Portal routes: register, list, change status, delete, and cross.

A portal (:mod:`holonbridge.portal`) is a door between two holons. These routes
are the HTTP side of it.

Access
------

* **Registering, changing status:** ``check_write`` on the portals graph.
  Registering also needs the caller to be able to *read* what the lock and
  effect queries read (the bridge evaluates them later, on callers' behalf, so
  a portal must not be a way to read what the person who made it could not).
* **Deleting:** ``check_replace`` on the portals graph, as for a named query.
  Deleting a portal frees its position property: fluents of that property may
  again be changed directly.
* **Crossing:** the same three write grants as ``POST /fluent/update`` (events,
  scene, sequences), plus read access to the lock and effect queries. Who may
  cross is otherwise a matter for the lock query, which receives the traveller as
  ``focus``. The ledger attributes the crossing to the caller, never to a name in
  the request.

Reading (``GET``) needs an identity but no grant, like the other registries.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .. import audit
from .. import delegation as dg
from ..acl import check_write
from ..deps import AnimusDep, ClientDep, ConnDep, SettingsDep
from ..fluent_trigger_action import FluentActionInvalid, bind_action
from ..fuseki import FusekiError
from ..named_queries import load_named_queries
from ..portal import (
    PORTAL_STATUSES,
    Portal,
    PortalError,
    deletion_update,
    load_portals,
    portal_node,
    registration_update,
    render_portal_turtle,
    status_update,
    traverse,
)
from ..sequence import SequenceError
from ..trigger_authz import PROBE_FOCUS, TriggerDenied, authorize_reads, require_grant

router = APIRouter(tags=["portals"])


class RegisterPortalRequest(BaseModel):
    id: str = Field(..., min_length=1, description="short name; letters, digits, _ . -")
    from_holon: str = Field(..., alias="from", description="IRI of the holon a traveller leaves")
    to_holon: str = Field(..., alias="to", description="IRI of the holon it arrives in")
    position_property: str = Field(
        ...,
        description="IRI of the holon:FluentProperty whose fluent says where a traveller is. "
        "Once this portal exists, fluents of this property change only by crossing a portal.",
    )
    lock: str | None = Field(
        default=None,
        description="id of a named SELECT, bound to the traveller as 'focus'. Open when it returns "
        "a row (and, if the row has ?open or ?allowed, that is not false). A ?reason column is "
        "passed back to the caller when the door is shut.",
    )
    effect: str | None = Field(
        default=None,
        description="id of a named SELECT, bound to the traveller, whose rows (?fluent ?value "
        "[?operation ?isIri ?assertedAt ?description]) are applied in the same transaction as the move.",
    )
    boundary_shapes: str | None = Field(
        default=None,
        description="IRI of a graph of SHACL shapes the traveller's payload must satisfy to cross.",
    )
    status: str = Field(default="Active", pattern="^(Active|Suspended)$")
    label: str | None = None
    description: str | None = None

    model_config = {"populate_by_name": True}


class StatusRequest(BaseModel):
    status: str = Field(..., pattern="^(Active|Suspended)$")


class TraverseRequest(BaseModel):
    traveller: str = Field(..., min_length=1, description="IRI of the holon crossing")
    payload: str | None = Field(
        default=None,
        description="Turtle describing the traveller, checked against the portal's boundary shapes "
        "(required if it has any).",
    )
    asserted_datetime: datetime | None = Field(
        default=None, description="Valid time of the crossing; defaults to now. See /fluent/update."
    )
    description: str | None = None
    asserted_by: str | None = Field(
        default=None, description="Ignored unless it names the authenticated person."
    )


# --- helpers -------------------------------------------------------------------


def _identified(animus) -> str:
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    return animus.person


def _query_fn(client, conn):
    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    return query_fn


def _denied(exc: TriggerDenied) -> HTTPException:
    if exc.status in (401, 403):
        audit.annotate(reason=exc.reason)
    return HTTPException(exc.status, detail=exc.detail)


def _portal_error(exc: PortalError) -> HTTPException:
    if exc.status in (401, 403):
        audit.annotate(reason=exc.message)
    return HTTPException(exc.status, detail=exc.as_dict())


def _safe(value: str, what: str) -> str:
    try:
        return dg.safe_iri(value)
    except dg.DelegationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{what}: {exc}") from exc


def _not_found(portal_id: str, available: list[str]) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"error": "unknown_portal", "id": portal_id, "available": sorted(available)},
    )


async def _authorize_query_reads(conn, client, person: str, queries, *, label: str, ident: str | None) -> list[str]:
    """The caller must be able to read what the named query reads. Returns warnings."""
    if not ident:
        return []
    named = queries.by_id(ident)
    if named is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": f"unknown_{label}", "id": ident, "available": sorted(q.id for q in queries.queries)},
        )
    try:
        sparql = bind_action(named, PROBE_FOCUS)
    except FluentActionInvalid as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail={"error": f"{label}_not_bindable", "message": str(exc)}
        ) from exc
    try:
        await authorize_reads(_query_fn(client, conn), conn, person, label=label, ident=ident, sparql=sparql)
    except TriggerDenied as exc:
        raise _denied(exc) from exc
    warnings = []
    if "focus" not in named.declared:
        warnings.append(
            f"{label} query {ident!r} declares no 'focus' parameter, so it is not bound to the "
            "traveller and gives the same answer for everyone"
        )
    return warnings


# --- registry ------------------------------------------------------------------


@router.get("/portals")
async def list_portals(
    conn: ConnDep, client: ClientDep, animus: AnimusDep, portal_status: str | None = None
) -> dict:
    _identified(animus)
    result = await load_portals(client, conn)
    portals = [p for p in result.portals if portal_status is None or p.status == portal_status]
    return {
        "dataset": conn.dataset,
        "graph": conn.graph("portals"),
        "count": len(portals),
        "statuses": list(PORTAL_STATUSES),
        "portals": [p.as_dict() for p in portals],
        "warnings": result.warnings,
    }


@router.get("/portal/{portal_id}")
async def get_portal(portal_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    _identified(animus)
    result = await load_portals(client, conn)
    portal = result.by_id(portal_id)
    if portal is None:
        raise _not_found(portal_id, [p.id for p in result.portals])
    return portal.as_dict()


@router.post("/portal")
async def register_portal(
    body: RegisterPortalRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    person = _identified(animus)
    registry = conn.graph("portals")
    try:
        await require_grant(_query_fn(client, conn), conn, person, registry)
    except TriggerDenied as exc:
        raise _denied(exc) from exc

    queries = await load_named_queries(client, conn)
    warnings = await _authorize_query_reads(conn, client, person, queries, label="lock", ident=body.lock)
    warnings += await _authorize_query_reads(conn, client, person, queries, label="effect", ident=body.effect)

    node = portal_node(conn, body.id)
    try:
        turtle = render_portal_turtle(
            node=node,
            portal_id=body.id,
            from_holon=body.from_holon,
            to_holon=body.to_holon,
            position_property=body.position_property,
            lock=body.lock,
            effect=body.effect,
            boundary_shapes=body.boundary_shapes,
            status=body.status,
            label=body.label,
            description=body.description,
            registered_by=person,
            registered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except PortalError as exc:
        raise _portal_error(exc) from exc

    audit.annotate(graphs=[registry])
    try:
        await client.update(conn, registration_update(registry, node, turtle))
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"ok": True, "id": body.id, "iri": node, "graph": registry, "status": body.status, "warnings": warnings}


@router.post("/portal/{portal_id}/status")
async def set_portal_status(
    portal_id: str, body: StatusRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    person = _identified(animus)
    registry = conn.graph("portals")
    try:
        await require_grant(_query_fn(client, conn), conn, person, registry)
    except TriggerDenied as exc:
        raise _denied(exc) from exc

    result = await load_portals(client, conn)
    portal = result.by_id(portal_id)
    if portal is None:
        raise _not_found(portal_id, [p.id for p in result.portals])

    audit.annotate(graphs=[registry])
    try:
        await client.update(conn, status_update(registry, portal.iri, body.status))
    except PortalError as exc:
        raise _portal_error(exc) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "id": portal_id, "status": body.status}


@router.delete("/portal/{portal_id}")
async def delete_portal(portal_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    """Remove a portal. Fluents of its position property become directly writable
    again (unless another portal governs the same property). Past crossings stay in
    the ledger."""
    person = _identified(animus)
    registry = conn.graph("portals")
    try:
        await require_grant(_query_fn(client, conn), conn, person, registry, replace=True)
    except TriggerDenied as exc:
        raise _denied(exc) from exc

    result = await load_portals(client, conn)
    portal = result.by_id(portal_id)
    if portal is None:
        raise _not_found(portal_id, [p.id for p in result.portals])

    audit.annotate(graphs=[registry])
    try:
        await client.update(conn, deletion_update(registry, portal.iri))
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "id": portal_id, "graph": registry}


# --- crossing ------------------------------------------------------------------


@router.post("/portal/{portal_id}/traverse")
async def traverse_portal(
    portal_id: str,
    body: TraverseRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    settings: SettingsDep,
) -> dict:
    """Cross a portal. The order of checks, and why, is in :mod:`holonbridge.portal`."""
    person = _identified(animus)
    traveller = _safe(body.traveller, "traveller")

    if body.asserted_by is not None and body.asserted_by != person:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="asserted_by is set from your authenticated identity; omit it, or name yourself.",
        )

    result = await load_portals(client, conn)
    portal: Portal | None = result.by_id(portal_id)
    if portal is None:
        raise _not_found(portal_id, [p.id for p in result.portals])

    graphs = [conn.graph("events"), conn.scene_graph, conn.graph("sequences")]
    digest, size = audit.payload_digest(json.dumps(body.model_dump(mode="json"), sort_keys=True))
    audit.annotate(graphs=graphs, payload_hash=digest, payload_bytes=size)

    query_fn = _query_fn(client, conn)
    for graph in graphs:
        decision = await check_write(query_fn, conn.holons_graph, person=person, target=graph)
        audit.annotate(authorised_by=decision.matched_grant, authorised_reason=decision.reason)
        if not decision.allowed:
            detail = f"{decision.reason} (graph: {graph})"
            audit.annotate(reason=detail)
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=detail)

    queries = await load_named_queries(client, conn)
    await _authorize_query_reads(conn, client, person, queries, label="lock", ident=portal.lock or None)
    await _authorize_query_reads(conn, client, person, queries, label="effect", ident=portal.effect or None)

    try:
        crossing = await traverse(
            client,
            conn,
            portal,
            traveller=traveller,
            asserted_by=person,
            payload_turtle=body.payload,
            asserted_datetime=_as_utc(body.asserted_datetime),
            description=body.description,
            gate_scope=settings.fluent_gate_scope,
        )
    except PortalError as exc:
        raise _portal_error(exc) from exc
    except SequenceError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "sequence_contention", "message": str(exc)}
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "portal": portal.id,
        "traveller": traveller,
        "from": portal.from_holon,
        "to": portal.to_holon,
        "transaction": crossing.transaction.transaction,
        "effectsApplied": crossing.effects_applied,
        "assertedBy": person,
        "updates": [
            {
                "fluent": r.fluent,
                "operation": r.operation.value,
                "oldValue": None if r.old_value is None else r.old_value.lexical,
                "newValue": r.new_value.lexical,
                "assertion": r.assertion_iri,
            }
            for r in crossing.transaction.results
        ],
    }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
