"""Fluent-update routes.

CHANGED 2026-09-30: both routes now require an identity, and the update route
enforces write access. Before this, ``POST /fluent/update`` depended only on
the connection: anyone holding the shared bearer token could write a fluent's
new value into the scene graph and append to the events ledger, with no
grant, no identity, and a free-text ``asserted_by`` that the ledger then
recorded as fact. It also interpolated ``fluent``, ``asserted_by`` and an
IRI-valued ``value`` into SPARQL unescaped.

- **Identity.** ``AnimusDep`` on both routes. A missing or unresolvable
  identity is a 401 like every other gated route.
- **Authorisation.** One update writes *three* graphs -- the events ledger,
  the scene graph, and (through ``sequence.mint``) the sequences graph -- so
  the caller needs an explicit ``holon:grantsWrite`` on each, checked with
  ``acl.check_write`` before anything runs. Admins pass, as everywhere. The
  model is graph-level: there is no per-fluent permission.
- **Attribution.** ``asserted_by`` is set from the authenticated person, not
  taken from the request. A request that names a *different* person is refused
  (400) rather than silently overridden, so a spoof attempt is visible. To
  assert as someone else, an admin uses ``act_as``, which the audit log records
  with both identities.
- **Injection.** ``fluent``, an IRI-valued ``value``, and the path parameter of
  the ``prior`` route are validated with ``delegation.safe_iri`` before they
  reach any generated SPARQL. Authorising the request cannot protect against
  text that changes the *shape* of the statement that is then run, so this is
  a separate check, and it applies to the read route too (an unescaped path
  parameter there could otherwise reach graphs the ACL never looked at).

The audit middleware records a fluent update like any mutation; the graphs,
authority and a payload digest are attached here.

CHANGED 2026-09-30 (sequence contention): ``sequence.mint`` raises
``SequenceError`` when it runs out of retries under contention, and this route
did not catch it, so a caller who lost that many races got a bare 500 with no
explanation. ``POST /sequence/mint`` has always mapped the same error to a 409.
This route now does too, with the same ``error``/``message`` body the other
fluent refusals use. Nothing of the transition was written (an attempt that
loses writes neither half of it), so the caller can simply retry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .. import audit
from .. import delegation as dg
from ..acl import check_write
from ..deps import AnimusDep, ClientDep, ConnDep, SettingsDep
from ..fluent import FluentError, FluentUpdateResult, Operation, TypedValue, prior_value, update_fluent
from ..fluent_transaction import MAX_OPS, FluentOp, update_fluents
from ..fuseki import FusekiError
from ..portal import governing_portals, portal_governed_message
from ..sequence import SequenceError

router = APIRouter(prefix="/fluent", tags=["fluent"])


class UpdateFluentRequest(BaseModel):
    fluent: str = Field(..., min_length=1, description="IRI of the holon:Fluent to update")
    operation: Operation
    value: Any = None
    is_iri: bool = False
    asserted_by: str | None = Field(
        default=None,
        description=(
            "Ignored unless it names the authenticated person. The ledger "
            "attributes every transition to the caller; this field exists only "
            "so older clients that send their own IRI keep working."
        ),
    )
    description: str | None = None
    asserted_datetime: datetime | None = Field(
        default=None,
        description=(
            "Valid time: when this was true in the world (ISO 8601). Defaults "
            "to now. Backdate it to record a fact that was true earlier than "
            "it was learnt; the bridge separately records when it was "
            "inserted (hevt:insertedAt), which this field cannot change. A "
            "timestamp with no timezone is read as UTC."
        ),
    )


def _result_dict(result: FluentUpdateResult) -> dict:
    return {
        "ok": True,
        "fluent": result.fluent,
        "operation": result.operation.value,
        "oldValue": (
            {"kind": result.old_value.kind, "value": result.old_value.lexical}
            if result.old_value
            else None
        ),
        "newValue": {"kind": result.new_value.kind, "value": result.new_value.lexical},
        "assertion": result.assertion_iri,
        "superseded": result.superseded,
        "sequenceId": result.sequence_id,
    }


def _as_utc(value: datetime | None) -> datetime | None:
    # A naive timestamp is read as UTC: stored assertedDateTime values are
    # timezone-aware, and SPARQL cannot reliably order a naive dateTime
    # against an aware one (see holon.py's as-of note).
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _safe(value: str, what: str) -> str:
    try:
        return dg.safe_iri(value)
    except dg.DelegationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{what}: {exc}") from exc


@router.post("/update")
async def update(
    body: UpdateFluentRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    settings: SettingsDep,
) -> dict:
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    person = animus.person

    fluent = _safe(body.fluent, "fluent")
    value = body.value
    if body.is_iri:
        if not isinstance(value, str):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="value: an IRI-valued update needs a string value"
            )
        value = _safe(value, "value")

    if body.asserted_by is not None and body.asserted_by != person:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "asserted_by is set from your authenticated identity; omit it, or name "
                "yourself. To assert as someone else, an admin uses act_as."
            ),
        )

    graphs = [conn.graph("events"), conn.scene_graph, conn.graph("sequences")]
    digest, size = audit.payload_digest(json.dumps(body.model_dump(mode="json"), sort_keys=True))
    audit.annotate(graphs=graphs, payload_hash=digest, payload_bytes=size)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    for graph in graphs:
        decision = await check_write(query_fn, conn.holons_graph, person=person, target=graph)
        audit.annotate(authorised_by=decision.matched_grant, authorised_reason=decision.reason)
        if not decision.allowed:
            detail = f"{decision.reason} (graph: {graph})"
            audit.annotate(reason=detail)
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=detail)

    governed = await governing_portals(client, conn, fluent)
    if governed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "portal_governed",
                "message": portal_governed_message(fluent, governed),
                "portals": governed,
            },
        )

    try:
        result = await update_fluent(
            client,
            conn,
            fluent=fluent,
            operation=body.operation,
            value=value,
            is_iri=body.is_iri,
            asserted_by=person,
            description=body.description,
            asserted_datetime=_as_utc(body.asserted_datetime),
            gate_scope=settings.fluent_gate_scope,
        )
    except FluentError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "fluent_error", "message": str(exc)},
        ) from exc
    except SequenceError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error": "sequence_contention", "message": str(exc)},
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {**_result_dict(result), "assertedBy": person}


class FluentOperationItem(BaseModel):
    fluent: str = Field(..., min_length=1)
    operation: Operation
    value: Any = None
    is_iri: bool = False
    description: str | None = None


class UpdateFluentsRequest(BaseModel):
    operations: list[FluentOperationItem] = Field(
        ...,
        min_length=1,
        max_length=MAX_OPS,
        description=(
            "Changes to apply together: all of them or none. One per fluent. Each is "
            "as for /fluent/update; they share the asserted_datetime and description below."
        ),
    )
    asserted_datetime: datetime | None = Field(
        default=None, description="Valid time for every member, as for /fluent/update."
    )
    description: str | None = Field(default=None, description="Recorded on the transaction and, unless a member has its own, on each entry.")


@router.post("/update-many")
async def update_many(
    body: UpdateFluentsRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    settings: SettingsDep,
) -> dict:
    """Several fluent changes as one transaction: every one is applied, or none is.

    Needs the same three write grants as ``/fluent/update``. A fluent that is the
    position property of a portal cannot be changed here (cross the portal)."""
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    person = animus.person

    ops: list[FluentOp] = []
    for item in body.operations:
        value = item.value
        if item.is_iri:
            if not isinstance(value, str):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, detail="value: an IRI-valued update needs a string value"
                )
            value = _safe(value, "value")
        ops.append(
            FluentOp(
                fluent=_safe(item.fluent, "fluent"),
                operation=item.operation,
                value=value,
                is_iri=item.is_iri,
                description=item.description,
            )
        )

    graphs = [conn.graph("events"), conn.scene_graph, conn.graph("sequences")]
    digest, size = audit.payload_digest(json.dumps(body.model_dump(mode="json"), sort_keys=True))
    audit.annotate(graphs=graphs, payload_hash=digest, payload_bytes=size)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    for graph in graphs:
        decision = await check_write(query_fn, conn.holons_graph, person=person, target=graph)
        audit.annotate(authorised_by=decision.matched_grant, authorised_reason=decision.reason)
        if not decision.allowed:
            detail = f"{decision.reason} (graph: {graph})"
            audit.annotate(reason=detail)
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=detail)

    for op in ops:
        governed = await governing_portals(client, conn, op.fluent)
        if governed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "portal_governed",
                    "message": portal_governed_message(op.fluent, governed),
                    "portals": governed,
                },
            )

    try:
        result = await update_fluents(
            client,
            conn,
            ops=ops,
            asserted_by=person,
            description=body.description,
            asserted_datetime=_as_utc(body.asserted_datetime),
            gate_scope=settings.fluent_gate_scope,
        )
    except FluentError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail={"error": "fluent_error", "message": str(exc)}
        ) from exc
    except SequenceError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "sequence_contention", "message": str(exc)}
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "transaction": result.transaction,
        "assertedBy": person,
        "updates": [_result_dict(r) for r in result.results],
    }


@router.get("/{fluent_id:path}/prior")
async def get_prior_value(
    fluent_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """The value this fluent held immediately before its current one --
    what a caller building a 'revert' Set would read first.

    Reads org-level graphs (scene and events), which any identified caller may
    read, as with ``/sparql/select``. The path parameter is validated before it
    reaches the query."""
    fluent = _safe(fluent_id, "fluent")
    try:
        value = await prior_value(client, conn, fluent=fluent)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    if value is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "no_prior_value", "fluent": fluent},
        )
    return {"fluent": fluent, "priorValue": {"kind": value.kind, "value": value.lexical}}
