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
from ..snapshot import SnapshotError, restore, take

router = APIRouter(prefix="/admin/dataset", tags=["admin"])


@router.post("/snapshot")
async def snapshot_dataset(
    animus: AnimusDep, conn: ConnDep, client: ClientDep, settings: SettingsDep
) -> dict:
    """Write a snapshot of the active dataset to ``SNAPSHOT_DIR``.

    Admin on the dataset only. Replaces the dataset's previous snapshot
    atomically. Refuses an empty dataset (409 ``nothing_to_snapshot``) so a
    snapshot taken after a restart cannot replace a good one with nothing, and
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
        authorised_reason=f"snapshot of {taken.dataset!r} taken ({taken.graphs} graphs)",
    )
    return {
        "ok": True,
        "dataset": taken.dataset,
        "graphs": taken.graphs,
        "bytes": taken.bytes,
        "createdAt": taken.created_at,
    }


@router.post("/restore")
async def restore_dataset(
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
) -> dict:
    """Put the active dataset's snapshot back into it, if it is empty.

    Only a caller on ``DATASET_CREATORS`` with an established identity; anyone
    else gets the same 403. Never overwrites: a dataset with any content is
    refused (409 ``dataset_not_empty``). 404 ``no_snapshot`` / ``unknown_dataset``;
    409 ``snapshot_mismatch``; 502 ``restore_failed`` (the graphs already written
    are removed again, so it can be retried).
    """
    who = creator(
        settings,
        token_auth,
        header_id=request.headers.get(ANIMUS_ID_HEADER),
        header_type=request.headers.get(ANIMUS_TYPE_HEADER),
        default_type=DEFAULT_ANIMUS_TYPE,
    )
    if who is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="this caller may not restore a dataset"
        )
    try:
        restored = await restore(client, conn, settings)
    except SnapshotError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    audit.annotate(
        authorised_by=None,
        authorised_reason=(
            f"snapshot of {restored.dataset!r} restored ({len(restored.graphs)} graphs); "
            f"{who.external_id!r} is on DATASET_CREATORS"
        ),
    )
    return {
        "ok": True,
        "dataset": restored.dataset,
        "restored": len(restored.graphs),
        "graphs": restored.graphs,
        "snapshotCreatedAt": restored.created_at,
    }
