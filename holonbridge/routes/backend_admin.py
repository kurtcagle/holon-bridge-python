"""``POST /admin/backend/restart`` and ``GET /admin/backend/status``.

Restart the Fuseki server next to the bridge, safely. The rules -- what counts as an
in-memory dataset, what blocks a restart, how the restart happens and how in-memory
datasets are put back -- are in :mod:`holonbridge.fuseki_restart`.

Gate: an *established* identity listed in ``FUSEKI_RESTARTERS`` (not dataset admin, not
``DATASET_CREATORS``: a restart interrupts every dataset on the server). Anyone else gets the
same 403 whatever they ask, so these routes cannot be used to find out what exists. The dry
run is gated too, because the plan names every dataset on the server.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit
from ..claim import restarter
from ..deps import (
    ANIMUS_ID_HEADER,
    ANIMUS_TYPE_HEADER,
    DEFAULT_ANIMUS_TYPE,
    ClientDep,
    ConnDep,
    SettingsDep,
    TokenAuth,
    require_auth,
)
from ..fuseki_restart import RestartError, backend_status, build_plan, commit

router = APIRouter(prefix="/admin/backend", tags=["admin"])


def restarter_or_403(request: Request, settings, token_auth, what: str = "restart Fuseki"):
    who = restarter(
        settings,
        token_auth,
        header_id=request.headers.get(ANIMUS_ID_HEADER),
        header_type=request.headers.get(ANIMUS_TYPE_HEADER),
        default_type=DEFAULT_ANIMUS_TYPE,
    )
    if who is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"this caller may not {what}")
    return who


class RestartRequest(BaseModel):
    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Why Fuseki needs restarting. Recorded in the audit log and the request.",
    )
    commit: bool = Field(
        default=False,
        description="False (the default) is a dry run: it returns the plan and changes nothing.",
    )
    snapshot_first: bool = Field(
        default=True,
        description="Snapshot every in-memory dataset that holds data before restarting, so "
        "it can be rebuilt as it was.",
    )
    restore_from: Literal["auto", "latest", "default"] = Field(
        default="auto",
        description="Where in-memory datasets are rebuilt from: 'auto' uses a pinned default "
        "backup if there is one, otherwise the newest snapshot; 'latest' ignores the default; "
        "'default' insists on it.",
    )
    acknowledge_loss: list[str] = Field(
        default_factory=list,
        description="In-memory datasets the caller accepts losing if they have no backup.",
    )
    wait_seconds: int = Field(
        default=90, ge=5, le=600, description="How long to wait for Fuseki to come back."
    )


@router.post("/restart")
async def restart_fuseki(
    body: RestartRequest,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
) -> dict:
    """Preview (default) or perform a Fuseki restart, then rebuild its in-memory datasets.

    Refusals: 403 for an ineligible caller; 409 ``not_applicable`` for a non-Fuseki store (and
    ``restart_blocked`` with the full plan when anything stands in the way -- an in-memory
    dataset with no way back, an unparseable assembler file, a write or a Fuseki backup in
    progress, a restart already pending or too recent); 400 ``bad_restore_from``.
    """
    who = restarter_or_403(request, settings, token_auth)
    try:
        plan = await build_plan(
            client,
            conn,
            settings,
            snapshot_first=body.snapshot_first,
            restore_from=body.restore_from,
            acknowledge_loss=body.acknowledge_loss,
        )
    except RestartError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    if not body.commit:
        audit.annotate(
            authorised_by=None,
            authorised_reason=f"restart dry run; {who.external_id!r} is on FUSEKI_RESTARTERS",
        )
        return {"ok": True, "committed": False, **plan.to_dict()}

    if any(b["code"] == "not_applicable" for b in plan.blockers):
        audit.annotate(reason="not_applicable")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "not_applicable", "message": plan.blockers[0]["message"], "plan": plan.to_dict()},
        )
    try:
        result = await commit(
            client,
            conn,
            settings,
            by=who.external_id,
            reason=body.reason,
            snapshot_first=body.snapshot_first,
            restore_from=body.restore_from,
            acknowledge_loss=body.acknowledge_loss,
            wait_seconds=body.wait_seconds,
        )
    except RestartError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    audit.annotate(
        authorised_by=None,
        authorised_reason=(
            f"fuseki restart {result['id']} ({result['status']}): {body.reason!r}; "
            f"{who.external_id!r} is on FUSEKI_RESTARTERS"
        ),
    )
    return result


@router.get("/status")
async def get_backend_status(
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
) -> dict:
    """Read-only: the datasets and their kinds, running Fuseki tasks, the last restart, and
    whether one is pending. Same gate as the restart itself."""
    restarter_or_403(request, settings, token_auth, "inspect the Fuseki backend")
    return await backend_status(client, conn, settings)
