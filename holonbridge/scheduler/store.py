"""Scheduler persistence.

Every query here goes through :func:`~holonbridge.scheduler.vocab.graph_query`,
so none of them can address the default graph by accident. Every literal goes
through :func:`~holonbridge.turtle.literal` — scheduler records carry error
text, SPARQL bodies, and Windows paths, and an unescaped backslash in a path
once killed every tick silently.
"""

from __future__ import annotations

import logging
from typing import Any

from ..conn import Conn
from ..fuseki import FusekiClient, FusekiError
from ..rdfutil import collect, local_name, pick, pick_all, truthy
from ..turtle import literal
from .model import (
    FiringRecord,
    Persona,
    Policy,
    QuarantinedProposal,
    Task,
    stamp,
)
from .vocab import (
    BILLABLE_OUTCOMES,
    PERSONAS_GRAPH,
    PROVENANCE_GRAPH,
    QUARANTINE_GRAPH,
    SCHED,
    TASKS_GRAPH,
    XSD,
    graph_query,
)

log = logging.getLogger("holonbridge.scheduler.store")

DT = f"<{XSD}dateTime>"
INT = f"<{XSD}integer>"

_TASK_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "taskId", "identifier"),
    "action_class": ("actionClass",),
    "status": ("taskStatus", "status"),
    "trigger_type": ("triggerType", "trigger"),
    # Unit comes from the property name, never from the magnitude.
    "interval_seconds": ("intervalSeconds", "interval"),
    "interval_ms": ("intervalMs",),
    "execute_at_inception": ("executeAtInception",),
    "dataset_scope": ("datasetScope", "dataset"),
    "persona": ("persona",),
    "policy": ("hasPolicy", "policy"),
    "label": ("label", "title"),
    "description": ("description", "comment"),
    "sparql": ("sparql", "query"),
    "rule": ("rule", "namedRule"),
    "pipeline": ("pipeline",),
    "projection": ("projection", "hook"),
    "maintenance": ("maintenance",),
    "payload": ("payload", "turtle"),
    "target_graph": ("targetGraph", "target"),
    "invocation_command": ("invocationCommand",),
    "last_fired": ("lastFired",),
}
_PERSONA_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "personaId", "identifier"),
    "label": ("label", "title"),
    "model": ("model",),
    "dataset_scope": ("datasetScope", "dataset"),
    "policy": ("hasPolicy", "policy"),
}
_CAPABILITIES = ("capability", "capabilities", "hasCapability")


class SchedulerStore:
    """Reads and writes the four scheduler graphs in the admin dataset."""

    def __init__(self, client: FusekiClient) -> None:
        self._client = client

    # --- tasks ----------------------------------------------------------------

    async def tasks(self, conn: Conn) -> list[Task]:
        query = graph_query(
            "SELECT ?t ?p ?o",
            TASKS_GRAPH,
            "    ?t a sched:ScheduledTask .\n    ?t ?p ?o .",
        )
        try:
            rows = (await self._client.select(conn, query))["results"]["bindings"]
        except (FusekiError, KeyError) as exc:
            log.warning("task load failed: %s", exc)
            return []

        tasks: list[Task] = []
        for iri, props in collect(rows, "t").items():
            tasks.append(
                Task(
                    iri=iri,
                    id=pick(props, _TASK_FIELDS["id"]) or _task_id(iri),
                    action_class=local_name(
                        pick(props, _TASK_FIELDS["action_class"]) or "ReadOnlyQuery"
                    ),
                    status=local_name(pick(props, _TASK_FIELDS["status"]) or "Active"),
                    trigger_type=local_name(
                        pick(props, _TASK_FIELDS["trigger_type"]) or "TemporalTrigger"
                    ),
                    interval_seconds=_interval_seconds(props),
                    execute_at_inception=truthy(
                        pick(props, _TASK_FIELDS["execute_at_inception"])
                    ),
                    dataset_scope=pick(props, _TASK_FIELDS["dataset_scope"]) or "",
                    persona_iri=pick(props, _TASK_FIELDS["persona"]) or "",
                    policy_iri=pick(props, _TASK_FIELDS["policy"]) or "",
                    label=pick(props, _TASK_FIELDS["label"]) or "",
                    description=pick(props, _TASK_FIELDS["description"]) or "",
                    sparql=pick(props, _TASK_FIELDS["sparql"]) or "",
                    rule=pick(props, _TASK_FIELDS["rule"]) or "",
                    pipeline=pick(props, _TASK_FIELDS["pipeline"]) or "",
                    projection=pick(props, _TASK_FIELDS["projection"]) or "",
                    maintenance=pick(props, _TASK_FIELDS["maintenance"]) or "",
                    payload=pick(props, _TASK_FIELDS["payload"]) or "",
                    target_graph=pick(props, _TASK_FIELDS["target_graph"]) or "",
                    invocation_command=pick(props, _TASK_FIELDS["invocation_command"])
                    or "",
                    last_fired=pick(props, _TASK_FIELDS["last_fired"]) or "",
                )
            )
        tasks.sort(key=lambda t: t.id)
        return tasks

    async def save_task(self, conn: Conn, task: Task) -> None:
        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{TASKS_GRAPH}> {{ <{task.iri}> ?p ?o }} }}
