"""Per-fluent write grants: create, revoke, list. Admin only.

See :mod:`holonbridge.fluent_grants` for what a grant is and why. The admin
check uses the REAL caller (``animus.real_person``), as ``identity_admin`` does,
so ``act_as`` cannot be used to mint grants. Writes go to the holons graph
through ``client.post_graph`` after the checks, like the delegation routes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .. import fluent_grants as fg
from ..acl import is_admin
from ..deps import AnimusDep, ClientDep, ConnDep
from ..fuseki import FusekiError

router = APIRouter(prefix="/fluent-grant", tags=["fluent-grant"])


async def _admin(conn, client, animus) -> None:
    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    if not await is_admin(query_fn, conn.holons_graph, person=animus.real_person):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin role required on this dataset")


class CreateRequest(BaseModel):
    grantee: str = Field(..., min_length=1, description="Person: slug or full IRI.")
    fluents: list[str] = Field(default_factory=list, description="Exact fluent IRIs.")
    prefixes: list[str] = Field(default_factory=list, description="IRI prefixes, e.g. 'urn:proc:run-a-campaign:campaign:spring:'.")
    lifetime_days: float | None = Field(default=None, gt=0, description="Optional; omit for no expiry.")
    note: str | None = None


@router.post("")
async def create(body: CreateRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    await _admin(conn, client, animus)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    grantee = body.grantee if body.grantee.startswith("http") or ":" in body.grantee else conn.person_iri(body.grantee)
    try:
        grantee = fg.safe_iri(grantee)
        fluents, prefixes = fg.validate(fluents=body.fluents, prefixes=body.prefixes)
        res = await query_fn(
            f"ASK {{ GRAPH <{conn.holons_graph}> {{ <{grantee}> a <{fg.HOLON}Person> }} }}"
        )
        if not res.get("boolean"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no holon:Person found: {grantee}")
        now = datetime.now(timezone.utc)
        until = now + timedelta(days=body.lifetime_days) if body.lifetime_days else None
        iri = fg.grant_iri(conn.holons_graph, fg.new_slug())
        turtle = fg.build_grant(
            iri=iri, grantee=grantee, granted_by=animus.real_person, fluents=fluents,
            prefixes=prefixes, granted_at=now, valid_until=until, note=body.note,
        )
        await client.post_graph(conn, conn.holons_graph, turtle)
    except fg.DelegationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "grant": iri, "grantee": grantee, "fluents": fluents, "prefixes": prefixes,
            "valid_until": fg._iso(until) if until else None}


class RevokeRequest(BaseModel):
    grant: str = Field(..., min_length=1)


@router.post("/revoke")
async def revoke(body: RevokeRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    await _admin(conn, client, animus)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    try:
        iri = fg.safe_iri(body.grant)
        if not [g for g in await fg.list_grants(query_fn, conn.holons_graph) if g["grant"] == iri]:
            return {"ok": False, "grant": iri, "note": "not_found: no such fluent grant"}
        await client.update(conn, fg.revoke_update(conn.holons_graph, iri, datetime.now(timezone.utc)))
    except fg.DelegationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "grant": iri, "revoked": True}


@router.get("")
async def list_(conn: ConnDep, client: ClientDep, animus: AnimusDep, grantee: str | None = None) -> dict:
    await _admin(conn, client, animus)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    who = None
    if grantee:
        who = grantee if grantee.startswith("http") or ":" in grantee else conn.person_iri(grantee)
        try:
            who = fg.safe_iri(who)
        except fg.DelegationError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    rows = await fg.list_grants(query_fn, conn.holons_graph, grantee=who)
    return {"grants": [{**g, "fluents": sorted(g["fluents"]), "prefixes": sorted(g["prefixes"])} for g in rows]}
