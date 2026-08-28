"""Scheduler tools.

These always target the admin dataset, whatever dataset the session is
otherwise using. One scheduler per process means one registry and one
provenance trail; a per-caller view of either would be a fiction.
"""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def get_scheduler_status() -> dict:
    """Whether the scheduler is running, and its task and persona counts."""
    return await _call("GET", "/scheduler/status")


@mcp.tool()
async def list_scheduled_tasks(task_status: str | None = None) -> dict:
    """List scheduled tasks. Filter by Active, Suspended, or Deprecated."""
    params = {"task_status": task_status} if task_status else None
    return await _call("GET", "/scheduler/tasks", params=params)


@mcp.tool()
async def create_scheduled_task(
    task_id: str,
    action_class: str = "ReadOnlyQuery",
    interval_seconds: float = 3600,
    dataset_scope: str | None = None,
    persona: str | None = None,
    policy: str | None = None,
    sparql: str | None = None,
    rule: str | None = None,
    pipeline: str | None = None,
    projection: str | None = None,
    maintenance: str | None = None,
    payload: str | None = None,
    target_graph: str | None = None,
    label: str | None = None,
) -> dict:
    """Create a scheduled task.

    Declare exactly one action: ``sparql``, ``rule``, ``pipeline``,
    ``projection`` (fire a projection hook), ``maintenance``
    (``projection-sweep``), or ``payload``.
    ``dataset_scope`` is the dataset the task acts on — distinct from the admin
    dataset its definition lives in. A task naming a persona may only perform
    action classes in that persona's capability set.
    """
    return await _call(
        "POST",
        "/scheduler/task",
        json_body={
            "id": task_id,
            "action_class": action_class,
            "interval_seconds": interval_seconds,
            "dataset_scope": dataset_scope,
            "persona": persona,
            "policy": policy,
            "sparql": sparql,
            "rule": rule,
            "pipeline": pipeline,
            "projection": projection,
            "maintenance": maintenance,
            "payload": payload,
            "target_graph": target_graph,
            "label": label,
        },
    )


@mcp.tool()
async def set_task_status(task_id: str, task_status: str) -> dict:
    """Suspend, resume, or deprecate a task. Only Active tasks fire."""
    return await _call(
        "POST", f"/scheduler/task/{task_id}/status", json_body={"status": task_status}
    )


@mcp.tool()
async def fire_scheduled_task(task_id: str) -> dict:
    """Fire a task now, out of band.

    Tagged ``manual`` in provenance and outside the daily scheduled allowance.
    """
    return await _call("POST", f"/scheduler/task/{task_id}/fire")


@mcp.tool()
async def get_recent_scheduler_activity(
    since: str | None = None, limit: int = 50
) -> dict:
    """Recent firing records, most recent first.

    ``since`` must carry a timezone (``2026-07-27T00:00:00Z``). Without one the
    comparison against stored timestamps is indeterminate within ±14 hours, so
    recent windows come back empty while distant ones work; the bridge refuses
    an unqualified value rather than returning a misleading empty list.
    """
    params = {"limit": limit}
    if since:
        params["since"] = since
    return await _call("GET", "/scheduler/activity", params=params)


@mcp.tool()
async def get_quarantined_proposals(limit: int = 50) -> dict:
    """Proposals held back because they failed validation, with their Turtle."""
    return await _call("GET", "/scheduler/quarantine", params={"limit": limit})


@mcp.tool()
async def reload_scheduler() -> dict:
    """Re-read tasks and personas. New tasks are inert until this runs."""
    return await _call("POST", "/scheduler/reload")
