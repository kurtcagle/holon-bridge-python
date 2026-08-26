"""Scheduler execution — gates, firing, and the tick loop.

A firing passes three gates before anything happens, and produces a
provenance record whichever way it goes:

1. **Status.** Only ``Active`` tasks fire.
2. **Capability.** A task naming a persona may only perform action classes in
   that persona's capability set.
3. **Policy.** Daily caps from ODRL count constraints, counted from
   provenance rather than from memory — so the limit survives a restart.

Gates that reject still record. A rejected firing that leaves no trace is
indistinguishable from a scheduler that never ran, which is precisely the
state the missing-GRAPH bug produced: silent, clean logs, no enforcement.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Protocol

from ..conn import Conn
from ..fuseki import FusekiClient, FusekiError
from ..named_rules import execute_named_rule, load_named_rules
from ..pipeline import load_manifest, run_pipeline
from ..messages import Message
from ..shacl import validate_delta
from .model import (
    FiringRecord,
    Persona,
    QuarantinedProposal,
    Task,
    now,
    stamp,
)
from .proposer import (
    NotConfigured,
    ProposalUnparseable,
    ProposerNotConfigured,
)
from .store import PolicyUnresolvable, SchedulerStore
from .vocab import LLM_INVOCATION, TRIGGER_TEMPORAL

log = logging.getLogger("holonbridge.scheduler")


class Proposer(Protocol):
    """Produces a Turtle proposal for an LLMInvocation task."""

    async def propose(
        self, conn: Conn, task: Task, persona: Persona | None
    ) -> tuple[str, str]:  # (turtle, summary)
        ...


class Scheduler:
    """Owns the task registry, the tick loop, and firing."""

    def __init__(
        self,
        client: FusekiClient,
        *,
        admin_conn: Conn,
        tick_seconds: float = 30.0,
        proposer: Proposer | None = None,
        max_firing_depth: int = 3,
    ) -> None:
        self._client = client
        self._store = SchedulerStore(client)
        self._admin = admin_conn
        self._tick = max(tick_seconds, 1.0)
        self._proposer: Proposer = proposer or NotConfigured()
        self._max_depth = max(max_firing_depth, 1)

        self._tasks: list[Task] = []
        self._personas: dict[str, Persona] = {}
        self._loop: asyncio.Task | None = None
        self._in_flight: set[str] = set()
        self._fired_this_pass: set[str] = set()
        self._started_at: str = ""
        self._ticks = 0
        self._firings = 0
        self._last_error = ""

    # --- lifecycle ------------------------------------------------------------

    async def reload(self) -> dict[str, Any]:
        """Re-read tasks and personas. New tasks are inert until this runs."""
        self._tasks = await self._store.tasks(self._admin)
        self._personas = await self._store.personas(self._admin)
        return {
            "tasks": len(self._tasks),
            "personas": len(self._personas),
            "active": sum(1 for t in self._tasks if t.active),
        }

    async def start(self) -> None:
        if self._loop is not None:
            return
        await self.reload()
        self._started_at = stamp()
        self._loop = asyncio.create_task(self._run())
        log.info(
            "scheduler started — dataset=%s, %d task(s), %d persona(s), tick=%.0fs",
            self._admin.dataset,
            len(self._tasks),
            len(self._personas),
            self._tick,
        )

    async def stop(self) -> None:
        if self._loop is None:
            return
        self._loop.cancel()
        try:
            await self._loop
        except asyncio.CancelledError:
            pass
        self._loop = None

    def status(self) -> dict[str, Any]:
        return {
            "running": self._loop is not None and not self._loop.done(),
            "dataset": self._admin.dataset,
            "startedAt": self._started_at,
            "tickSeconds": self._tick,
            "ticks": self._ticks,
            "firings": self._firings,
            "tasks": len(self._tasks),
            "activeTasks": sum(1 for t in self._tasks if t.active),
            "personas": len(self._personas),
            "inFlight": sorted(self._in_flight),
            "maxFiringDepth": self._max_depth,
            "lastError": self._last_error,
        }

    @property
    def admin_conn(self) -> Conn:
        """The connection the scheduler reads its own configuration through."""
        return self._admin

    @property
    def tasks(self) -> list[Task]:
        return self._tasks

    def task(self, task_id: str) -> Task | None:
        for task in self._tasks:
            if task.id == task_id:
                return task
        return None

    # --- the loop -------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must not die
                self._last_error = str(exc)
                log.exception("scheduler tick failed")
            await asyncio.sleep(self._tick)

    async def tick(self) -> list[FiringRecord]:
        """One pass: fire every task whose interval has elapsed."""
        self._ticks += 1
        reference = now()
        # Cleared each pass so a task may fire once per tick at top level, but
        # cannot be reached again through another task's action within the
        # same pass.
        self._fired_this_pass = set()
        fired: list[FiringRecord] = []

        for task in list(self._tasks):
            if not task.due(reference=reference):
                continue
            record = await self.fire(task, invocation_source="scheduled")
            if record is not None:
                fired.append(record)
        return fired

    # --- firing ---------------------------------------------------------------

    async def fire(
        self,
        task: Task,
        *,
        invocation_source: str = "scheduled",
        depth: int = 0,
    ) -> FiringRecord | None:
        """Run one task through the gates. Returns ``None`` if it was skipped.

        Three separate protections against runaway firing, because they catch
        different shapes of the same problem:

        * **In flight.** A task still running is skipped, not queued. Without
          this a slow task on a fast tick accumulates overlapping firings.
        * **Depth.** A firing reached from inside another firing carries a
          depth; past ``max_firing_depth`` it is refused. Nothing in the
          current action set nests, so this is installed ahead of the feature
          that will need it — ``StateTrigger`` and subscriptions — rather than
          after the incident.
        * **Per pass.** Within one tick a task fires at most once at top
          level and cannot be reached again through another task's action.
          This is the one the in-flight check misses: A triggering B
          triggering A involves no task re-entering itself, so nothing is ever
          simultaneously in flight, and the cycle just runs.
        """
        if task.iri in self._in_flight:
            log.warning("task %s is still running; skipping this tick", task.id)
            return None

        if depth >= self._max_depth:
            log.warning("task %s refused: firing depth %d", task.id, depth)
            return await self._refuse(
                task, invocation_source, f"firing depth {depth} exceeded"
            )

        if depth > 0 and task.iri in self._fired_this_pass:
            log.warning("task %s refused: already fired in this pass", task.id)
            return await self._refuse(
                task,
                invocation_source,
                "already fired in this pass; refusing to complete a cycle",
            )

        self._fired_this_pass.add(task.iri)

        self._in_flight.add(task.iri)
        started = time.monotonic()
        persona = self._personas.get(task.persona_iri) if task.persona_iri else None

        record = FiringRecord(
            iri=FiringRecord.new_iri(),
            task_iri=task.iri,
            outcome="failed",
            persona_iri=persona.iri if persona else "",
            trigger_type=task.trigger_type or TRIGGER_TEMPORAL,
            invocation_source=invocation_source,
        )

        try:
            gate = await self._gate(task, persona, record, invocation_source)
            if gate is None:
                await self._execute(task, persona, record)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised at the loop
            record.outcome = "failed"
            record.detail = str(exc)
            log.exception("task %s failed", task.id)
        finally:
            record.duration_ms = int((time.monotonic() - started) * 1000)
            self._in_flight.discard(task.iri)

        try:
            await self._store.record(self._admin, record)
            if record.outcome in {"committed", "read-only", "deferred"}:
                await self._store.touch_last_fired(self._admin, task, record.fired_at)
        except FusekiError:
            # If provenance cannot be written the rate limiter is blind, so say
            # so loudly rather than carrying on with an unenforceable limit.
            log.exception("could not record firing for task %s", task.id)
            self._last_error = f"provenance write failed for {task.id}"

        self._firings += 1
        return record

    async def _refuse(
        self, task: Task, invocation_source: str, reason: str
    ) -> FiringRecord:
        """Record a refusal that never reached the gates."""
        record = FiringRecord(
            iri=FiringRecord.new_iri(),
            task_iri=task.iri,
            outcome="rejected-policy",
            trigger_type=task.trigger_type or TRIGGER_TEMPORAL,
            invocation_source=invocation_source,
            detail=reason,
        )
        try:
            await self._store.record(self._admin, record)
        except FusekiError:
            log.exception("could not record refusal for task %s", task.id)
        return record

    async def _gate(
        self,
        task: Task,
        persona: Persona | None,
        record: FiringRecord,
        invocation_source: str,
    ) -> str | None:
        """Return a rejection reason, or ``None`` to proceed."""
        if not task.active:
            record.outcome = "rejected-policy"
            record.detail = f"task is {task.status.lower()}"
            return record.detail

        if persona is not None and not persona.can(task.action_class):
            record.outcome = "rejected-capability"
            record.detail = (
                f"persona {persona.id} lacks {task.action_class}; "
                f"it has {', '.join(persona.capabilities) or 'no capabilities'}"
            )
            return record.detail

        # Manual invocations are tagged separately and deliberately do not draw
        # on the scheduled daily allowance.
        if invocation_source != "scheduled":
            return None

        day = record.fired_at[:10]
        for subject, predicate, policy_iri, label in (
            (task.iri, "task", task.policy_iri, f"task {task.id}"),
            *(
                [(persona.iri, "persona", persona.policy_iri, f"persona {persona.id}")]
                if persona
                else []
            ),
        ):
            try:
                policy = await self._store.policy(self._admin, policy_iri)
            except PolicyUnresolvable as exc:
                # Fail closed. A limit that cannot be read is not the same as
                # no limit, and treating it as none is how enforcement silently
                # disappears.
                record.outcome = "rejected-policy"
                record.detail = f"{label}: {exc}"
                return record.detail

            if predicate == "task":
                record.task_policy_version = policy.version
            else:
                record.persona_policy_version = policy.version

            if policy.unlimited:
                continue

            count = await self._store.firings_today(
                self._admin, subject_iri=subject, predicate=predicate, day=day
            )
            if count >= (policy.count or 0):
                record.outcome = "rejected-policy"
                record.detail = (
                    f"{label} has reached its daily limit of {policy.count} "
                    f"({count} today)"
                )
                return record.detail

        return None

    # --- actions --------------------------------------------------------------

    def _target_conn(self, task: Task) -> Conn:
        """The connection a task acts through.

        Distinct from the admin connection the scheduler reads its own config
        from. A task that writes through the admin connection puts its output
        in the wrong dataset.
        """
        if not task.dataset_scope or task.dataset_scope == self._admin.dataset:
            return self._admin
        return replace(self._admin, dataset=task.dataset_scope, overridden=True)

    async def _execute(
        self, task: Task, persona: Persona | None, record: FiringRecord
    ) -> None:
        conn = self._target_conn(task)

        if task.action_class == LLM_INVOCATION:
            await self._execute_proposal(task, persona, record, conn)
            return

        action = task.action
        if action == "sparql":
            results = await self._client.select(conn, task.sparql)
            rows = len(results.get("results", {}).get("bindings", []))
            record.outcome = "read-only"
            record.detail = f"{rows} row(s)"
            return

        if action == "rule":
            rules = await load_named_rules(self._client, conn)
            rule = rules.by_id(task.rule)
            if rule is None:
                record.outcome = "failed"
                record.detail = f"unknown rule {task.rule!r} in dataset {conn.dataset}"
                return
            if task.target_graph:
                rule = replace(rule, target_graph=task.target_graph)
            run = await execute_named_rule(conn, self._client, rule)
            record.outcome = "committed"
            record.triples_written = run.triples_constructed
            record.detail = (
                f"{run.write_mode} into <{run.target_graph}>: "
                f"+{run.triples_added} / -{run.triples_removed}"
            )
            return

        if action == "pipeline":
            manifest = await load_manifest(self._client, conn, task.pipeline)
            if not manifest.nodes:
                record.outcome = "failed"
                record.detail = f"unknown pipeline {task.pipeline!r}"
                return
            message = Message(id=Message.new_id(), pipeline=task.pipeline)
            await run_pipeline(conn, self._client, manifest, message)
            failed = [s for s in message.stages if s.status == "Failed"]
            record.outcome = "failed" if failed else "committed"
            record.triples_written = sum(s.triples_written for s in message.stages)
            record.detail = (
                f"{len(message.stages)} stage(s), {len(failed)} failed"
                if failed
                else f"{len(message.stages)} stage(s)"
            )
            return

        if action == "projection":
            from ..projection import ProjectionError, ProjectionRunner, ProjectionStore

            hooks = await ProjectionStore(self._client).hooks(conn)
            hook = next((h for h in hooks if h.id == task.projection), None)
            if hook is None:
                record.outcome = "failed"
                record.detail = f"unknown projection hook {task.projection!r}"
                return
            try:
                delivery, envelope = await ProjectionRunner(self._client).run(conn, hook)
            except ProjectionError as exc:
                record.outcome = "failed"
                record.detail = str(exc)
                return
            record.outcome = "failed" if delivery.status == "failed" else "committed"
            record.triples_written = envelope.addition_count
            record.detail = (
                f"{delivery.status} to {hook.target}: "
                f"+{envelope.addition_count} / -{envelope.retraction_count}"
            )
            return

        if action == "maintenance":
            await self._run_maintenance(task, record, conn)
            return

        if action == "payload":
            if not task.target_graph:
                record.outcome = "failed"
                record.detail = "payload task names no target graph"
                return
            await self._client.post_graph(conn, task.target_graph, task.payload)
            record.outcome = "committed"
            record.detail = f"payload merged into <{task.target_graph}>"
            return

        record.outcome = "failed"
        record.detail = (
            "task declares no sparql, rule, pipeline, projection, maintenance, "
            "or payload"
        )

    async def _run_maintenance(
        self, task: Task, record: FiringRecord, conn: Conn
    ) -> None:
        """Housekeeping the bridge owns.

        CHANGED 2026-08-26: added ``trigger-sweep`` — the periodic half of
        the named-trigger feature (see ``holonbridge/triggers.py``). A
        ``TemporalTrigger`` has no write to hook, unlike a ``StateTrigger``
        (wired into ``fluent.py`` instead) — its condition can only become
        true because wall-clock time passed a threshold, so it needs a
        caller to ask again periodically. This maintenance job is that
        caller: an ordinary scheduler ``Task`` with
        ``maintenance="trigger-sweep"`` on whatever interval fits the
        condition (an hourly age-eligibility sweep is more than adequate;
        there is nothing here that benefits from a shorter one). Imported
        lazily, same as the ``projection-sweep`` job just above it, to
        avoid a module-level import cycle between the scheduler package
        and ``triggers.py``.
        """
        job = task.maintenance.strip().lower()
        if job in {"projection-sweep", "sweep"}:
            from ..projection import ProjectionRunner

            result = await ProjectionRunner(self._client).sweep(conn)
            record.outcome = "read-only"
            record.detail = (
                f"swept {result['abandonedCount']} abandoned delivery(ies), "
                f"{result['orphanedCount']} orphaned graph(s)"
            )
            return

        if job in {"trigger-sweep", "sweep-triggers"}:
            from ..triggers import TRIGGER_TEMPORAL as _TEMPORAL
            from ..triggers import evaluate_triggers

            firings = await evaluate_triggers(self._client, conn, kind=_TEMPORAL)
            proposed = sum(1 for f in firings if f.outcome == "proposed")
            executed = sum(1 for f in firings if f.outcome == "executed")
            failed = sum(1 for f in firings if f.outcome == "failed")
            record.outcome = "failed" if failed and not (proposed or executed) else "committed"
            record.triples_written = 0
            record.detail = (
                f"{len(firings)} firing(s): {proposed} proposed, {executed} executed, "
                f"{failed} failed"
            )
            return

        record.outcome = "failed"
        record.detail = f"unknown maintenance job {task.maintenance!r}"

    async def _execute_proposal(
        self, task: Task, persona: Persona | None, record: FiringRecord, conn: Conn
    ) -> None:
        """Propose, validate, then commit or quarantine.

        A proposal is never written straight through. The persona returns
        Turtle and a one-line summary; the summary is stripped before the
        Turtle is validated, and anything that fails validation is quarantined
        with its text intact so it can be inspected rather than lost.
        """
        try:
            turtle, summary = await self._proposer.propose(conn, task, persona)
        except ProposerNotConfigured as exc:
            # Nothing was attempted. Distinct from a proposer that tried and
            # failed — conflating the two hides a broken persona behind a
            # status that reads like a configuration choice.
            record.outcome = "deferred"
            record.detail = str(exc)
            return
        except ProposalUnparseable as exc:
            # The persona replied with something. Keep it: unreadable output is
            # the most informative thing there is about a misbehaving prompt.
            await self._quarantine(
                task, persona, record, reason=str(exc), turtle=exc.raw
            )
            return
        except Exception as exc:  # noqa: BLE001
            record.outcome = "failed"
            record.detail = f"proposal failed: {exc}"
            return

        if not task.target_graph:
            record.outcome = "failed"
            record.detail = "LLMInvocation task names no target graph"
            return

        reason = ""
        try:
            report = await validate_delta(
                self._client,
                conn,
                turtle=turtle,
                shapes_graph=conn.shapes_graph,
                target_graph=task.target_graph,
            )
            if not report.conforms:
                reason = f"{len(report.results)} new violation(s)"
        except (ValueError, FusekiError) as exc:
            reason = f"validation could not be run: {exc}"

        if reason:
            await self._quarantine(task, persona, record, reason=reason, turtle=turtle)
            return

        await self._client.post_graph(conn, task.target_graph, turtle)
        record.outcome = "committed"
        # The summary is what the persona said it did; it goes in provenance,
        # never in the payload.
        record.detail = summary or f"proposal merged into <{task.target_graph}>"

    async def _quarantine(
        self,
        task: Task,
        persona: Persona | None,
        record: FiringRecord,
        *,
        reason: str,
        turtle: str,
    ) -> None:
        proposal = QuarantinedProposal(
            iri=QuarantinedProposal.new_iri(),
            task_iri=task.iri,
            persona_iri=persona.iri if persona else "",
            reason=reason,
            proposed_turtle=turtle,
        )
        await self._store.quarantine(self._admin, proposal)
        record.outcome = "quarantined"
        record.detail = f"{reason}; held at <{proposal.iri}>"
