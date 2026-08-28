"""Named-trigger tools.

Added 2026-08-26, alongside PR #11 on the bridge itself. A trigger's
condition is an ordinary named query (a SELECT projecting ?focus) and its
action is an ordinary named rule bound with $this=focus -- no new query
dialect, no new rule mechanism. StateTrigger fires from a fluent.py hook
after a confirmed transition; TemporalTrigger fires from the scheduler's
periodic "trigger-sweep" maintenance job. A reviewRequired=true trigger's
firing stages its rule's literal output as a Turtle candidate (see
``tools/candidates.py``) instead of writing it immediately.

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
