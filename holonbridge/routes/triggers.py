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

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import audit
from ..agents.registry import load_endpoints as load_agent_endpoints
from ..agents.service import authorize_request
from ..agents.trigger_action import authorize_agent_trigger
from ..deps import AnimusDep, ClientDep, ConnDep, PersonasDep, RegistryDep, SettingsDep
from ..fuseki import FusekiError
from ..named_queries import load_named_queries
from ..named_rules import load_named_rules
from ..fluent_trigger_action import authorize_fluent_trigger
from ..toolset import resolve_reachable
from ..trigger_authz import (
    TriggerDenied,
    authorize_definition,
    authorize_firing,
    require_grant,
)
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
    on_fire: str | None = Field(
        default=None,
        description="id of a named rule, bound with $this=focus. Exactly one of on_fire "
        "and on_fire_agent.",
    )
    on_fire_agent: str | None = Field(
        default=None,
        description="id of an agent endpoint: instead of running a rule, place a call to it "
        "for each matching focus (see agent_task).",
    )
    on_fire_fluent_query: str | None = Field(
        default=None,
        description="id of a named SELECT whose rows (?fluent, ?value, optional ?operation, ?isIri, "
        "?assertedAt, ?description), bound to the focus, are applied as fluent updates by the bridge, "
        "as you. Needs review_required=false. Exactly one of on_fire, on_fire_agent and "
        "on_fire_fluent_query.",
    )
    agent_task: str | None = Field(default=None, description="what to ask the agent (on_fire_agent only)")
    agent_context: str | None = Field(
        default=None,
        description="id of a named SELECT/CONSTRUCT run when each call is placed and sent "
        "with it; bound to the focus if it declares a 'focus' anyURI parameter",
    )
    agent_target_graph: str | None = Field(
        default=None, description="graph the agent's Turtle answer is proposed for (staged, never written)"
    )
    review_required: bool = Field(
        default=True,
        description="true stages the rule's output as a candidate for review (or, for an agent, "
        "holds each call for a person's approval); false runs the rule for real, through its own "
        "write mode (or lets a call to an endpoint with callReview=false be sent by the "
        "scheduler)",
    )
    fire_mode: str = Field(
        default="Edge",
        pattern="^(Edge|EveryEvaluation)$",
        description="Edge (default): fire when a focus starts matching and again only after it has "
        "stopped and started matching. EveryEvaluation: fire for every matching focus on every "
        "evaluation (rule actions only), e.g. to rebuild a projection after each write.",
    )
    status: str = Field(default="Active", pattern="^(Active|Suspended|Deprecated)$")
    label: str | None = None
    description: str | None = None
    watched_predicates: list[str] = Field(default_factory=list)


class TriggerStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(Active|Suspended|Deprecated)$")


def _not_found(trigger_id: str, available_ids: list[str]) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"error": "unknown_named_trigger", "id": trigger_id, "available": available_ids},
    )



# --- access -------------------------------------------------------------------
#
# The checks themselves live in ``trigger_authz`` so the firing path can ask the
# same questions of the person who armed a trigger. These are the HTTP-shaped
# wrappers: a TriggerDenied becomes the response it describes.


def _query_fn(client, conn):
    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    return query_fn


def _identified(animus) -> str:
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")
    return animus.person


def _http(exc: TriggerDenied) -> HTTPException:
    if exc.status in (401, 403):
        audit.annotate(reason=exc.reason)
    return HTTPException(exc.status, detail=exc.detail)


async def _require_grant(conn, client, animus, graph: str, *, replace: bool = False) -> None:
    person = _identified(animus)
    try:
        await require_grant(_query_fn(client, conn), conn, person, graph, replace=replace)
    except TriggerDenied as exc:
        raise _http(exc) from exc


async def _authorize_firing(conn, client, animus, *, rule, review_required: bool) -> None:
    person = _identified(animus)
    try:
        await authorize_firing(
            _query_fn(client, conn), conn, person, rule=rule, review_required=review_required
        )
    except TriggerDenied as exc:
        raise _http(exc) from exc


async def _authorize_definition(
    conn, client, animus, personas, *, condition, rule, review_required: bool
) -> list[str]:
    person = _identified(animus)
    try:
        return await authorize_definition(
            _query_fn(client, conn),
            conn,
            person,
            condition=condition,
            rule=rule,
            review_required=review_required,
        )
    except TriggerDenied as exc:
        raise _http(exc) from exc


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


