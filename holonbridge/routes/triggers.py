"""Named-trigger and candidate-review routes.

Mirrors ``routes/named_rules.py``'s shape deliberately — list/get/reload
follow the same registry-cache pattern, and manual evaluation follows the
same dry-run-first convention ``run_named_rule`` established.

CHANGED 2026-11-01: added registration -- ``POST /named-trigger``,
``POST /named-trigger/{id}/status`` and ``DELETE /named-trigger/{id}`` --
and gated manual evaluation. Until now a trigger could only be installed by
pushing hand-authored Turtle, with nothing checking that the caller could do
what the trigger would later do on its own. A trigger is definer's rights:
once registered, the bridge evaluates its condition and runs (or stages) its
rule as itself, so every check has to happen here, at registration, because
there is no caller at firing time. Registering needs:

* ``check_write`` on the named-triggers graph (installing something at all);
* a condition that is a named SELECT and a rule that CONSTRUCTs, both
  reachable from the caller's persona (an unreachable id is reported as
  unknown, as everywhere else) and both readable by the caller -- the
  trigger would otherwise read, as the bridge, what the caller cannot;
* for an auto-executing trigger (``review_required`` false), ``check_write``
  on the rule's target graph, and ``check_replace`` as well when the rule
  reconciles wholesale (Replace/Sync/Supersede). A reviewed trigger only
  stages a candidate, and approving that is its own gated write.

Deleting needs ``check_replace`` on the registry, as for named queries.
Re-activating a trigger re-runs the same checks, so a Suspended auto-executing
trigger cannot be switched back on by someone who could not have registered it.

Manual evaluation used to run every Active trigger of the same kind and merely
filter the answer; it now runs only the trigger asked for, and needs the same
write access the trigger would exercise on its own.

Not yet Toolset/persona-reachability-gated, unlike ``named-queries`` and
``named-rules`` since PR #9 — the automatic firing path (``evaluate_triggers``,
called from ``fluent.py`` and the scheduler's maintenance job) runs as a
system process on nobody's behalf, so reachability filtering only has
anything to gate on these read/manual-evaluate routes. Deferred here the
same way PR #9's Tier 3 was deferred, not silently dropped — routes below
still require ``AnimusDep``, they just don't yet narrow by persona.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import audit
from ..acl import authorize_query, check_replace, check_write, is_admin
from ..deps import AnimusDep, ClientDep, ConnDep, PersonasDep, RegistryDep
from ..fuseki import FusekiError
from ..named_queries import apply_query_params, load_named_queries
from ..named_rules import RuleError, bind_rule, load_named_rules
from ..params import ParameterError
from ..sparql_kind import form
from ..toolset import resolve_reachable
from ..trigger_registry import (
    InvalidTrigger,
    deletion_update,
    registration_update,
    render_trigger_turtle,
    status_update,
    trigger_node,
    validate_watched_predicates,
)
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


class RegisterTriggerRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    trigger_kind: str = Field(..., description="StateTrigger or TemporalTrigger")
    condition: str = Field(..., min_length=1, description="id of a named SELECT projecting ?focus")
    on_fire: str = Field(..., min_length=1, description="id of a named rule, bound with $this=focus")
    review_required: bool = Field(
        default=True,
        description="true stages the rule's output as a candidate for review; "
        "false runs the rule for real, through its own write mode",
    )
    status: str = Field(default="Active", pattern="^(Active|Suspended|Deprecated)$")
    label: str | None = None
    description: str | None = None
    watched_predicates: list[str] = Field(default_factory=list)


class TriggerStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(Active|Suspended|Deprecated)$")


#: Stands in for the focus node when a rule is bound only to be inspected.
_PROBE_FOCUS = "urn:holonbridge:trigger-probe"

#: Write modes that reconcile a whole graph rather than add to it.
_WHOLESALE_MODES = {"replace", "sync", "supersede"}


def _not_found(trigger_id: str, available_ids: list[str]) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"error": "unknown_named_trigger", "id": trigger_id, "available": available_ids},
    )



# --- access -------------------------------------------------------------------


def _query_fn(client, conn):
    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    return query_fn


def _identified(animus) -> str:
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    return animus.person


async def _require_grant(conn, client, animus, graph: str, *, replace: bool = False) -> None:
    person = _identified(animus)
    check = check_replace if replace else check_write
    decision = await check(_query_fn(client, conn), conn.holons_graph, person=person, target=graph)
    if not decision.allowed:
        audit.annotate(reason=decision.reason)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"{decision.reason} (graph: {graph})"
        )


async def _resolve_parts(conn, client, animus, personas, condition_id: str, rule_id: str):
    """The condition query and action rule a trigger names, if both exist and
    this caller can reach them. Unreachable reads as unknown, like every other
    registry, so a restricted tool never confirms its own existence."""
    person = _identified(animus)
    queries = await load_named_queries(client, conn)
    rules = await load_named_rules(client, conn)
    condition = queries.by_id(condition_id)
    rule = rules.by_id(rule_id)

    persona, _source = personas.get(person_id=person, dataset=conn.dataset)
    candidates = {item.iri: ("condition", item.id) for item in queries.queries}
    candidates.update({item.iri: ("rule", item.id) for item in rules.rules})
    reachable = await resolve_reachable(
        _query_fn(client, conn), conn, persona=persona, candidate_iris=list(candidates)
    )
    reachable_queries = sorted(candidates[i][1] for i in reachable if candidates[i][0] == "condition")
    reachable_rules = sorted(candidates[i][1] for i in reachable if candidates[i][0] == "rule")

    if condition is None or condition.iri not in reachable:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_condition", "id": condition_id, "available": reachable_queries},
        )
    if rule is None or rule.iri not in reachable:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_rule", "id": rule_id, "available": reachable_rules},
        )
    return condition, rule


async def _authorize_definition(
    conn, client, animus, personas, *, condition, rule, review_required: bool
) -> list[str]:
    """Everything a trigger would later do as the bridge, checked against the
    caller who is defining it. Returns warnings (non-fatal observations)."""
    person = _identified(animus)
    query_fn = _query_fn(client, conn)
    warnings: list[str] = []

    try:
        condition_sparql = apply_query_params(condition, {}).sparql
    except ParameterError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "condition_needs_parameters", "message": str(exc)},
        ) from exc
    if form(condition_sparql) != "SELECT":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "condition_not_select",
                "message": f"condition {condition.id!r} is a {form(condition_sparql)}; "
                "a trigger condition must be a SELECT projecting ?focus",
            },
        )
    if "?focus" not in condition_sparql and "$focus" not in condition_sparql:
        warnings.append(f"condition {condition.id!r} never mentions ?focus; it will match nothing")
    if re.search(r"\bLIMIT\b", condition_sparql, re.IGNORECASE):
        warnings.append(
            f"condition {condition.id!r} carries a LIMIT; a truncated result looks like a "
            "falling edge and re-arms the rows it cut off"
        )

    try:
        rule_sparql = bind_rule(rule, {"$this": _PROBE_FOCUS})
    except (RuleError, ParameterError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "rule_not_bindable", "message": str(exc)},
        ) from exc
    if form(rule_sparql) not in {"CONSTRUCT", "DESCRIBE"}:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "rule_not_construct",
                "message": f"rule {rule.id!r} is a {form(rule_sparql)}; a rule must CONSTRUCT",
            },
        )
    if not rule.runnable:
        warnings.append(f"rule {rule.id!r} is {rule.status.lower()}; the trigger will fail until it is Active")

    # The trigger reads both as the bridge, so the definer must be able to.
    if not await is_admin(query_fn, conn.holons_graph, person=person):
        for label, sparql, ident in (
            ("condition", condition_sparql, condition.id),
            ("rule", rule_sparql, rule.id),
        ):
            decision = await authorize_query(
                query_fn,
                conn.holons_graph,
                person=person,
                sparql_text=sparql,
                persona_of_graph=conn.persona_for_graph,
            )
            if not decision.allowed:
                audit.annotate(reason=decision.reason)
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": f"{label}_not_readable",
                        "message": f"{label} {ident!r} reads something this caller may not read: "
                        f"{decision.reason}",
                    },
                )

    await _authorize_firing(conn, client, animus, rule=rule, review_required=review_required)
    return warnings


async def _authorize_firing(conn, client, animus, *, rule, review_required: bool) -> None:
    """The write a firing performs. A reviewed trigger only stages a candidate
    (approving it is its own gated write); an auto-executing one writes the
    rule's target graph through the rule's own write mode."""
    if review_required:
        await _require_grant(conn, client, animus, conn.graph("candidates"))
        return
    target = rule.target_graph
    if not target:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "rule_has_no_target",
                "message": f"rule {rule.id!r} names no target graph, so it cannot run unreviewed",
            },
        )
    await _require_grant(conn, client, animus, target)
    if rule.write_mode.lower() in _WHOLESALE_MODES:
        await _require_grant(conn, client, animus, target, replace=True)


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
    re-checking one manually.

    Runs only this trigger, and needs the write access a firing exercises: the
    candidates graph for a reviewed trigger, the rule's target graph for an
    auto-executing one."""
    result = await cache.get(client, conn, kind=KIND, loader=load_named_triggers)
    trig = result.by_id(trigger_id)
    if trig is None:
        raise _not_found(trigger_id, [t.id for t in result.triggers])

    rules = await load_named_rules(client, conn)
    rule = rules.by_id(trig.on_fire_rule_id)
    if rule is not None:
        await _authorize_firing(
            conn, client, animus, rule=rule, review_required=trig.review_required
        )
    else:
        _identified(animus)

    touched = frozenset(body.touched_predicates) if body.touched_predicates else None
    try:
        firings = await evaluate_triggers(
            client,
            conn,
            kind=trig.trigger_kind,
            touched_predicates=touched,
            trigger_ids=frozenset({trig.id}),
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"triggerId": trig.id, "count": len(firings), "firings": [f.as_dict() for f in firings]}