WHERE {{ GRAPH <{TASKS_GRAPH}> {{ <{task.iri}> ?p ?o }} }}""",
        )
        await self._client.update(conn, f"INSERT DATA {{ {self._task_turtle(task)} }}")

    def _task_turtle(self, task: Task) -> str:
        lines = [
            f"  GRAPH <{TASKS_GRAPH}> {{",
            f"    <{task.iri}> a sched:ScheduledTask ;",
            f"      <{SCHED}id> {literal(task.id)} ;",
            f"      <{SCHED}actionClass> <{SCHED}{task.action_class}> ;",
            f"      <{SCHED}taskStatus> <{SCHED}{task.status}> ;",
            f"      <{SCHED}triggerType> {literal(task.trigger_type)} ;",
            f"      <{SCHED}intervalSeconds> {literal(str(int(task.interval_seconds)), datatype=INT)} ;",
            f"      <{SCHED}executeAtInception> {literal('true' if task.execute_at_inception else 'false')} ;",
        ]
        optional = {
            "datasetScope": task.dataset_scope,
            "persona": task.persona_iri,
            "hasPolicy": task.policy_iri,
            "label": task.label,
            "description": task.description,
            "sparql": task.sparql,
            "rule": task.rule,
            "pipeline": task.pipeline,
            "projection": task.projection,
            "maintenance": task.maintenance,
            "payload": task.payload,
            "targetGraph": task.target_graph,
            "invocationCommand": task.invocation_command,
            "lastFired": task.last_fired,
        }
        for term, value in optional.items():
            if value:
                lines.append(f"      <{SCHED}{term}> {literal(str(value))} ;")

        lines[-1] = lines[-1].rstrip(" ;") + " ."
        lines.append("  }")
        # sched: is not bound inside INSERT DATA, so write the class out in full
        return "\n".join(lines).replace(
            "a sched:ScheduledTask", f"a <{SCHED}ScheduledTask>"
        )

    async def set_task_status(self, conn: Conn, task: Task, status: str) -> None:
        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{TASKS_GRAPH}> {{ <{task.iri}> <{SCHED}taskStatus> ?s }} }}
INSERT {{ GRAPH <{TASKS_GRAPH}> {{ <{task.iri}> <{SCHED}taskStatus> <{SCHED}{status}> }} }}
WHERE  {{ OPTIONAL {{ GRAPH <{TASKS_GRAPH}> {{ <{task.iri}> <{SCHED}taskStatus> ?s }} }} }}""",
        )
        task.status = status

    async def touch_last_fired(self, conn: Conn, task: Task, when: str) -> None:
        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{TASKS_GRAPH}> {{ <{task.iri}> <{SCHED}lastFired> ?t }} }}
