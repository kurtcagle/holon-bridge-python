"""Holon lifecycle routes: change a holon's state, and read it.

See :mod:`holonbridge.lifecycle` for the model. A holon is Active, Inactive or
Tombstoned; an inactive holon can be read but its fluents cannot change; all
transitions are reversible.

Access
------

``POST /holon/lifecycle`` writes the events ledger, the scene graph, the
sequences graph and the holons graph (the lifecycle fluent is linked from the
holon the first time), so the caller needs a write grant on each, as for
``POST /fluent/update``. The first use in a dataset also declares the lifecycle
property in the ontology graph, which needs a grant there too; it is asked for
only when the declaration is missing. Admins pass, as everywhere. The model is
graph-level: there is no per-holon permission.

The change is attributed to the authenticated person, never to a name in the
request. There is no ``asserted_by`` field; an admin who needs to act for
someone else uses ``act_as``, as for any other write.

``GET /holon/lifecycle`` needs an identity but no grant, like the other
registries, and reads ground truth.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import audit
from .. import delegation as dg
from .. import lifecycle as lc
from .. import succession as sc
from ..acl import check_write
from ..deps import AnimusDep, ClientDep, ConnDep, SettingsDep
from ..fuseki import FusekiError
from ..sequence import SequenceError

router = APIRouter(prefix="/holon", tags=["lifecycle"])


class SetLifecycleRequest(BaseModel):
    holon: str = Field(..., min_length=1, description="IRI of the holon")
    state: Literal["Active", "Inactive", "Tombstoned"] = Field(
        ...,
        description=(
            "Active: the holon accepts change. Inactive: it can be read and queried but "
            "its fluents cannot change. Tombstoned: inactive, and left out of discovery "
            "(it can still be read by IRI). Any state may follow any other."
        ),
    )
    reason: str | None = Field(
        default=None, description="Recorded on the ledger entry, e.g. 'season over'."
    )
    effective_at: datetime | None = Field(
        default=None,
        description=(
            "Valid time: when the holon entered this state (ISO 8601). Defaults to now. "
            "A timestamp with no timezone is read as UTC."
        ),
    )


class SucceedRequest(BaseModel):
    holon: str = Field(..., min_length=1, description="IRI of the holon that ends")
    successor: str = Field(
        ..., min_length=1, description="IRI of the new holon; it must not exist yet"
    )
    reason: str | None = Field(
        default=None, description="Recorded on the predecessor's ledger entry."
    )
    label: str | None = Field(
        default=None,
        description="rdfs:label for the successor. Defaults to the predecessor's own labels.",
    )
    tombstone: bool = Field(
        default=False,
        description="Tombstone the predecessor (hidden from discovery) instead of making it Inactive.",
    )
    effective_at: datetime | None = Field(
        default=None,
        description="Valid time of the change (ISO 8601). Defaults to now; no timezone means UTC.",
    )


def _safe(value: str, what: str) -> str:
    try:
        return dg.safe_iri(value)
    except dg.DelegationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{what}: {exc}") from exc


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@router.post("/lifecycle")
async def set_lifecycle(
    body: SetLifecycleRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    settings: SettingsDep,
) -> dict:
    """Make a holon Active, Inactive or Tombstoned."""
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    person = animus.person
    holon = _safe(body.holon, "holon")

    graphs = [
        conn.graph("events"),
        conn.scene_graph,
        conn.graph("sequences"),
        conn.holons_graph,
    ]
    try:
        if not await lc.vocabulary_declared(client, conn):
            graphs.append(conn.ontology_graph)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

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

    try:
        change = await lc.set_state(
            client,
            conn,
            holon=holon,
            state=body.state,
            asserted_by=person,
            reason=body.reason,
            asserted_datetime=_as_utc(body.effective_at),
            gate_scope=settings.fluent_gate_scope,
        )
    except lc.LifecycleError as exc:
        raise HTTPException(exc.status, detail={"error": exc.code, "message": str(exc)}) from exc
    except SequenceError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "sequence_contention", "message": str(exc)}
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {**change.as_dict(), "assertedBy": person}


@router.post("/succeed")
async def succeed(
    body: SucceedRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    settings: SettingsDep,
) -> dict:
    """Close a holon and open a successor that carries its final state forward.

    The predecessor becomes Inactive (Tombstoned with ``tombstone``) and is linked to the
    successor with ``hb:succeededBy``. The successor gets the predecessor's structure
    and its own copy of every fluent, seeded with the predecessor's final value. A
    holon with a successor cannot be made Active again. See :mod:`holonbridge.succession`
    for what is and is not carried over.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    person = animus.person
    holon = _safe(body.holon, "holon")
    successor = _safe(body.successor, "successor")

    graphs = [
        conn.graph("events"),
        conn.scene_graph,
        conn.graph("sequences"),
        conn.holons_graph,
    ]
    try:
        if not await lc.vocabulary_declared(client, conn):
            graphs.append(conn.ontology_graph)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

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

    try:
        result = await sc.succeed(
            client,
            conn,
            holon=holon,
            successor=successor,
            asserted_by=person,
            reason=body.reason,
            label=body.label,
            tombstone=body.tombstone,
            asserted_datetime=_as_utc(body.effective_at),
            gate_scope=settings.fluent_gate_scope,
        )
    except lc.LifecycleError as exc:
        raise HTTPException(exc.status, detail={"error": exc.code, "message": str(exc)}) from exc
    except SequenceError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "sequence_contention", "message": str(exc)}
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {**result.as_dict(), "assertedBy": person}


@router.get("/lifecycle")
async def get_lifecycle(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    iri: str = Query(..., description="holon IRI"),
) -> dict:
    """A holon's current lifecycle state. A holon that has never changed state is Active."""
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    holon = _safe(iri, "iri")
    try:
        if not await lc.holon_exists(client, conn, holon):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "unknown_holon",
                    "message": f"<{holon}> is not a holon in this dataset",
                },
            )
        state = await lc.read_state(client, conn, holon)
        successor, predecessor = await lc.read_succession(client, conn, holon)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return replace(state, successor=successor, predecessor=predecessor).as_dict()