async def _resolve_agent_parts(
    conn, client, animus, personas, condition_id: str, endpoint_id: str, context_id: str | None
):
    """The condition, agent endpoint and optional context query an agent trigger
    names. Condition and context are subject to the same reachability as rules."""
    person = _identified(animus)
    queries = await load_named_queries(client, conn)
    endpoints = await load_agent_endpoints(client, conn)
    condition = queries.by_id(condition_id)
    context = queries.by_id(context_id) if context_id else None

    persona, _source = personas.get(person_id=person, dataset=conn.dataset)
    reachable = await resolve_reachable(
        _query_fn(client, conn), conn, persona=persona,
        candidate_iris=[q.iri for q in queries.queries],
    )
    available = sorted(q.id for q in queries.queries if q.iri in reachable)
    if condition is None or condition.iri not in reachable:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_condition", "id": condition_id, "available": available},
        )
    if context_id and (context is None or context.iri not in reachable):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_context_query", "id": context_id, "available": available},
        )
    endpoint = endpoints.by_id(endpoint_id)
    if endpoint is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unknown_agent_endpoint",
                "id": endpoint_id,
                "available": sorted(e.id for e in endpoints.endpoints),
            },
        )
    return condition, endpoint, context


async def _resolve_fluent_parts(conn, client, animus, personas, condition_id: str, action_id: str):
    """The condition and action query a fluent trigger names; both must exist and
    be reachable by this caller (unreachable reads as unknown)."""
    person = _identified(animus)
    queries = await load_named_queries(client, conn)
    condition = queries.by_id(condition_id)
    action = queries.by_id(action_id)

    persona, _source = personas.get(person_id=person, dataset=conn.dataset)
    reachable = await resolve_reachable(
        _query_fn(client, conn), conn, persona=persona,
        candidate_iris=[q.iri for q in queries.queries],
    )
    available = sorted(q.id for q in queries.queries if q.iri in reachable)
    if condition is None or condition.iri not in reachable:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_condition", "id": condition_id, "available": available},
        )
    if action is None or action.iri not in reachable:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_action_query", "id": action_id, "available": available},
        )
    return condition, action


async def _authorize_fluent_definition(
    conn, client, animus, *, condition, action, review_required: bool
) -> list[str]:
    person = _identified(animus)
    try:
        return await authorize_fluent_trigger(
            _query_fn(client, conn), conn, person,
            condition=condition, action_query=action, review_required=review_required,
        )
    except TriggerDenied as exc:
        raise _http(exc) from exc


