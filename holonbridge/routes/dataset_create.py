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

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit
from ..claim import creator
from ..dataset_create import CreateError, Kind, create, describe_new
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
    audit.annotate(
        authorised_by=created.claim["person"] if created.claim else None,
        authorised_reason=(
            f"dataset {created.dataset!r} created ({created.kind}); "
            f"{who.external_id!r} is on DATASET_CREATORS"
        ),
    )
    return result
