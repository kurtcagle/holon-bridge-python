"""Projection hook routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..deps import ClientDep, ConnDep
from ..fuseki import FusekiError
from ..projection import (
    CHANGE_MODES,
    DELIVERY_MODES,
    HOOK_STATUSES,
    PROJ,
    ProjectionError,
    ProjectionHook,
    ProjectionRunner,
    ProjectionStore,
    scope_graph,
)

router = APIRouter(prefix="/projection", tags=["projection"])


class RegisterHookRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    target: str = Field(..., min_length=1)
    # named 'scope' rather than 'construct': pydantic's BaseModel already has
    # a 'construct' attribute, and shadowing it is a warning waiting to become
    # a bug.
    scope: str | None = None
    named_query: str | None = None
    change_mode: str = Field(default="upsert")
    delivery: str = Field(default="pull")
    endpoint: str | None = None
    key_predicate: str | None = None
    media_type: str = "text/turtle"
    label: str | None = None
    description: str | None = None


class RunRequest(BaseModel):
    params: dict[str, object] = Field(default_factory=dict)
    force: bool = False
    include_payload: bool = True


class StatusRequest(BaseModel):
    status: str = Field(..., pattern="^(Active|Suspended|Deprecated)$")


class RejectRequest(BaseModel):
    reason: str = Field(default="rejected by target", max_length=2000)


async def _hook(client, conn, hook_id: str) -> ProjectionHook:
    hooks = await ProjectionStore(client).hooks(conn)
    for hook in hooks:
        if hook.id == hook_id:
            return hook
    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={
            "error": "unknown_hook",
            "id": hook_id,
            "available": [h.id for h in hooks],
        },
    )


@router.get("/hooks")
async def list_hooks(
    conn: ConnDep,
    client: ClientDep,
    hook_status: str | None = Query(default=None, pattern="^(Active|Suspended|Deprecated)$"),
) -> dict:
    hooks = await ProjectionStore(client).hooks(conn)
    if hook_status:
        hooks = [h for h in hooks if h.status == hook_status]
    return {
        "dataset": conn.dataset,
        "graph": conn.graph("projections"),
        "count": len(hooks),
        "changeModes": list(CHANGE_MODES),
        "deliveryModes": list(DELIVERY_MODES),
        "statuses": list(HOOK_STATUSES),
        "hooks": [{**h.summary(), "problems": h.problems()} for h in hooks],
    }


@router.get("/hook/{hook_id}")
async def get_hook(hook_id: str, conn: ConnDep, client: ClientDep) -> dict:
    hook = await _hook(client, conn, hook_id)
    store = ProjectionStore(client)
    watermark = scope_graph(conn.dataset, hook.id)
    return {
        **hook.detail(),
        "problems": hook.problems(),
        "watermark": {
            "graph": watermark,
            "triples": await store.graph_count(conn, watermark),
        },
    }


@router.post("/hook")
async def register_hook(
    body: RegisterHookRequest, conn: ConnDep, client: ClientDep
) -> dict:
    hook = ProjectionHook(
        id=body.id,
        iri=f"{PROJ}hook-{body.id}",
        target=body.target,
        construct=body.scope or "",
        named_query=body.named_query or "",
        change_mode=body.change_mode.lower(),
        delivery=body.delivery.lower(),
        endpoint=body.endpoint or "",
        key_predicate=body.key_predicate or "",
        media_type=body.media_type,
        label=body.label or "",
        description=body.description or "",
    )

    problems = hook.problems()
    fatal = [p for p in problems if not p.startswith("upsert with no keyPredicate")]
    if fatal:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_hook", "problems": fatal},
        )

    try:
        await ProjectionStore(client).save_hook(conn, hook)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"ok": True, **hook.summary(), "warnings": problems}


@router.post("/hook/{hook_id}/status")
async def set_hook_status(
    hook_id: str, body: StatusRequest, conn: ConnDep, client: ClientDep
) -> dict:
    hook = await _hook(client, conn, hook_id)
    await ProjectionStore(client).set_hook_status(conn, hook, body.status)
    return {"ok": True, "id": hook_id, "hookStatus": body.status}


@router.delete("/hook/{hook_id}")
async def delete_hook(hook_id: str, conn: ConnDep, client: ClientDep) -> dict:
    hook = await _hook(client, conn, hook_id)
    await ProjectionStore(client).delete_hook(conn, hook)
    return {"ok": True, "id": hook_id, "watermarkDropped": True}


@router.post("/hook/{hook_id}/run")
async def run_hook(
    hook_id: str, body: RunRequest, conn: ConnDep, client: ClientDep
) -> dict:
    hook = await _hook(client, conn, hook_id)
    try:
        delivery, envelope = await ProjectionRunner(client).run(
            conn, hook, params=body.params, force=body.force
        )
    except ProjectionError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "projection_error", "id": hook_id, "message": str(exc)},
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        **delivery.as_dict(),
        "envelope": envelope.as_dict(include_payload=body.include_payload),
    }


@router.post("/hook/{hook_id}/reset")
async def reset_hook(hook_id: str, conn: ConnDep, client: ClientDep) -> dict:
    """Forget what has been delivered, so the next run sends the whole slice."""
    hook = await _hook(client, conn, hook_id)
    await ProjectionStore(client).reset_watermark(conn, hook.id)
    return {"ok": True, "id": hook_id, "watermark": "cleared"}


class SweepRequest(BaseModel):
    max_age_seconds: float = Field(default=86_400, ge=0, le=2_592_000)


@router.post("/sweep")
async def sweep(body: SweepRequest, conn: ConnDep, client: ClientDep) -> dict:
    """Reclaim deliveries no target ever acknowledged.

    Safe to run often: sweeping leaves the watermark alone, so a swept
    delivery's difference is simply re-derived on the next run.
    """
    try:
        return {"ok": True, **await ProjectionRunner(client).sweep(
            conn, max_age_seconds=body.max_age_seconds
        )}
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc


@router.get("/deliveries")
async def list_deliveries(
    conn: ConnDep,
    client: ClientDep,
    hook: str | None = Query(default=None),
    delivery_status: str | None = Query(
        default=None, pattern="^(pending|delivered|acknowledged|failed)$"
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    records = await ProjectionStore(client).deliveries(
        conn, hook_id=hook, delivery_status=delivery_status, limit=limit
    )
    return {"count": len(records), "deliveries": records}


@router.get("/delivery/{delivery_id}")
async def get_delivery(
    delivery_id: str,
    conn: ConnDep,
    client: ClientDep,
    include_payload: bool = Query(default=True),
) -> dict:
    store = ProjectionStore(client)
    delivery = await store.delivery(conn, delivery_id)
    if delivery is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_delivery", "id": delivery_id},
        )
    body = delivery.as_dict()
    if delivery.status == "pending":
        envelope = await ProjectionRunner(client).envelope_for(
            conn, delivery, include_payload=include_payload
        )
        body["envelope"] = envelope.as_dict(include_payload=include_payload)
    return body


@router.post("/delivery/{delivery_id}/ack")
async def acknowledge(delivery_id: str, conn: ConnDep, client: ClientDep) -> dict:
    """Confirm the target applied this envelope; the watermark advances."""
    store = ProjectionStore(client)
    delivery = await store.delivery(conn, delivery_id)
    if delivery is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_delivery", "id": delivery_id},
        )
    try:
        await ProjectionRunner(client).acknowledge(conn, delivery)
    except ProjectionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"ok": True, **delivery.as_dict()}


@router.post("/delivery/{delivery_id}/reject")
async def reject(
    delivery_id: str, body: RejectRequest, conn: ConnDep, client: ClientDep
) -> dict:
    """Report that the target could not apply it; the watermark stays put."""
    store = ProjectionStore(client)
    delivery = await store.delivery(conn, delivery_id)
    if delivery is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_delivery", "id": delivery_id},
        )
    try:
        await ProjectionRunner(client).reject(conn, delivery, body.reason)
    except ProjectionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"ok": True, **delivery.as_dict()}
