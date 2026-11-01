"""Projection hook routes.

CHANGED 2026-11-01: these routes were gated by the bearer token alone -- no
resolved identity, no ACL. Anyone holding a valid token could register a hook
with any scope and any webhook endpoint, run it, and read the slice back,
which is a way to read the whole dataset and send it anywhere. They now work
like the other write routes (``/pipeline``, ``/named-query``):

* Every route needs a resolved identity (``AnimusDep``).
* Operating the registry -- register, change status, run, reset, sweep,
  acknowledge, reject -- needs ``check_write`` on the dataset's hook registry
  graph (``urn:{dataset}:projections``); deleting a hook needs
  ``check_replace`` on it, as dropping a pipeline does. One graph stands for
  all of them: whoever may write the registry may operate it, and there is no
  second grant to provision for the delivery log or the watermark graphs.
* A hook's scope is read with the bridge's own authority when it runs, so the
  check that the *caller* may read it happens here, at the points where the
  caller causes it to be read or sees the result: registering, running, and
  fetching a pending delivery's payload. A CONSTRUCT scope must pass
  ``authorize_query`` for the caller; a named-query scope must be reachable
  from the caller's persona, exactly as for running that query. Admins bypass,
  as everywhere in ``acl.py``.
* A webhook endpoint must pass the egress policy (``egress.py``) when it is
  registered, and again each time the bridge sends to it.

Listing hooks and deliveries needs an identity but nothing more: both return
configuration and counts, not graph content.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import audit
from ..acl import authorize_query, check_replace, check_write, is_admin
from ..deps import AnimusDep, ClientDep, ConnDep, PersonasDep, SettingsDep
from ..egress import EgressPolicy, EndpointRefused, check_endpoint
from ..fuseki import FusekiError
from ..named_queries import load_named_queries
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
from ..toolset import resolve_reachable

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


# --- access -------------------------------------------------------------------


def _query_fn(client, conn):
    async def query_fn(query: str) -> dict:
        return await client.select(conn, query)

    return query_fn


def _identified(animus) -> str:
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    return animus.person


async def _gate(conn, client, animus, *, replace: bool = False) -> None:
    """May this caller operate (or, with ``replace``, delete from) the hook
    registry?"""
    person = _identified(animus)
    target = conn.graph("projections")
    check = check_replace if replace else check_write
    decision = await check(
        _query_fn(client, conn), conn.holons_graph, person=person, target=target
    )
    audit.annotate(graphs=[target])
    if not decision.allowed:
        audit.annotate(reason=decision.reason)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"{decision.reason} (graph: {target})"
        )


async def _authorize_scope(hook: ProjectionHook, conn, client, animus, personas) -> None:
    """May this caller read what the hook's scope reads?

    The runner reads the scope as the bridge, so without this a caller who can
    operate hooks could read, through a hook, anything the bridge can.
    """
    person = _identified(animus)
    query_fn = _query_fn(client, conn)

    # Break-glass, as in acl.py. Checked first because authorize_query refuses a
    # query it cannot parse even for an admin, and an admin registering a hook
    # should not be locked out by the parser's limits.
    if await is_admin(query_fn, conn.holons_graph, person=person):
        return

    if hook.construct:
        decision = await authorize_query(
            query_fn,
            conn.holons_graph,
            person=person,
            sparql_text=hook.construct,
            persona_of_graph=conn.persona_for_graph,
        )
        if not decision.allowed:
            audit.annotate(reason=decision.reason)
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "scope_not_readable",
                    "message": "the hook's scope reads something this caller may "
                    f"not read: {decision.reason}",
                },
            )
        return

    registry = await load_named_queries(client, conn)
    named = registry.by_id(hook.named_query)
    if named is None:
        return  # nothing to authorise; running the hook reports the unknown query
    persona, _source = personas.get(person_id=person, dataset=conn.dataset)
    reachable = await resolve_reachable(
        query_fn, conn, persona=persona, candidate_iris=[named.iri]
    )
    if named.iri not in reachable:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail={
                "error": "scope_not_reachable",
                "message": f"named query {hook.named_query!r} is not reachable "
                "from this caller's persona",
            },
        )


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


# --- hooks --------------------------------------------------------------------


@router.get("/hooks")
async def list_hooks(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
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
async def get_hook(
    hook_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
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
    body: RegisterHookRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    settings: SettingsDep,
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

    # Access first, so a caller who may not register hooks learns nothing about
    # which endpoints the policy would have accepted.
    await _gate(conn, client, animus)

    if hook.delivery == "webhook":
        try:
            await check_endpoint(hook.endpoint, EgressPolicy(settings.webhook_allowlist))
        except EndpointRefused as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={"error": "endpoint_refused", "message": str(exc)},
            ) from exc

    await _authorize_scope(hook, conn, client, animus, personas)

    try:
        await ProjectionStore(client).save_hook(conn, hook)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"ok": True, **hook.summary(), "warnings": problems}


@router.post("/hook/{hook_id}/status")
async def set_hook_status(
    hook_id: str, body: StatusRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    await _gate(conn, client, animus)
    hook = await _hook(client, conn, hook_id)
    await ProjectionStore(client).set_hook_status(conn, hook, body.status)
    return {"ok": True, "id": hook_id, "hookStatus": body.status}


@router.delete("/hook/{hook_id}")
async def delete_hook(
    hook_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    # Deleting discards the hook and its watermark: the same wholesale removal
    # that dropping a pipeline is, so the same, stricter grant.
    await _gate(conn, client, animus, replace=True)
    hook = await _hook(client, conn, hook_id)
    await ProjectionStore(client).delete_hook(conn, hook)
    return {"ok": True, "id": hook_id, "watermarkDropped": True}


@router.post("/hook/{hook_id}/run")
async def run_hook(
    hook_id: str,
    body: RunRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
) -> dict:
    await _gate(conn, client, animus)
    hook = await _hook(client, conn, hook_id)
    await _authorize_scope(hook, conn, client, animus, personas)
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
async def reset_hook(
    hook_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Forget what has been delivered, so the next run sends the whole slice."""
    await _gate(conn, client, animus)
    hook = await _hook(client, conn, hook_id)
    await ProjectionStore(client).reset_watermark(conn, hook.id)
    return {"ok": True, "id": hook_id, "watermark": "cleared"}


