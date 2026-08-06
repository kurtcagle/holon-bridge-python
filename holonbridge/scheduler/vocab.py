"""Scheduler vocabulary and graph names.

The scheduler's own registries are **not** dataset-scoped. There is one
scheduler per process, its config lives in the admin dataset, and a task
names the dataset it acts on via ``sched:datasetScope``. So a firing reads
its configuration through one connection and does its work through another.
Conflating the two is how a task ends up writing its output into the admin
dataset.
"""

from __future__ import annotations

from typing import Final

SCHED: Final = "https://w3id.org/holon/sched#"
ODRL: Final = "http://www.w3.org/ns/odrl/2/"
XSD: Final = "http://www.w3.org/2001/XMLSchema#"

TASKS_GRAPH: Final = "urn:scheduler:tasks"
PERSONAS_GRAPH: Final = "urn:scheduler:personas"
PROVENANCE_GRAPH: Final = "urn:scheduler:provenance"
QUARANTINE_GRAPH: Final = "urn:scheduler:quarantine"

SCHEDULER_GRAPHS: Final = (
    TASKS_GRAPH,
    PERSONAS_GRAPH,
    PROVENANCE_GRAPH,
    QUARANTINE_GRAPH,
)

#: Action classes. A persona may only perform those in its capability set.
READ_ONLY_QUERY: Final = "ReadOnlyQuery"
GRAPH_WRITE: Final = "GraphWrite"
LLM_INVOCATION: Final = "LLMInvocation"
ACTION_CLASSES: Final = (READ_ONLY_QUERY, GRAPH_WRITE, LLM_INVOCATION)

#: Lifecycle, mirroring hb:ruleStatus so the two read the same way.
TASK_STATUSES: Final = ("Active", "Suspended", "Deprecated")

TRIGGER_TEMPORAL: Final = "TemporalTrigger"
TRIGGER_STATE: Final = "StateTrigger"

#: Every firing produces one of these, whether or not anything was written.
OUTCOMES: Final = (
    "committed",
    "read-only",
    "deferred",
    "rejected-capability",
    "rejected-policy",
    "quarantined",
    "failed",
)

#: Outcomes that consume a task's or persona's daily allowance. A firing that
#: was refused by a gate must not count against the limit that refused it —
#: otherwise one rejection permanently consumes a slot.
BILLABLE_OUTCOMES: Final = ("committed", "read-only")

PREFIXES: Final = f"""PREFIX sched: <{SCHED}>
PREFIX odrl:  <{ODRL}>
PREFIX xsd:   <{XSD}>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
"""


def graph_query(select: str, graph: str, pattern: str, *, tail: str = "") -> str:
    """Build a query whose pattern is always inside a GRAPH clause.

    This exists because of a real bug: a policy lookup written without a
    ``GRAPH`` wrapper queried the default graph, matched nothing, returned
    "no limit", and so silently disabled rate limiting altogether. Nothing
    errored — the query was valid, it just asked the wrong place.

    Every scheduler query is built here so that cannot recur. There is a test
    asserting no scheduler query string escapes without a GRAPH clause.
    """
    if not graph:
        raise ValueError("a scheduler query must name the graph it reads")
    body = pattern.strip()
    return f"""{PREFIXES}
{select.strip()}
WHERE {{
  GRAPH <{graph}> {{
{body}
  }}
}}
{tail.strip()}""".rstrip()
