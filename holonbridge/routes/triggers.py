"""Named-trigger and candidate-review routes.

Mirrors ``routes/named_rules.py``'s shape deliberately — list/get/reload
follow the same registry-cache pattern, and manual evaluation follows the
same dry-run-first convention ``run_named_rule`` established.

Not yet Toolset/persona-reachability-gated, unlike ``named-queries`` and
``named-rules`` since PR #9 — the automatic firing path (``evaluate_triggers``,
called from ``fluent.py`` and the scheduler's maintenance job) runs as a
system process on nobody's behalf, so reachability filtering only has
anything to gate on these read/manual-evaluate routes. Deferred here the
same way PR #9's Tier 3 was deferred, not silently dropped — routes below
still require ``AnimusDep``, they just don't yet narrow by persona.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..deps import AnimusDep, ClientDep, ConnDep, RegistryDep
from ..fuseki import FusekiError
from ..triggers import (
    CandidateError,
    TRIGGER_KINDS,
    TRIGGER_STATUSES,
    approve_candidate,
    evaluate_triggers,
    get_candidate,
    list_candidates,
    load_named_triggers,
    reject_candidate,
)

router = APIRouter(tags=["named-triggers"])
KIND = "named-triggers"


class EvaluateRequest(BaseModel):
    touched_predicates: list[str] | None = Field(
        default=None,
        description="Optional predicate IRIs to narrow evaluation to triggers "
        "that declared at least one matching watchedPredicate.",
    )


def _not_found(trigger_id: str, available_ids: list[str]) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"error": "unknown_named_trigger", "id": trigger_id, "available": available_ids},
    )


@router.get("/named-triggers")
async def list_named_triggers(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
    trigger_status: str | None = Query(default=None, pattern="^(Active|Suspended|Deprecated)$"),
    refresh: bool = Query(default=False),
) -> dict:
    result = await cache.get(
        client, conn, kind=KIND, loader=load_named_triggers, refresh=refresh
    )
    triggers = result.triggers
    if trigger_status:
        triggers = [t for t in triggers if t.status == trigger_status]
    return {
        "dataset": conn.dataset,
        "graph": conn.graph("named-triggers"),
        "count": len(triggers),
        "triggerKinds": list(TRIGGER_KINDS),
        "statuses": list(TRIGGER_STATUSES),
        "triggers": [t.summary() for t in triggers],
        "warnings": result.warnings,
    }


@router.get("/named-trigger/{trigger_id}")
async def get_named_trigger(
    trigger_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep, cache: RegistryDep
) -> dict:
    result = await cache.get(client, conn, kind=KIND, loader=load_named_triggers)
    trig = result.by_id(trigger_id)
    if trig is None:
        raise _not_found(trigger_id, [t.id for t in result.triggers])
    return trig.summary()


@router.post("/named-trigger/{trigger_id}/evaluate")
async def evaluate_named_trigger(
    trigger_id: str,
    body: EvaluateRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    cache: RegistryDep,
) -> dict:
    """Evaluate one trigger on demand, out of band from its normal firing
    path (a fluent write for a StateTrigger, a scheduler sweep for a
    TemporalTrigger) — for testing a newly registered trigger, or
    re-checking one manually."""
    result = await cache.get(client, conn, kind=KIND, loader=load_named_triggers)
    trig = result.by_id(trigger_id)
    if trig is None:
        raise _not_found(trigger_id, [t.id for t in result.triggers])

    touched = frozenset(body.touched_predicates) if body.touched_predicates else None
    try:
        all_firings = await evaluate_triggers(
            client, conn, kind=trig.trigger_kind, touched_predicates=touched
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    firings = [f for f in all_firings if f.trigger_id == trig.id]
    return {"triggerId": trig.id, "count": len(firings), "firings": [f.as_dict() for f in firings]}


@router.post("/named-triggers/reload")
async def reload_named_triggers(conn: ConnDep, client: ClientDep, cache: RegistryDep) -> dict:
    """Registry cache maintenance, not tool access — same reasoning as the
    matching route in named_queries.py / named_rules.py."""
    cache.invalidate(conn, KIND)
    result = await cache.get(client, conn, kind=KIND, loader=load_named_triggers, refresh=True)
    return {"ok": True, "dataset": conn.dataset, "count": len(result.triggers), "warnings": result.warnings}


# --- candidate review queue ----------------------------------------------------


@router.get("/candidates")
async def list_candidate_proposals(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    candidate_status: str | None = Query(default=None, pattern="^(Pending|Approved|Rejected)$"),
) -> dict:
    candidates = await list_candidates(client, conn, status=candidate_status)
    return {
        "dataset": conn.dataset,
        "graph": conn.graph("candidates"),
        "count": len(candidates),
        "candidates": [c.summary() for c in candidates],
    }


@router.get("/candidate/{candidate_id}")
async def get_candidate_proposal(
    candidate_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    candidate_iri = conn.scoped("candidates", candidate_id)
    candidate = await get_candidate(client, conn, candidate_iri)
    if candidate is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_candidate", "id": candidate_id}
        )
    return candidate.detail()


@router.post("/candidate/{candidate_id}/approve")
async def approve_candidate_proposal(
    candidate_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    candidate_iri = conn.scoped("candidates", candidate_id)
    candidate = await get_candidate(client, conn, candidate_iri)
    if candidate is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_candidate", "id": candidate_id}
        )
    try:
        await approve_candidate(client, conn, candidate)
    except CandidateError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "candidate_not_pending", "message": str(exc)}
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "id": candidate_id, "status": "Approved"}


@router.post("/candidate/{candidate_id}/reject")
async def reject_candidate_proposal(
    candidate_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    candidate_iri = conn.scoped("candidates", candidate_id)
    candidate = await get_candidate(client, conn, candidate_iri)
    if candidate is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_candidate", "id": candidate_id}
        )
    try:
        await reject_candidate(client, conn, candidate)
    except CandidateError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "candidate_not_pending", "message": str(exc)}
        ) from exc
    return {"ok": True, "id": candidate_id, "status": "Rejected"}
