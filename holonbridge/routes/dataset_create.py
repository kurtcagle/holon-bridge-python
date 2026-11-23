"""``POST /datasets`` -- create a new, empty dataset on the backend.

Create only; the bridge has no route that drops a dataset and never will without
a separate decision. Depends on ``ConnDep``/``require_auth`` (not ``AnimusDep``)
for the same reason ``dataset_claim`` does: the gate is *who the caller provably
is*, checked against ``DATASET_CREATORS``, not a Person in whatever dataset
happens to be active. An ineligible caller always gets the same 403, so this
route cannot be used to ask what exists. All the rules are in
:mod:`holonbridge.dataset_create`.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit
from ..claim import creator, restarter
from ..dataset_create import CreateError, Kind, create, describe_new, reseed_shapes
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

from ..fuseki_restart import RestartError, build_plan, commit as restart_commit

log = logging.getLogger(__name__)

router = APIRouter(tags=["datasets"])


class CreateDatasetRequest(BaseModel):
    name: str = Field(
        description="1-64 characters: lower-case letters, digits, hyphen, underscore; "
        "starts with a letter."
    )
    kind: Kind = Field(
        description="'persistent' (TDB2, survives a Fuseki restart) or 'memory' "
        "(contents lost on restart). Required: there is no default, so a volatile "
        "dataset is never created by omission."
    )
    shapes: bool = Field(
        default=True,
        description="Seed the bundled shapes (identity, delegation, fluent) into the new "
        "dataset's shapes graph. On by default, because every created dataset has SHACL "
        "enabled and a gated bridge rejects writes to a dataset with no shapes. Set false "
        "to leave the shapes graph empty, e.g. to configure bank scoping first.",
    )
    restart: Literal["plan", "commit"] = Field(
        default="plan",
        description="A new dataset only gets its /shacl endpoint when Fuseki restarts. 'plan' "
        "(the default) creates the dataset and returns the restart dry run under 'restart'. "
        "'commit' also performs the restart, when the caller is on FUSEKI_RESTARTERS (checked "
        "BEFORE anything is created) and nothing stands in the way; if something does, the "
        "dataset is still created and 'restarted' is false.",
    )
    claim: bool = Field(
        default=True,
        description="Make the caller the new dataset's owner. Set false to create it "
        "without writing anything, e.g. to configure bank scoping first.",
    )


@router.post("/datasets", status_code=status.HTTP_201_CREATED)
async def create_dataset(
    body: CreateDatasetRequest,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
) -> dict:
    """Create a new, empty dataset.

    Only a caller whose identity is established (identity-bound token, or a
    trusted proxy asserting one) AND listed in ``DATASET_CREATORS`` may use it;
    anyone else gets the same 403. The caller becomes the dataset's owner unless
    ``claim`` is false. Confirm the name and kind with the person you are acting
    for first -- the bridge cannot drop a dataset once it exists.

    Refusals: 400 ``bad_dataset_name`` / ``reserved_dataset_name``; 409
    ``dataset_exists`` or ``dataset_limit_reached``; 502 ``admin_api_unavailable``
    or ``create_failed`` when Fuseki would not do it.
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
            status.HTTP_403_FORBIDDEN, detail="this caller may not create a dataset"
        )

    restart_who = None
    if body.restart == "commit":
        # Decided before anything is created: a caller who may not restart Fuseki must not
        # leave a half-finished dataset behind by asking for it.
        restart_who = restarter(
            settings,
            token_auth,
            header_id=request.headers.get(ANIMUS_ID_HEADER),
            header_type=request.headers.get(ANIMUS_TYPE_HEADER),
            default_type=DEFAULT_ANIMUS_TYPE,
        )
        if restart_who is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="this caller may not restart Fuseki (restart='commit'); nothing was created",
            )

    try:
        created = await create(
            client, conn, settings, who, name=body.name, kind=body.kind, claim_for_creator=body.claim,
            seed_shapes=body.shapes,
        )
    except CreateError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    result = describe_new(conn, created.dataset, created.kind)
    result.update(
        ok=True,
        created=True,
        claimed=created.claim is not None,
        owner=created.claim["person"] if created.claim else None,
        claimError=created.claim_error,
        shaclEnabled=created.shacl_enabled,
        shaclEndpoint=created.conn.shacl_endpoint if created.shacl_enabled else None,
        shapesSeeded=created.shapes_seeded,
        shapesError=created.shapes_error,
    )
    result.update(
        shaclConfigured=created.shacl_configured,
        restartRequired=created.shacl_configured and not created.shacl_enabled,
        shaclConfigError=created.shacl_config_error,
    )
    if created.shacl_configured and not created.shacl_enabled:
        result.setdefault("notes", []).append(
            "a shacl endpoint was added to the dataset's Fuseki configuration, but Fuseki reads "
            "that at startup: restart Fuseki before validating against this dataset. Until then "
            "a SHACL-gated bridge cannot write to it"
        )
    elif not created.shacl_enabled:
        result.setdefault("notes", []).append(
            f"this dataset has no shacl endpoint ({created.shacl_config_error}); add one in "
            "Fuseki's configuration for it, then restart Fuseki"
        )
    if created.shacl_configured and not created.shacl_enabled:
        result["restart"] = await _restart_block(
            client, conn, settings, restart_who, body.restart, created.dataset
        )
        result["restarted"] = bool(result["restart"].get("restarted"))
    audit.annotate(
        authorised_by=created.claim["person"] if created.claim else None,
        authorised_reason=(
            f"dataset {created.dataset!r} created ({created.kind}); "
            f"{who.external_id!r} is on DATASET_CREATORS"
        ),
    )
    return result


