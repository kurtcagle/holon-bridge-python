"""Scheduler domain model."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .vocab import ACTION_CLASSES, SCHED, TASK_STATUSES, TRIGGER_TEMPORAL


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime | None = None) -> str:
    """ISO-8601 with an explicit offset, always.

    A timestamp without a timezone compares indeterminately against qualified
    ones within ±14 hours, so a "firings today" count can silently come back
    empty. Everything the scheduler writes is qualified, and everything it
    accepts must be too.
    """
    return (moment or now()).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Policy:
    """An ODRL permission carrying a daily count constraint."""

    iri: str
    count: int | None = None
    version: str = ""

    @property
    def unlimited(self) -> bool:
        return self.count is None


@dataclass
class Persona:
    iri: str
    id: str
    label: str = ""
    model: str = ""
    dataset_scope: str = ""
    policy_iri: str = ""
    capabilities: list[str] = field(default_factory=list)

    def can(self, action_class: str) -> bool:
        return action_class in self.capabilities

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "label": self.label or self.id,
            "model": self.model,
            "datasetScope": self.dataset_scope,
            "policy": self.policy_iri,
            "capabilities": self.capabilities,
        }


@dataclass
class Task:
    iri: str
    id: str
    action_class: str = "ReadOnlyQuery"
    status: str = "Active"
    trigger_type: str = TRIGGER_TEMPORAL
    interval_seconds: float = 3600.0
    execute_at_inception: bool = False
    dataset_scope: str = ""
    persona_iri: str = ""
    policy_iri: str = ""
    label: str = ""
    description: str = ""

    # What the task actually does — exactly one should be set.
    sparql: str = ""
    rule: str = ""
    pipeline: str = ""
    projection: str = ""
    maintenance: str = ""
    payload: str = ""
    target_graph: str = ""

    invocation_command: str = ""
    last_fired: str = ""

    @property
    def active(self) -> bool:
        return self.status == "Active"

    @property
    def action(self) -> str:
        """What this task will do, derived from which field is populated."""
        if self.sparql:
            return "sparql"
        if self.rule:
            return "rule"
        if self.pipeline:
            return "pipeline"
        if self.projection:
            return "projection"
        if self.maintenance:
            return "maintenance"
        if self.payload:
            return "payload"
        return "none"

    def due(self, *, reference: datetime | None = None) -> bool:
        """Has the interval elapsed since the last firing?

        A task that has never fired is due immediately — a fresh process
        should not have to wait a full interval before doing anything.
        """
        if not self.active or self.trigger_type != TRIGGER_TEMPORAL:
            return False
        if not self.last_fired:
            return True
        try:
            last = datetime.fromisoformat(self.last_fired)
        except ValueError:
            return True
        if last.tzinfo is None:
            # An unqualified stamp cannot be compared safely; treat the task as
            # due and let the next firing write a qualified one.
            return True
        return (reference or now()) - last >= _interval(self.interval_seconds)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "label": self.label or self.id,
            "description": self.description,
            "actionClass": self.action_class,
            "taskStatus": self.status,
            "triggerType": self.trigger_type,
            "intervalSeconds": self.interval_seconds,
            "executeAtInception": self.execute_at_inception,
            "datasetScope": self.dataset_scope,
            "persona": self.persona_iri,
            "policy": self.policy_iri,
            "action": self.action,
            "rule": self.rule,
            "pipeline": self.pipeline,
            "projection": self.projection,
            "maintenance": self.maintenance,
            "targetGraph": self.target_graph,
            "invocationCommand": self.invocation_command,
            "lastFired": self.last_fired,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "sparql": self.sparql, "payload": self.payload}


def _interval(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=max(seconds, 1.0))


@dataclass
class FiringRecord:
    """One firing, recorded whether or not anything was written.

    Provenance is not a success log. A rejected firing that leaves no trace is
    indistinguishable from a scheduler that never ran, which is exactly the
    state the rate-limit bug produced.
    """

    iri: str
    task_iri: str
    outcome: str
    fired_at: str = field(default_factory=stamp)
    persona_iri: str = ""
    trigger_type: str = TRIGGER_TEMPORAL
    invocation_source: str = "scheduled"
    task_policy_version: str = ""
    persona_policy_version: str = ""
    detail: str = ""
    triples_written: int = 0
    duration_ms: int = 0

    @staticmethod
    def new_iri() -> str:
        return f"urn:scheduler:firing:{uuid.uuid4().hex}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "iri": self.iri,
            "task": self.task_iri,
            "persona": self.persona_iri,
            "outcome": self.outcome,
            "firedAt": self.fired_at,
            "triggerType": self.trigger_type,
            "invocationSource": self.invocation_source,
            "taskPolicyVersion": self.task_policy_version,
            "personaPolicyVersion": self.persona_policy_version,
            "detail": self.detail,
            "triplesWritten": self.triples_written,
            "durationMs": self.duration_ms,
        }


@dataclass
class QuarantinedProposal:
    iri: str
    task_iri: str
    reason: str
    proposed_turtle: str
    persona_iri: str = ""
    quarantined_at: str = field(default_factory=stamp)

    @staticmethod
    def new_iri() -> str:
        return f"urn:scheduler:quarantine:{uuid.uuid4().hex}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "iri": self.iri,
            "task": self.task_iri,
            "persona": self.persona_iri,
            "reason": self.reason,
            "quarantinedAt": self.quarantined_at,
            "proposedTurtle": self.proposed_turtle,
        }


def task_iri(task_id: str) -> str:
    return f"{SCHED}task-{task_id}"


def validate_task_fields(
    *, action_class: str, status: str, interval_seconds: float
) -> None:
    if action_class not in ACTION_CLASSES:
        raise ValueError(f"actionClass must be one of {', '.join(ACTION_CLASSES)}")
    if status not in TASK_STATUSES:
        raise ValueError(f"taskStatus must be one of {', '.join(TASK_STATUSES)}")
    if interval_seconds < 1:
        raise ValueError("intervalSeconds must be at least 1")
