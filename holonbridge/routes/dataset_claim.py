"""``POST /admin/dataset/initialise`` -- claim an unprovisioned dataset.

Deliberately depends on ``ConnDep`` (which runs the bearer check) and NOT on
``AnimusDep``: the whole point is that the caller has no Person yet, and
``AnimusDep`` answers 401 for exactly that. Everything it does instead -- decide
who the caller is, check the allowlist, check the dataset is unclaimed -- is in
:mod:`holonbridge.claim`; this route is a thin wrapper that adds the audit
record.

An ineligible caller always gets the same 403, whether or not the dataset is
unclaimed, so this route cannot be used to ask which datasets are open.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import audit
from ..claim import ClaimError, claim, claimant
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

router = APIRouter(prefix="/admin", tags=["admin"])


class InitialiseRequest(BaseModel):
    slug: str | None = Field(
        default=None,
        description="Local identifier for the new owner Person. Defaults to one derived "
        "from your identity. There is no field for naming anyone else: the owner is the caller.",
    )
    full_name: str | None = Field(
        default=None, description="Display name (rdfs:label). Defaults to your identifier."
    )


@router.post("/dataset/initialise")
async def initialise_dataset(
    body: InitialiseRequest,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
) -> dict:
    """Make the caller the first owner of a dataset that has no owner or admin.

    Only callers whose identity is established (identity-bound token, or a
    trusted proxy asserting one) AND listed in ``DATASET_CLAIMANTS`` may use it.
    Refused with 409 if the dataset already has an owner or admin. Creates the
    dataset's Home if it has none. Confirm with the person you are acting for
    before calling it.
    """
    who = claimant(
        settings,
        token_auth,
        header_id=request.headers.get(ANIMUS_ID_HEADER),
        header_type=request.headers.get(ANIMUS_TYPE_HEADER),
        default_type=DEFAULT_ANIMUS_TYPE,
    )
    if who is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="this caller may not initialise a dataset"
        )

    try:
        result = await claim(client, conn, who, slug=body.slug, full_name=body.full_name)
    except ClaimError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc

    audit.annotate(
        authorised_by=result["person"],
        authorised_reason=(
            f"dataset initialised: no owner or admin existed; {who.external_id!r} is on "
            "DATASET_CLAIMANTS and was granted owner"
        ),
    )
    return result