class SweepRequest(BaseModel):
    max_age_seconds: float = Field(default=86_400, ge=0, le=2_592_000)


@router.post("/sweep")
async def sweep(
    body: SweepRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Reclaim deliveries no target ever acknowledged.

    Safe to run often: sweeping leaves the watermark alone, so a swept
    delivery's difference is simply re-derived on the next run.
    """
    await _gate(conn, client, animus)
    try:
        return {"ok": True, **await ProjectionRunner(client).sweep(
            conn, max_age_seconds=body.max_age_seconds
        )}
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc


# --- deliveries ---------------------------------------------------------------


@router.get("/deliveries")
async def list_deliveries(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
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
    animus: AnimusDep,
    personas: PersonasDep,
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
        if include_payload:
            # The payload is the slice itself: whoever collects it must be able
            # to operate hooks and to read what the hook's scope reads.
            await _gate(conn, client, animus)
            hooks = await store.hooks(conn)
            hook = next((h for h in hooks if h.id == delivery.hook_id), None)
            if hook is not None:
                await _authorize_scope(hook, conn, client, animus, personas)
        envelope = await ProjectionRunner(client).envelope_for(
            conn, delivery, include_payload=include_payload
        )
        body["envelope"] = envelope.as_dict(include_payload=include_payload)
    return body


@router.post("/delivery/{delivery_id}/ack")
async def acknowledge(
    delivery_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Confirm the target applied this envelope; the watermark advances."""
    await _gate(conn, client, animus)
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
    delivery_id: str,
    body: RejectRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
) -> dict:
    """Report that the target could not apply it; the watermark stays put."""
    await _gate(conn, client, animus)
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
