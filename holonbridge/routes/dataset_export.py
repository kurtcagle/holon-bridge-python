"""``POST /admin/dataset/export``: a whole dataset as one zipped TriG file, and a way to fetch it.

Where ``/admin/dataset/snapshot`` keeps a checkpoint on the bridge's own disk for a restore
into the same host, an export is a portable file: ``{id}.trig.zip``, loadable by any triple
store, and retrievable through the bridge on a link that expires. The rules are in
:mod:`holonbridge.export`. The gates:

* **export, list and mint a link** need *admin on the dataset*: the file holds every graph in
  it (identity, delegation and audit graphs included), so the right to make it is the right to
  read across the whole dataset, the same as a snapshot. ``DATASET_CREATORS`` does not imply it.
* **download** needs no bearer token: the signed, expiring link *is* the authority, and it
  names one file. That is what lets ``curl`` or a browser on another machine fetch it. A link
  that fails (bad signature, expired, links off) is one 403 and, deliberately, is not audited:
  the dataset named in the URL is attacker-chosen until the signature checks out, and
  recording against it before then would let anyone with the URL shape write audit entries
  into any dataset. A *valid* download is audited as a security read.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import audit
from ..acl import is_admin
from ..conn import resolve_conn
from ..deps import AnimusDep, BanksDep, ClientDep, ConnDep, SettingsDep
from ..export import (
    MAX_LINK_TTL,
    MIN_LINK_TTL,
    list_exports,
    mint_link,
    take_export,
    verify_link,
)
from ..snapshot import SnapshotError

router = APIRouter(prefix="/admin/dataset", tags=["admin"])


class ExportRequest(BaseModel):
    linkTtlSeconds: int | None = Field(
        default=None,
        ge=MIN_LINK_TTL,
        le=MAX_LINK_TTL,
        description="How long the download link lives, in seconds. Default: EXPORT_LINK_TTL (15 minutes).",
    )


class LinkRequest(BaseModel):
    ttlSeconds: int | None = Field(default=None, ge=MIN_LINK_TTL, le=MAX_LINK_TTL)


async def _require_admin(animus, conn, client) -> None:
    async def query_fn(query: str) -> dict:
        return await client.select(conn, query)

    if not await is_admin(query_fn, conn.holons_graph, person=animus.person):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin role required")


def _link_or_none(settings, dataset: str, export_id: str, ttl: int | None) -> dict:
    """A link if links are configured; otherwise a note saying why there is none."""
    try:
        return mint_link(settings, dataset, export_id, ttl=ttl)
    except SnapshotError as exc:
        if exc.code == "export_links_off":
            return {"linkError": exc.detail()}
        raise


@router.post("/export")
async def export_dataset(
    animus: AnimusDep,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    body: ExportRequest | None = None,
) -> dict:
    """Write a zipped-TriG export of the active dataset under ``SNAPSHOT_DIR``.

    Admin on the dataset only. Keeps the newest ``SNAPSHOT_KEEP`` exports and drops any older
    than ``SNAPSHOT_MAX_AGE_DAYS`` (never the newest). Refuses an empty dataset (409
    ``nothing_to_export``) and one over ``EXPORT_MAX_BYTES`` (413). 503 when snapshots are
    not configured. If ``EXPORT_SIGNING_KEY`` is set the response carries an expiring
    download link; otherwise ``linkError`` says links are off and the file stays on the host.
    """
    await _require_admin(animus, conn, client)
    try:
        done = await take_export(client, conn, settings, by=animus.person)
    except SnapshotError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    audit.annotate(
        authorised_by=animus.person,
        authorised_reason=f"export {done.id} of {done.dataset!r} written ({done.graphs} graphs)",
    )
    result = {
        "ok": True,
        "dataset": done.dataset,
        "export": done.id,
        "kept": done.kept,
        "graphs": done.graphs,
        "statements": done.statements,
        "uncompressedBytes": done.uncompressed_bytes,
        "zipBytes": done.bytes,
        "sha256": done.sha256,
        "createdAt": done.created_at,
    }
    result.update(_link_or_none(settings, done.dataset, done.id, body.linkTtlSeconds if body else None))
    return result


@router.get("/exports")
async def exports(animus: AnimusDep, conn: ConnDep, client: ClientDep, settings: SettingsDep) -> dict:
    """List the active dataset's exports, newest first. Admin on the dataset; metadata only."""
    await _require_admin(animus, conn, client)
    try:
        found = list_exports(settings, conn.dataset)
    except SnapshotError as exc:
        raise HTTPException(exc.status, detail=exc.detail()) from exc
    return {
        "dataset": conn.dataset,
        "count": len(found),
        "keep": settings.snapshot_keep,
        "maxAgeDays": settings.snapshot_max_age_days,
        "linksEnabled": bool(settings.export_signing_key and len(settings.export_signing_key) >= 32),
        "exports": found,
    }


@router.post("/export/{export_id}/link")
async def export_link(
    export_id: str,
    animus: AnimusDep,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    body: LinkRequest | None = None,
) -> dict:
    """A fresh expiring download link for an existing export of the active dataset. Admin only.

    503 ``export_links_off`` without ``EXPORT_SIGNING_KEY``; 404 ``no_export``; 400
    ``bad_export_id``. Every link is audited as it is made.
    """
    await _require_admin(animus, conn, client)
    try:
        link = mint_link(settings, conn.dataset, export_id, ttl=body.ttlSeconds if body else None)
    except SnapshotError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc
    audit.annotate(
        authorised_by=animus.person,
        authorised_reason=(
            f"download link for export {export_id} of {conn.dataset!r} made, "
            f"expires {link['expiresAt']}"
        ),
    )
    return {"ok": True, "dataset": conn.dataset, "export": export_id, **link}


@router.get("/export/{dataset}/{export_id}/download")
async def download_export(
    dataset: str,
    export_id: str,
    request: Request,
    settings: SettingsDep,
    banks: BanksDep,
    exp: int = Query(...),
    sig: str = Query(...),
) -> FileResponse:
    """Fetch an export on a signed, expiring link. No bearer token: the link is the authority.

    One 403 for every kind of bad link (see the module docstring). 404 ``no_export`` if the
    export was pruned after the link was made.
    """
    try:
        path = verify_link(settings, dataset, export_id, exp, sig)
    except SnapshotError as exc:
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    # The signature checked out, so the dataset name is the one the bridge signed for.
    # Only now is it bound for the audit middleware.
    try:
        request.state.conn = resolve_conn(settings=settings, banks=banks, override=dataset)
    except (KeyError, ValueError):
        pass
    audit.annotate(
        security_read=True,
        authorised_by=None,
        authorised_reason=f"export {export_id} of {dataset!r} downloaded on a signed link",
    )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{dataset}-{export_id}.trig.zip",
        headers={"Cache-Control": "no-store"},
    )