# --- registration ---------------------------------------------------------------


@router.post("/named-trigger")
async def register_named_trigger(
    body: RegisterTriggerRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
) -> dict:
    """Register (or overwrite) a named trigger. See this module's docstring for
    the checks, and why they happen here and not at firing time."""
    registry = conn.graph("named-triggers")
    await _require_grant(conn, client, animus, registry)

    try:
        watched = validate_watched_predicates(body.watched_predicates)
    except InvalidTrigger as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_trigger", "message": str(exc)}
        ) from exc
    if body.trigger_kind not in TRIGGER_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_trigger_kind", "expected": list(TRIGGER_KINDS)},
        )

    condition, rule = await _resolve_parts(
        conn, client, animus, personas, body.condition, body.on_fire
    )
    warnings = await _authorize_definition(
        conn, client, animus, personas,
        condition=condition, rule=rule, review_required=body.review_required,
    )

    node = trigger_node(conn, body.id)
    try:
        turtle = render_trigger_turtle(
            node=node,
            trigger_id=body.id,
            trigger_kind=body.trigger_kind,
            condition=condition.id,
            on_fire=rule.id,
            review_required=body.review_required,
            status=body.status,
            label=body.label,
            description=body.description,
            watched_predicates=watched,
            registered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except InvalidTrigger as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_trigger", "message": str(exc)}
        ) from exc

    audit.annotate(graphs=[registry])
    try:
        await client.update(conn, registration_update(registry, node, turtle))
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    cache.invalidate(conn, KIND)
    return {
        "ok": True,
        "id": body.id,
        "iri": node,
        "graph": registry,
        "triggerKind": body.trigger_kind,
        "reviewRequired": body.review_required,
        "triggerStatus": body.status,
        "warnings": warnings,
    }