async def _authorize_agent_definition(
    conn, client, animus, settings, *, condition, endpoint, context, target_graph, review_required: bool
) -> list[str]:
    person = _identified(animus)
    try:
        return await authorize_agent_trigger(
            _query_fn(client, conn), conn, person,
            condition=condition, endpoint=endpoint, endpoint_id=endpoint.id,
            context_query=context, target_graph=target_graph or "",
            review_required=review_required,
            settings=settings,
        )
    except TriggerDenied as exc:
        raise _http(exc) from exc


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

    if trig.is_fluent:
        # A fluent trigger writes events, scene and sequences: the same three
        # graphs POST /fluent/update asks of a caller.
        for graph in (conn.graph("events"), conn.scene_graph, conn.graph("sequences")):
            await _require_grant(conn, client, animus, graph)
    elif trig.is_agent:
        # An agent trigger writes to the call ledger (and, if it asks for an
        # answer, the candidates graph and the target graph).
        person = _identified(animus)
        try:
            await authorize_request(
                _query_fn(client, conn), conn, person,
                target_graph=trig.agent_target_graph or None,
            )
        except TriggerDenied as exc:
            raise _http(exc) from exc
    else:
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
    settings: SettingsDep,
) -> dict:
    """Register (or overwrite) a named trigger. See this module's docstring for
    the checks, and why they happen here and not at firing time."""
    registry = conn.graph("named-triggers")
    await _require_grant(conn, client, animus, registry)

    if sum(bool(x) for x in (body.on_fire, body.on_fire_agent, body.on_fire_fluent_query)) != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_trigger",
                "message": "name exactly one action: on_fire (a rule), on_fire_agent (an agent "
                "endpoint) or on_fire_fluent_query (a query whose rows update fluents)",
            },
        )

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

    rule = endpoint = context = action = None
    if body.on_fire_fluent_query:
        condition, action = await _resolve_fluent_parts(
            conn, client, animus, personas, body.condition, body.on_fire_fluent_query
        )
        warnings = await _authorize_fluent_definition(
            conn, client, animus,
            condition=condition, action=action, review_required=body.review_required,
        )
    elif body.on_fire_agent:
        condition, endpoint, context = await _resolve_agent_parts(
            conn, client, animus, personas, body.condition, body.on_fire_agent, body.agent_context
        )
        warnings = await _authorize_agent_definition(
            conn, client, animus, settings,
            condition=condition, endpoint=endpoint, context=context,
            target_graph=body.agent_target_graph, review_required=body.review_required,
        )
    else:
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
            on_fire=rule.id if rule else None,
            fluent_query=action.id if action else None,
            agent_endpoint=endpoint.id if endpoint else None,
            agent_task=body.agent_task,
            agent_context=context.id if context else None,
            agent_target_graph=body.agent_target_graph,
            review_required=body.review_required,
            status=body.status,
            label=body.label,
            description=body.description,
            watched_predicates=watched,
            registered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            armed_by=animus.person,
            fire_mode=body.fire_mode,
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
        "fireMode": body.fire_mode,
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
    settings: SettingsDep,
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
    if body.status == "Active" and trig.is_fluent:
        condition, action = await _resolve_fluent_parts(
            conn, client, animus, personas, trig.condition_id, trig.fluent_query
        )
        warnings = await _authorize_fluent_definition(
            conn, client, animus,
            condition=condition, action=action, review_required=trig.review_required,
        )
    elif body.status == "Active" and trig.is_agent:
        condition, endpoint, context = await _resolve_agent_parts(
            conn, client, animus, personas, trig.condition_id, trig.agent_endpoint,
            trig.agent_context or None,
        )
        warnings = await _authorize_agent_definition(
            conn, client, animus, settings,
            condition=condition, endpoint=endpoint, context=context,
            target_graph=trig.agent_target_graph, review_required=trig.review_required,
        )
    elif body.status == "Active":
        condition, rule = await _resolve_parts(
            conn, client, animus, personas, trig.condition_id, trig.on_fire_rule_id
        )
        warnings = await _authorize_definition(
            conn, client, animus, personas,
            condition=condition, rule=rule, review_required=trig.review_required,
        )

    audit.annotate(graphs=[registry])
    try:
        await client.update(
            conn,
            status_update(
                registry,
                trig.iri,
                body.status,
                # Arming a trigger makes its access yours to answer for.
                armed_by=animus.person if body.status == "Active" else None,
            ),
        )
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


class DecisionBody(BaseModel):
    """Optional body of an approve or reject call."""

    reason: str | None = Field(default=None, max_length=1000)
    shapes_graph: str | None = Field(
        default=None,
        description=(
            "approve only: the shapes graph to validate the merge against. Omit to use the "
            "dataset's own shapes graph when the deployment sets SHACL_REQUIRED (otherwise no "
            "validation); an empty string skips validation for this approval; any other "
            "value is used as given."
        ),
    )


@router.post("/candidate/{candidate_id}/approve")
async def approve_candidate_proposal(
    candidate_id: str,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    settings: SettingsDep,
    body: DecisionBody | None = None,
) -> dict:
    """CHANGED 2026-11-13: SHACL on approval follows ``SHACL_REQUIRED`` /
    ``SHACL_DELTA`` like every other write path, and the body may name a
    ``shapes_graph`` (``""`` opts out). A violation is a 422 and the candidate
    stays Pending.

    CHANGED: ``approve_candidate`` now routes through the ordinary
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
        result = await approve_candidate(
            client,
            conn,
            candidate,
            animus=animus,
            reason=body.reason if body else None,
            shapes_graph=body.shapes_graph if body else None,
            shacl_required=settings.shacl_required,
            shacl_delta=settings.shacl_delta,
        )
    except CandidateError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "candidate_not_pending", "message": str(exc)}
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"id": candidate_id, "status": "Approved", **result}


@router.post("/candidate/{candidate_id}/reject")
async def reject_candidate_proposal(
    candidate_id: str,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    body: DecisionBody | None = None,
) -> dict:
    candidate_iri = conn.scoped("candidates", candidate_id)
    candidate = await get_candidate(client, conn, candidate_iri)
    if candidate is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_candidate", "id": candidate_id}
        )
    try:
        await reject_candidate(
            client, conn, candidate, animus=animus, reason=body.reason if body else None
        )
    except CandidateError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"error": "candidate_not_pending", "message": str(exc)}
        ) from exc
    return {"ok": True, "id": candidate_id, "status": "Rejected"}
