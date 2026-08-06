"""Scheduler routes.

Every route here works against the admin dataset, whatever the caller has
selected. The scheduler is process-level: one registry, one provenance trail.
A caller with `X-Dataset-Override` set to their own dataset must not get a
different task list, or a different rate-limit count.

That is deliberate and worth stating in the responses, so the pinning is
visible rather than surprising.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ..deps import ClientDep, ConnDep, SettingsDep
from ..fuseki import FusekiError
from ..params import ParameterError, render_term
from ..scheduler import Scheduler, Task, task_iri
from ..scheduler.model import validate_task_fields
from ..scheduler.store import SchedulerStore
from ..scheduler.vocab import (
    ACTION_CLASSES,
    OUTCOMES,
    SCHEDULER_GRAPHS,
    TASK_STATUSES,
)

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


def _scheduler(request: Request) -> Scheduler:
    scheduler: Scheduler | None = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "scheduler_disabled",
                "message": "set SCHEDULER_ENABLED=true to enable the scheduler",
            },
        )
    return scheduler


class CreateTaskRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    action_class: str = Field(default="ReadOnlyQuery")
    interval_seconds: float = Field(default=3600, ge=1)
    status: str = Field(default="Active")
    dataset_scope: str | None = None
    persona: str | None = None
    policy: str | None = None
    label: str | None = None
    description: str | None = None
    execute_at_inception: bool = False
    invocation_command: str | None = None

    sparql: str | None = None
    rule: str | None = None
    pipeline: str | None = None
    projection: str | None = None
    maintenance: str | None = None
    payload: str | None = None
    target_graph: str | None = None


class StatusRequest(BaseModel):
    status: str = Field(..., pattern="^(Active|Suspended|Deprecated)$")


@router.get("/status")
async def scheduler_status(request: Request, conn: ConnDep) -> dict:
    scheduler = _scheduler(request)
    return {
        **scheduler.status(),
        "graphs": list(SCHEDULER_GRAPHS),
        "actionClasses": list(ACTION_CLASSES),
        "outcomes": list(OUTCOMES),
        "callerDataset": conn.dataset,
        "note": "scheduler routes always target the admin dataset",
    }


@router.get("/tasks")
async def list_tasks(
    request: Request,
    task_status: str | None = Query(default=None, pattern="^(Active|Suspended|Deprecated)$"),
) -> dict:
    scheduler = _scheduler(request)
    tasks = scheduler.tasks
    if task_status:
        tasks = [t for t in tasks if t.status == task_status]
    return {
        "dataset": scheduler.status()["dataset"],
        "count": len(tasks),
        "statuses": list(TASK_STATUSES),
        "tasks": [t.summary() for t in tasks],
    }


@router.get("/task/{task_id}")
async def get_task(task_id: str, request: Request) -> dict:
    scheduler = _scheduler(request)
    task = scheduler.task(task_id)
    if task is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_task",
                "id": task_id,
                "available": [t.id for t in scheduler.tasks],
            },
        )
    return task.detail()


@router.post("/task")
async def create_task(
    body: CreateTaskRequest, request: Request, client: ClientDep, settings: SettingsDep
) -> dict:
    scheduler = _scheduler(request)

    declared = [
        name
        for name, value in (
            ("sparql", body.sparql),
            ("rule", body.rule),
            ("pipeline", body.pipeline),
            ("projection", body.projection),
            ("maintenance", body.maintenance),
            ("payload", body.payload),
        )
        if value
    ]
    if len(declared) != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="declare exactly one of sparql, rule, pipeline, projection, "
            "maintenance, payload; "
            f"got {', '.join(declared) or 'none'}",
        )

    try:
        validate_task_fields(
            action_class=body.action_class,
            status=body.status,
            interval_seconds=body.interval_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if scheduler.task(body.id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"task {body.id!r} already exists"
        )

    if body.payload and not body.target_graph:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="a payload task needs a target_graph"
        )

    task = Task(
        iri=task_iri(body.id),
        id=body.id,
        action_class=body.action_class,
        status=body.status,
        interval_seconds=body.interval_seconds,
        execute_at_inception=body.execute_at_inception,
        dataset_scope=body.dataset_scope or "",
        persona_iri=body.persona or "",
        policy_iri=body.policy or "",
        label=body.label or "",
        description=body.description or "",
        sparql=body.sparql or "",
        rule=body.rule or "",
        pipeline=body.pipeline or "",
        projection=body.projection or "",
        maintenance=body.maintenance or "",
        payload=body.payload or "",
        target_graph=body.target_graph or "",
        invocation_command=body.invocation_command or "",
    )

    store = SchedulerStore(client)
    admin = scheduler.admin_conn
    try:
        await store.save_task(admin, task)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    await scheduler.reload()

    warnings: list[str] = []
    if body.persona and body.persona not in await store.personas(admin):
        # Not fatal — the persona may be registered later — but the task will
        # fail its capability gate until it exists, so say so now.
        warnings.append(f"persona <{body.persona}> is not registered")
    if body.action_class == "LLMInvocation":
        warnings.append(
            "LLMInvocation tasks record 'deferred' unless a proposer is configured"
        )

    return {"ok": True, **task.summary(), "warnings": warnings}


@router.post("/task/{task_id}/status")
async def set_task_status(
    task_id: str, body: StatusRequest, request: Request, client: ClientDep
) -> dict:
    scheduler = _scheduler(request)
    task = scheduler.task(task_id)
    if task is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_task", "id": task_id}
        )
    try:
        await SchedulerStore(client).set_task_status(
            scheduler.admin_conn, task, body.status
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "id": task_id, "taskStatus": body.status}


@router.post("/task/{task_id}/fire")
async def fire_task(task_id: str, request: Request) -> dict:
    """Fire a task now, out of band.

    Tagged ``invocationSource: manual`` and deliberately outside the daily
    scheduled allowance — a manual run is an operator decision, not one of the
    task's own automatic firings.
    """
    scheduler = _scheduler(request)
    task = scheduler.task(task_id)
    if task is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail={"error": "unknown_task", "id": task_id}
        )
    record = await scheduler.fire(task, invocation_source="manual")
    if record is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error": "already_running", "id": task_id},
        )
    return record.as_dict()


@router.get("/activity")
async def activity(
    request: Request,
    client: ClientDep,
    since: str | None = Query(
        default=None,
        description="ISO-8601 with a timezone, e.g. 2026-07-27T00:00:00Z",
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    scheduler = _scheduler(request)
    if since:
        try:
            # An unqualified dateTime compares indeterminately against the
            # qualified stamps in provenance whenever the two are within
            # 14 hours — recent windows come back empty while distant ones
            # work. Refuse it rather than silently returning nothing.
            render_term(since, "xsd:dateTime")
        except ParameterError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={"error": "bad_since", "message": str(exc)},
            ) from exc

    records = await SchedulerStore(client).activity(
        scheduler.admin_conn, since=since, limit=limit
    )
    return {"count": len(records), "since": since or "", "activity": records}


@router.get("/quarantine")
async def quarantine(
    request: Request, client: ClientDep, limit: int = Query(default=50, ge=1, le=200)
) -> dict:
    scheduler = _scheduler(request)
    held = await SchedulerStore(client).quarantined(scheduler.admin_conn, limit)
    return {"count": len(held), "quarantined": held}


@router.post("/reload")
async def reload(request: Request) -> dict:
    """Re-read tasks and personas without a restart.

    Without this a newly created task sits inert until the process restarts,
    which is a surprising thing for a scheduler to do.
    """
    scheduler = _scheduler(request)
    return {"ok": True, **(await scheduler.reload())}


@router.post("/tick")
async def tick(request: Request) -> dict:
    """Run one pass immediately, for testing and operator use."""
    scheduler = _scheduler(request)
    fired = await scheduler.tick()
    return {"ok": True, "fired": len(fired), "records": [r.as_dict() for r in fired]}


