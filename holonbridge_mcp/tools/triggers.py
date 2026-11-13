"""Named-trigger tools.

Added 2026-08-26, alongside PR #11 on the bridge itself. A trigger's
condition is an ordinary named query (a SELECT projecting ?focus) and its
action is an ordinary named rule bound with $this=focus -- no new query
dialect, no new rule mechanism. StateTrigger fires from a fluent.py hook
after a confirmed transition; TemporalTrigger fires from the scheduler's
periodic "trigger-sweep" maintenance job. A reviewRequired=true trigger's
firing stages its rule's literal output as a Turtle candidate (see
``tools/candidates.py``) instead of writing it immediately.

CHANGED 2026-11-01: added ``register_named_trigger``,
``set_named_trigger_status`` and ``delete_named_trigger``. Registration is
definer's rights -- the trigger later acts as the bridge, with nobody calling
it -- so the bridge checks at registration that the caller could do what the
trigger will do (see ``routes/triggers.py``).

Not yet Toolset/persona-reachability-gated on the bridge side, same as
named-queries/named-rules were before PR #9 -- routes/triggers.py notes
this as deferred, not dropped.
"""

from __future__ import annotations

from typing import Any

from ..session import mcp, _call


@mcp.tool()
async def list_named_triggers(
    trigger_status: str | None = None, refresh: bool = False
) -> dict:
    """List registered named triggers.

    Filter by ``Active``, ``Suspended``, or ``Deprecated``. Each entry
    reports its ``triggerKind`` (``StateTrigger`` or ``TemporalTrigger``),
    its condition (a named-query id) and action (a named-rule id), and
    whether firing stages to the candidate queue (``reviewRequired: true``)
    or runs the rule directly.
    """
    params: dict[str, Any] = {}
    if trigger_status:
        params["trigger_status"] = trigger_status
    if refresh:
        params["refresh"] = refresh
    return await _call("GET", "/named-triggers", params=params or None)


@mcp.tool()
async def get_named_trigger(trigger_id: str) -> dict:
    """Full definition of one named trigger, including its condition and action."""
    return await _call("GET", f"/named-trigger/{trigger_id}")


@mcp.tool()
async def evaluate_named_trigger(
    trigger_id: str, touched_predicates: list[str] | None = None
) -> dict:
    """Evaluate one trigger on demand, out of band from its normal firing path.

    A StateTrigger normally fires from a fluent transition, a TemporalTrigger
    from the scheduler's periodic sweep — this runs the same condition/action
    logic immediately, for testing a newly registered trigger or re-checking
    one manually. ``touched_predicates`` narrows evaluation to triggers that
    declared a matching ``watchedPredicate``; omit it to evaluate regardless
    of that narrowing.

    A ``reviewRequired`` trigger's firing lands in the candidate queue (see
    ``list_candidates``/``get_candidate``) rather than writing immediately —
    check there for the result, not the target graph, until it's approved.
    """
    return await _call(
        "POST",
        f"/named-trigger/{trigger_id}/evaluate",
        json_body={"touched_predicates": touched_predicates},
    )


@mcp.tool()
async def reload_named_triggers() -> dict:
    """Re-read the named-trigger registry, discarding the cached copy."""
    return await _call("POST", "/named-triggers/reload")


@mcp.tool()
async def register_named_trigger(
    id: str,
    trigger_kind: str,
    condition: str,
    on_fire: str | None = None,
    review_required: bool = True,
    status: str = "Active",
    label: str | None = None,
    description: str | None = None,
    watched_predicates: list[str] | None = None,
    on_fire_agent: str | None = None,
    agent_task: str | None = None,
    agent_context: str | None = None,
    agent_target_graph: str | None = None,
    fire_mode: str = "Edge",
) -> dict:
    """Register (or overwrite) a named trigger.

    ``trigger_kind`` is ``StateTrigger`` (evaluated after a fluent write) or
    ``TemporalTrigger`` (evaluated by the scheduler's trigger-sweep).
    ``condition`` is the id of a registered named query: a SELECT projecting
    ``?focus``. ``on_fire`` is the id of a registered named rule, bound with
    ``$this`` set to each focus node. A focus fires once while its condition
    holds, and again after the condition has gone false and true. That is
    ``fire_mode="Edge"``, the default. ``fire_mode="EveryEvaluation"`` fires for
    every matching focus on every evaluation instead, for work that should be
    redone after each write (a ``Replace``-mode rule rebuilding a projection).
    Rule actions only. With ``review_required`` true it still stages no second
    candidate while one is Pending for that focus, so pair it with
    ``review_required=false`` when the point is to refresh automatically.

    ``review_required`` (default true) stages the rule's output in the
    candidate queue for approval. ``false`` runs the rule for real, through
    its own write mode, with nobody reviewing it first.

    The trigger will run as the bridge, not as you, so the bridge checks now
    what you could do yourself: write access to the trigger registry; read
    access to everything the condition and rule read; and, for
    ``review_required=false``, write access to the rule's target graph (and
    replace access when the rule reconciles wholesale). A condition or rule
    you cannot reach is reported as unknown.

    **Agent action.** Give ``on_fire_agent`` (an agent endpoint id, see
    ``register_agent_endpoint``) *instead of* ``on_fire`` to place a call to an
    external agent for each focus. ``agent_task`` is what it is asked;
    ``agent_context`` is the id of a named SELECT/CONSTRUCT whose result
    travels with the call (bound to the focus if it declares a ``focus``
    anyURI parameter); ``agent_target_graph`` is where its Turtle answer is
    *proposed* (staged as a candidate, never written). Calls are placed as you,
    and wait for a person's approval unless both ``review_required=false`` and
    the endpoint has ``call_review=false``. You need write access to the call
    ledger, the candidates graph and the target graph. Exactly one of
    ``on_fire`` and ``on_fire_agent``.
    """
    return await _call(
        "POST",
        "/named-trigger",
        json_body={
            "id": id,
            "trigger_kind": trigger_kind,
            "condition": condition,
            "on_fire": on_fire,
            "review_required": review_required,
            "status": status,
            "label": label,
            "description": description,
            "watched_predicates": watched_predicates or [],
            "on_fire_agent": on_fire_agent,
            "agent_task": agent_task,
            "agent_context": agent_context,
            "agent_target_graph": agent_target_graph,
            "fire_mode": fire_mode,
        },
    )


@mcp.tool()
async def set_named_trigger_status(trigger_id: str, status: str) -> dict:
    """Set a trigger to ``Active``, ``Suspended`` or ``Deprecated``.

    Making a trigger Active re-runs the registration checks against you: a
    switch that arms something is not a lesser act than registering it.
    """
    return await _call(
        "POST", f"/named-trigger/{trigger_id}/status", json_body={"status": status}
    )


@mcp.tool()
async def delete_named_trigger(trigger_id: str) -> dict:
    """Remove a trigger. Needs replace access to the trigger registry. Its
    firing history stays in the ledger."""
    return await _call("DELETE", f"/named-trigger/{trigger_id}")