INSERT {{ GRAPH <{TASKS_GRAPH}> {{ <{task.iri}> <{SCHED}lastFired> {literal(when)} }} }}
WHERE  {{ OPTIONAL {{ GRAPH <{TASKS_GRAPH}> {{ <{task.iri}> <{SCHED}lastFired> ?t }} }} }}""",
        )
        task.last_fired = when

    # --- personas -------------------------------------------------------------

    async def personas(self, conn: Conn) -> dict[str, Persona]:
        query = graph_query(
            "SELECT ?a ?p ?o",
            PERSONAS_GRAPH,
            "    ?a a sched:Persona .\n    ?a ?p ?o .",
        )
        try:
            rows = (await self._client.select(conn, query))["results"]["bindings"]
        except (FusekiError, KeyError) as exc:
            log.warning("persona load failed: %s", exc)
            return {}

        out: dict[str, Persona] = {}
        for iri, props in collect(rows, "a").items():
            out[iri] = Persona(
                iri=iri,
                id=pick(props, _PERSONA_FIELDS["id"]) or local_name(iri),
                label=pick(props, _PERSONA_FIELDS["label"]) or "",
                model=pick(props, _PERSONA_FIELDS["model"]) or "",
                dataset_scope=pick(props, _PERSONA_FIELDS["dataset_scope"]) or "",
                policy_iri=pick(props, _PERSONA_FIELDS["policy"]) or "",
                capabilities=[local_name(c) for c in pick_all(props, _CAPABILITIES)],
            )
        return out

    # --- policy ---------------------------------------------------------------

    async def policy(self, conn: Conn, policy_iri: str) -> Policy:
        """Resolve an ODRL daily-count permission.

        A missing ``policy_iri`` means "no policy declared", which is
        unlimited. A declared policy that will not resolve is a different
        thing entirely and raises — see :class:`PolicyUnresolvable`. Treating
        the two alike is what let a broken query read as "no limit" and
        disable rate limiting without a single error.
        """
        if not policy_iri:
            return Policy(iri="", count=None)

        query = graph_query(
            "SELECT ?count ?version",
            TASKS_GRAPH,
            f"""    <{policy_iri}> odrl:permission ?perm .
    ?perm odrl:constraint ?c .
    ?c odrl:leftOperand odrl:count ;
       odrl:rightOperand ?count .
    OPTIONAL {{ <{policy_iri}> <http://www.w3.org/2002/07/owl#versionInfo> ?version }}""",
        )
        try:
            rows = (await self._client.select(conn, query))["results"]["bindings"]
        except (FusekiError, KeyError) as exc:
            raise PolicyUnresolvable(
                f"policy <{policy_iri}> could not be read: {exc}"
            ) from exc

        if not rows:
            raise PolicyUnresolvable(
                f"policy <{policy_iri}> is declared but carries no odrl:count "
                f"constraint in <{TASKS_GRAPH}>"
            )

        row = rows[0]
        try:
            count = int(row["count"]["value"])
        except (KeyError, ValueError) as exc:
            raise PolicyUnresolvable(
                f"policy <{policy_iri}> has a non-integer count"
            ) from exc

        return Policy(
            iri=policy_iri,
            count=count,
            version=row.get("version", {}).get("value", ""),
        )

    async def firings_today(
        self, conn: Conn, *, subject_iri: str, predicate: str, day: str
    ) -> int:
        """Count billable firings for a task or persona on a given day."""
        outcomes = ", ".join(f'"{o}"' for o in BILLABLE_OUTCOMES)
        query = graph_query(
            "SELECT (COUNT(?rec) AS ?c)",
            PROVENANCE_GRAPH,
            f"""    ?rec <{SCHED}{predicate}> <{subject_iri}> ;
         <{SCHED}firedAt> ?ts ;
         <{SCHED}outcome> ?outcome .
    FILTER( STRSTARTS(STR(?ts), "{day}") )
    FILTER( ?outcome IN ({outcomes}) )""",
        )
        rows = (await self._client.select(conn, query))["results"]["bindings"]
        return int(rows[0]["c"]["value"]) if rows else 0

    # --- provenance -----------------------------------------------------------

    async def record(self, conn: Conn, firing: FiringRecord) -> None:
        turtle = "\n".join(
            [
                f"  GRAPH <{PROVENANCE_GRAPH}> {{",
                f"    <{firing.iri}> a <{SCHED}FiringRecord> ;",
                f"      <{SCHED}task> <{firing.task_iri}> ;",
                f"      <{SCHED}outcome> {literal(firing.outcome)} ;",
                f"      <{SCHED}firedAt> {literal(firing.fired_at, datatype=DT)} ;",
                f"      <{SCHED}triggerType> {literal(firing.trigger_type)} ;",
                f"      <{SCHED}invocationSource> {literal(firing.invocation_source)} ;",
                f"      <{SCHED}triplesWritten> {literal(str(firing.triples_written), datatype=INT)} ;",
                f"      <{SCHED}durationMs> {literal(str(firing.duration_ms), datatype=INT)} ;",
            ]
            + (
                [f"      <{SCHED}persona> <{firing.persona_iri}> ;"]
                if firing.persona_iri
                else []
            )
            + (
                [
                    f"      <{SCHED}taskPolicyVersion> {literal(firing.task_policy_version)} ;"
                ]
                if firing.task_policy_version
                else []
            )
            + (
                [
                    f"      <{SCHED}personaPolicyVersion> {literal(firing.persona_policy_version)} ;"
                ]
                if firing.persona_policy_version
                else []
            )
            + [
                f"      <{SCHED}detail> {literal(firing.detail[:2000])} .",
                "  }",
            ]
        )
        await self._client.update(conn, f"INSERT DATA {{ {turtle} }}")

    async def activity(
        self, conn: Conn, *, since: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        clause = (
            f'    FILTER( ?firedAt >= "{since}"^^<{XSD}dateTime> )\n' if since else ""
        )
        query = graph_query(
            "SELECT ?rec ?task ?outcome ?firedAt ?persona ?source ?detail",
            PROVENANCE_GRAPH,
            f"""    ?rec a <{SCHED}FiringRecord> ;
         <{SCHED}task> ?task ;
         <{SCHED}outcome> ?outcome ;
         <{SCHED}firedAt> ?firedAt .
    OPTIONAL {{ ?rec <{SCHED}persona> ?persona }}
    OPTIONAL {{ ?rec <{SCHED}invocationSource> ?source }}
    OPTIONAL {{ ?rec <{SCHED}detail> ?detail }}
{clause}""",
            tail=f"ORDER BY DESC(?firedAt) LIMIT {int(limit)}",
        )
        rows = (await self._client.select(conn, query))["results"]["bindings"]
        return [
            {
                "iri": r["rec"]["value"],
                "task": r["task"]["value"],
                "outcome": r["outcome"]["value"],
                "firedAt": r["firedAt"]["value"],
                "persona": r.get("persona", {}).get("value", ""),
                "invocationSource": r.get("source", {}).get("value", ""),
                "detail": r.get("detail", {}).get("value", ""),
            }
            for r in rows
        ]

    # --- quarantine -----------------------------------------------------------

    async def quarantine(self, conn: Conn, proposal: QuarantinedProposal) -> None:
        turtle = "\n".join(
            [
                f"  GRAPH <{QUARANTINE_GRAPH}> {{",
                f"    <{proposal.iri}> a <{SCHED}QuarantinedProposal> ;",
                f"      <{SCHED}task> <{proposal.task_iri}> ;",
                f"      <{SCHED}reason> {literal(proposal.reason[:1000])} ;",
                f"      <{SCHED}quarantinedAt> {literal(proposal.quarantined_at, datatype=DT)} ;",
            ]
            + (
                [f"      <{SCHED}persona> <{proposal.persona_iri}> ;"]
                if proposal.persona_iri
                else []
            )
            + [
                f"      <{SCHED}proposedTurtle> {literal(proposal.proposed_turtle[:20000])} .",
                "  }",
            ]
        )
        await self._client.update(conn, f"INSERT DATA {{ {turtle} }}")

    async def quarantined(self, conn: Conn, limit: int = 50) -> list[dict[str, Any]]:
        query = graph_query(
            "SELECT ?q ?task ?reason ?at ?turtle",
            QUARANTINE_GRAPH,
            f"""    ?q a <{SCHED}QuarantinedProposal> ;
       <{SCHED}task> ?task ;
       <{SCHED}reason> ?reason ;
       <{SCHED}quarantinedAt> ?at ;
       <{SCHED}proposedTurtle> ?turtle .""",
            tail=f"ORDER BY DESC(?at) LIMIT {int(limit)}",
        )
        rows = (await self._client.select(conn, query))["results"]["bindings"]
        return [
            {
                "iri": r["q"]["value"],
                "task": r["task"]["value"],
                "reason": r["reason"]["value"],
                "quarantinedAt": r["at"]["value"],
                "proposedTurtle": r["turtle"]["value"],
            }
            for r in rows
        ]


class PolicyUnresolvable(RuntimeError):
    """A policy is declared but cannot be read.

    Distinct from "no policy", and it must stay distinct: conflating them
    fails open, and a rate limiter that fails open is not a rate limiter.
    """


def _task_id(iri: str) -> str:
    name = local_name(iri)
    return name[len("task-") :] if name.startswith("task-") else name


def _interval_seconds(props: dict[str, list[str]]) -> float:
    """Resolve the firing interval from whichever property carries it.

    ``sched:intervalMs`` is milliseconds, ``sched:intervalSeconds`` and
    ``sched:interval`` are seconds. The unit comes from the property name and
    nothing else — inferring it from the magnitude would silently reinterpret
    a legitimately large interval, and a scheduler that quietly fires a
    thousand times more often than asked is a bad failure.
    """
    raw = pick(props, _TASK_FIELDS["interval_seconds"])
    if raw is not None:
        return _number(raw, 3600.0)

    raw = pick(props, _TASK_FIELDS["interval_ms"])
    if raw is not None:
        return _number(raw, 3_600_000.0) / 1000.0

    return 3600.0


def _number(raw: str, fallback: float) -> float:
    try:
        return float(raw)
    except ValueError:
        return fallback
