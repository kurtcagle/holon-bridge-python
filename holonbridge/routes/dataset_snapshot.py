"""``POST /admin/dataset/snapshot`` and ``POST /admin/dataset/restore``.

Snapshot a dataset's graphs to files, and put them back into an empty dataset
after Fuseki has lost them (an in-memory dataset after a restart). The rules are
in :mod:`holonbridge.snapshot`; the two gates differ on purpose:

* **snapshot** reads every graph in the dataset, so it needs *admin on that
  dataset*, the same as any other way of reading across it. A server-level role
  such as ``DATASET_CREATORS`` does not imply the right to read a dataset it
  does not administer.
* **restore** runs against an *empty* dataset, which has no admin, so it uses the
  creation gate (``DATASET_CREATORS``, established identity). It can only put
  back what an admin of that same dataset snapshotted earlier.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit
from ..acl import is_admin
from ..claim import creator
from ..deps import (
    ANIMUS_ID_HEADER,
    ANIMUS_TYPE_HEADER,
    DEFAULT_ANIMUS_TYPE,
    AnimusDep,
    ClientDep,
    ConnDep,
    SettingsDep,
    TokenAuth,
    require_auth,
)
from ..snapshot import SnapshotError, list_snapshots, restore, take

router = APIRouter(prefix="/admin/dataset", tags=["admin"])


@router.post("/snapshot")
async def snapshot_dataset(
    animus: AnimusDep, conn: ConnDep, client: ClientDep, settings: SettingsDep
) -> dict:
    """Write a snapshot of the active dataset to ``SNAPSHOT_DIR``.

    Admin on the dataset only. Adds a timestamped snapshot, keeping the newest
    ``SNAPSHOT_KEEP`` (default 5) and removing older ones only after the new one
    is safely written. Refuses an empty dataset (409 ``nothing_to_snapshot``) and
    one over ``SNAPSHOT_MAX_BYTES`` (413). 503 ``snapshot_dir_unset`` when
    snapshots are not configured. Take it BEFORE restarting Fuseki.
    """

    async def query_fn(query: str) -> dict:
        return await client.select(conn, query)

    if not await is_admin(query_fn, conn.holons_graph, person=animus.person):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin role required")

    try:
        taken = await take(client, conn, settings, by=animus.person)
    except SnapshotError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    audit.annotate(
        authorised_by=animus.person,
        authorised_reason=f"snapshot {taken.id} of {taken.dataset!r} taken ({taken.graphs} graphs)",
    )
    return {
        "ok": True,
        "dataset": taken.dataset,
        "snapshot": taken.id,
        "kept": taken.kept,
        "graphs": taken.graphs,
        "bytes": taken.bytes,
        "createdAt": taken.created_at,
    }


class RestoreRequest(BaseModel):
    snapshot: str | None = Field(
        default=None,
        description="Id of the snapshot to restore (from list_snapshots). Default: the newest.",
    )


def _creator_or_403(request: Request, settings, token_auth, what: str):
    who = creator(
        settings,
        token_auth,
        header_id=request.headers.get(ANIMUS_ID_HEADER),
        header_type=request.headers.get(ANIMUS_TYPE_HEADER),
        default_type=DEFAULT_ANIMUS_TYPE,
    )
    if who is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"this caller may not {what}")
    return who


@router.get("/snapshots")
async def snapshots(
    request: Request,
    conn: ConnDep,
    settings: SettingsDep,
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
) -> dict:
    """List the active dataset's snapshots, newest first.

    Same gate as restore (``DATASET_CREATORS``): what you may restore you may
    see, and an emptied dataset has no admin to ask. Shows ids, times, who took
    them and sizes -- never contents.
    """
    _creator_or_403(request, settings, token_auth, "list a dataset's snapshots")
    try:
        found = list_snapshots(settings, conn.dataset)
    except SnapshotError as exc:
        raise HTTPException(exc.status, detail=exc.detail()) from exc
    return {"dataset": conn.dataset, "count": len(found), "keep": settings.snapshot_keep, "snapshots": found}


@router.post("/restore")
async def restore_dataset(
    body: RestoreRequest,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
) -> dict:
    """Put a snapshot of the active dataset (the newest unless ``snapshot`` names one) back into it.

    Only a caller on ``DATASET_CREATORS`` with an established identity; anyone
    else gets the same 403. Never overwrites: a dataset with any content is
    refused (409 ``dataset_not_empty``). 400 ``bad_snapshot_id``; 404
    ``no_snapshot`` / ``unknown_dataset``; 409 ``snapshot_mismatch``; 502
    ``restore_failed`` (the graphs already written are removed again, so it can
    be retried).
    """
    who = _creator_or_403(request, settings, token_auth, "restore a dataset")
    try:
        restored = await restore(client, conn, settings, snapshot=body.snapshot)
    except SnapshotError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    audit.annotate(
        authorised_by=None,
        authorised_reason=(
            f"snapshot {restored.id} of {restored.dataset!r} restored ({len(restored.graphs)} graphs); "
            f"{who.external_id!r} is on DATASET_CREATORS"
        ),
    )
    return {
        "ok": True,
        "dataset": restored.dataset,
        "snapshot": restored.id,
        "restored": len(restored.graphs),
        "graphs": restored.graphs,
        "snapshotCreatedAt": restored.created_at,
    }
