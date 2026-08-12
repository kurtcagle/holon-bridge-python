"""Fluent-update routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import ClientDep, ConnDep
from ..fluent import FluentError, FluentUpdateResult, Operation, prior_value, update_fluent
from ..fuseki import FusekiError

router = APIRouter(prefix="/fluent", tags=["fluent"])


class UpdateFluentRequest(BaseModel):
    fluent: str = Field(..., min_length=1, description="IRI of the holon:Fluent to update")
    operation: Operation
    value: Any = None
    is_iri: bool = False
    asserted_by: str | None = None
    description: str | None = None


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


@router.post("/update")
async def update(
    body: UpdateFluentRequest, conn: ConnDep, client: ClientDep
) -> dict:
    try:
        result = await update_fluent(
            client,
            conn,
            fluent=body.fluent,
            operation=body.operation,
            value=body.value,
            is_iri=body.is_iri,
            asserted_by=body.asserted_by,
            description=body.description,
        )
    except FluentError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "fluent_error", "message": str(exc)},
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return _result_dict(result)


@router.get("/{fluent_id:path}/prior")
async def get_prior_value(fluent_id: str, conn: ConnDep, client: ClientDep) -> dict:
    """The value this fluent held immediately before its current one --
    what a caller building a 'revert' Set would read first."""
    try:
        value = await prior_value(client, conn, fluent=fluent_id)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    if value is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "no_prior_value", "fluent": fluent_id},
        )
    return {"fluent": fluent_id, "priorValue": {"kind": value.kind, "value": value.lexical}}