@router.post("/named-trigger/{trigger_id}/status")
async def set_named_trigger_status(
    trigger_id: str,
    body: TriggerStatusRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
) -> dict:
    """Suspend, resume or deprecate a trigger. Making one Active again re-runs
    the registration checks: a switch that arms something is not a lesser act
    than registering it."""
    registry = conn.graph("named-triggers")
    await _require_grant(conn, client, animus, registry)

    result = await cache.get(client, conn, kind=KIND, loader=load_named_triggers)
    trig = result.by_id(trigger_id)
    if trig is None:
        raise _not_found(trigger_id, [t.id for t in result.triggers])

    warnings: list[str] = []
    if body.status == "Active":
        condition, rule = await _resolve_parts(
            conn, client, animus, personas, trig.condition_id, trig.on_fire_rule_id
        )
        warnings = await _authorize_definition(
            conn, client, animus, personas,
            condition=condition, rule=rule, review_required=trig.review_required,
        )

    audit.annotate(graphs=[registry])
    try:
        await client.update(conn, status_update(registry, trig.iri, body.status))
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    cache.invalidate(conn, KIND)
    return {"ok": True, "id": trigger_id, "triggerStatus": body.status, "warnings": warnings}


@router.delete("/named-trigger/{trigger_id}")
async def delete_named_trigger(
    trigger_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep, cache: RegistryDep
) -> dict:
    """Remove a trigger. ``check_replace`` on the registry, the same tier as
    deleting a named query. Its firing ledger entries stay: they are history."""
    registry = conn.graph("named-triggers")
    await _require_grant(conn, client, animus, registry, replace=True)

    result = await cache.get(client, conn, kind=KIND, loader=load_named_triggers)
    trig = result.by_id(trigger_id)
    if trig is None:
        raise _not_found(trigger_id, [t.id for t in result.triggers])

    audit.annotate(graphs=[registry])
    try:
        await client.update(conn, deletion_update(registry, trig.iri))
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    cache.invalidate(conn, KIND)
    return {"ok": True, "id": trigger_id, "graph": registry}


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
    """CHANGED: ``approve_candidate`` now routes through the ordinary
    ACL/SHACL-gated write path (see that function's own docstring), so
    this now also passes ``animus`` through and lets a 401/403/422/502
    ``HTTPException`` raised from inside it propagate as-is -- it is
    already the exact response FastAPI should return, not something to
    re-wrap. ``CandidateError``/``FusekiError`` stay handled explicitly:
    the former for "already resolved", the latter for a failure in the
    status-update write that follows the merge.
    """
    candidate_iri = conn.scoped("candidates", candidate_id)
    candidate = await get_candidate(client, conn, candidate_iri)
    if candidate is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_candidate", "id": candidate_id}
        )
    try:
        result = await approve_candidate(client, conn, candidate, animus=animus)
    except CandidateError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "candidate_not_pending", "message": str(exc)}
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"id": candidate_id, "status": "Approved", **result}


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