async def _restart_block(client, conn, settings, who, mode: str, dataset: str) -> dict:
    """The ``restart`` part of a create result: the dry-run plan, and the outcome if committed.

    Advisory: the dataset already exists, so nothing here may turn the response into an error.
    """
    try:
        plan = await build_plan(client, conn, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("restart plan for new dataset %s failed: %s", dataset, exc)
        return {"required": True, "planError": str(exc), "restarted": False}
    block: dict = {
        "required": True,
        "plan": plan.to_dict(),
        "canRestart": plan.can_restart,
        "restarted": False,
        "next": (
            "call restart_fuseki(commit=true) to bring the dataset's /shacl endpoint up"
            if plan.can_restart
            else "restart_fuseki reports what is in the way; clear it, then restart_fuseki(commit=true)"
        ),
    }
    if mode != "commit" or who is None:
        return block
    if not plan.can_restart:
        block["commitSkipped"] = plan.blockers
        return block
    try:
        outcome = await restart_commit(
            client,
            conn,
            settings,
            by=who.external_id,
            reason=f"new dataset {dataset!r} needs its shacl endpoint",
        )
    except RestartError as exc:
        block["commitError"] = exc.detail()
        return block
    block["outcome"] = outcome
    block["restarted"] = outcome.get("restarted") is True
    return block


class ReseedRequest(BaseModel):
    claim: bool = Field(
        default=False,
        description="Also make the caller the dataset's owner, if it has none. Use this on a "
        "dataset that came back empty (an in-memory one after a Fuseki restart) to restore "
        "both the shapes and an owner in one call.",
    )


@router.post("/admin/dataset/shapes")
async def seed_dataset_shapes(
    body: ReseedRequest,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
) -> dict:
    """Put the bundled shapes into the active dataset, if its shapes graph is empty.

    Same gate as creating a dataset (``DATASET_CREATORS``, established identity),
    because it writes to a dataset the caller may not own yet. It only ever fills
    an *empty* shapes graph: existing shapes are never overwritten, and that is
    reported as ``seeded: false`` rather than an error. ``claim=true`` also makes
    the caller the owner of a dataset that has none.

    Refusals: 403 for an ineligible caller; 404 ``unknown_dataset``.
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
            status.HTTP_403_FORBIDDEN, detail="this caller may not seed a dataset's shapes"
        )
    try:
        result = await reseed_shapes(client, conn, who, claim_for_caller=body.claim)
    except CreateError as exc:
        audit.annotate(reason=exc.code)
        raise HTTPException(exc.status, detail=exc.detail()) from exc

    audit.annotate(
        authorised_by=result.claim["person"] if result.claim else None,
        authorised_reason=(
            f"shapes {'seeded into' if result.seeded else 'left alone in'} {result.dataset!r}; "
            f"{who.external_id!r} is on DATASET_CREATORS"
        ),
    )
    return {
        "ok": True,
        "dataset": result.dataset,
        "shapesGraph": result.shapes_graph,
        "seeded": result.seeded,
        "skippedReason": result.skipped_reason,
        "claimed": result.claim is not None,
        "owner": result.claim["person"] if result.claim else None,
        "claimError": result.claim_error,
        "shaclEnabled": result.shacl_enabled,
    }
