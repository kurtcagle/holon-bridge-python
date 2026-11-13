"""Writing named triggers: validation and the Turtle they are stored as.

Until now a trigger could only be installed by hand-authoring Turtle and
pushing it into ``urn:{dataset}:named-triggers``, with no gate specific to
"may this caller install something that will later act on its own". That
matters more for triggers than for anything else in the registry, because a
trigger is *definer's rights* in the strictest sense: once it exists, it is
evaluated by the bridge, as the bridge, on a write or a sweep, with nobody
calling it. Whatever check is going to happen has to happen when it is
registered. The routes in ``routes/triggers.py`` do that; this module is the
part with no HTTP in it.

The shape stored here is exactly what :func:`holonbridge.triggers.load_named_triggers`
reads back (``hev:NamedTrigger`` with ``hev:id``, ``hev:triggerKind``,
``hev:condition``, ``hev:onFire``, ``hev:reviewRequired``, ``hev:triggerStatus``
and ``hev:watchedPredicate``), so a registered trigger and a hand-authored one
are indistinguishable to the evaluator.
"""

from __future__ import annotations

import re

from .conn import Conn
from .triggers import HEV_NAMESPACE, TRIGGER_CLASS_SUFFIX, TRIGGER_KINDS, TRIGGER_STATUSES
from .turtle import literal

__all__ = [
    "InvalidTrigger",
    "deletion_update",
    "registration_update",
    "render_trigger_turtle",
    "status_update",
    "trigger_node",
    "validate_watched_predicates",
]

_XSD_DATETIME = "<http://www.w3.org/2001/XMLSchema#dateTime>"
_XSD_BOOLEAN = "<http://www.w3.org/2001/XMLSchema#boolean>"

#: An absolute IRI that can sit inside ``<...>`` in Turtle/SPARQL without
#: escaping: a scheme, then nothing that terminates or confuses the IRI.
_SAFE_IRI = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>"{}|\\^`]*$')


class InvalidTrigger(ValueError):
    """A trigger definition that cannot be stored as given."""


def trigger_node(conn: Conn, trigger_id: str) -> str:
    """The IRI a trigger is stored under: ``urn:{dataset}:named-trigger:{id}``."""
    return conn.scoped("named-triggers", trigger_id)


def validate_watched_predicates(values: list[str]) -> list[str]:
    """Absolute, injection-safe IRIs, de-duplicated, order kept."""
    seen: list[str] = []
    for value in values:
        if not _SAFE_IRI.match(value or ""):
            raise InvalidTrigger(f"watched predicate {value!r} is not a usable absolute IRI")
        if value not in seen:
            seen.append(value)
    return seen


def render_trigger_turtle(
    *,
    node: str,
    trigger_id: str,
    trigger_kind: str,
    condition: str,
    on_fire: str | None,
    review_required: bool,
    status: str,
    label: str | None,
    description: str | None,
    watched_predicates: list[str],
    registered_at: str,
    armed_by: str | None = None,
    agent_endpoint: str | None = None,
    agent_task: str | None = None,
    agent_context: str | None = None,
    agent_target_graph: str | None = None,
    fluent_query: str | None = None,
) -> str:
    if sum(bool(x) for x in (on_fire, agent_endpoint, fluent_query)) != 1:
        raise InvalidTrigger(
            "a trigger names exactly one action: a rule (onFire), an agent (onFireAgent) "
            "or a fluent update query (onFireFluentQuery)"
        )
    if fluent_query and review_required:
        raise InvalidTrigger(
            "onFireFluentQuery needs reviewRequired=false: a fluent update is a compare-and-set, "
            "not Turtle that can wait in the candidate queue"
        )
    if agent_endpoint and not (agent_task or "").strip():
        raise InvalidTrigger("an agent trigger needs a task to give the agent")
    if not agent_endpoint and (agent_task or agent_context or agent_target_graph):
        raise InvalidTrigger("agentTask, agentContext and agentTargetGraph only apply to onFireAgent")
    if agent_target_graph and not _SAFE_IRI.match(agent_target_graph):
        raise InvalidTrigger(f"agent target graph {agent_target_graph!r} is not a usable absolute IRI")
    if trigger_kind not in TRIGGER_KINDS:
        raise InvalidTrigger(
            f"triggerKind must be one of {', '.join(TRIGGER_KINDS)}, not {trigger_kind!r}"
        )
    if status not in TRIGGER_STATUSES:
        raise InvalidTrigger(f"status must be one of {', '.join(TRIGGER_STATUSES)}")

    hev = HEV_NAMESPACE
    subject = f"<{node}>"
    pairs = [
        f"a <{hev}{TRIGGER_CLASS_SUFFIX}>",
        f"<{hev}id> {literal(trigger_id)}",
        f"<{hev}triggerKind> {literal(trigger_kind)}",
        f"<{hev}condition> {literal(condition)}",
        f"<{hev}reviewRequired> {literal('true' if review_required else 'false', datatype=_XSD_BOOLEAN)}",
        f"<{hev}triggerStatus> {literal(status)}",
    ]
    if on_fire:
        pairs.append(f"<{hev}onFire> {literal(on_fire)}")
    elif fluent_query:
        pairs.append(f"<{hev}onFireFluentQuery> {literal(fluent_query)}")
    else:
        pairs.append(f"<{hev}onFireAgent> {literal(agent_endpoint)}")
        pairs.append(f"<{hev}agentTask> {literal(agent_task)}")
        if agent_context:
            pairs.append(f"<{hev}agentContext> {literal(agent_context)}")
        if agent_target_graph:
            pairs.append(f"<{hev}agentTargetGraph> {literal(agent_target_graph)}")
    if label:
        pairs.append(f"<{hev}label> {literal(label)}")
    if description:
        pairs.append(f"<{hev}description> {literal(description)}")
    pairs.extend(f"<{hev}watchedPredicate> <{iri}>" for iri in watched_predicates)
    if armed_by:
        if not _SAFE_IRI.match(armed_by):
            raise InvalidTrigger(f"armed_by {armed_by!r} is not a usable absolute IRI")
        pairs.append(f"<{hev}armedBy> <{armed_by}>")
    pairs.append(f"<{hev}registeredAt> {literal(registered_at, datatype=_XSD_DATETIME)}")
    return f"{subject}\n    " + " ;\n    ".join(pairs) + " ."


def registration_update(registry: str, node: str, turtle: str) -> str:
    """Replace everything stored about ``node`` with ``turtle``, in one request."""
    return f"""DELETE WHERE {{ GRAPH <{registry}> {{ <{node}> ?p ?o }} }} ;
INSERT DATA {{ GRAPH <{registry}> {{
{turtle}
}} }}"""


def deletion_update(registry: str, node: str) -> str:
    return f"DELETE WHERE {{ GRAPH <{registry}> {{ <{node}> ?p ?o }} }}"


def status_update(
    registry: str, node: str, status: str, *, armed_by: str | None = None
) -> str:
    """Set the status, removing it under whichever alias it was authored with
    (the loader accepts ``triggerStatus`` and ``status``).

    ``armed_by`` is passed when the change arms the trigger (making it Active):
    whoever does that takes over responsibility for it, and is the person whose
    access is re-checked when it fires.
    """
    if status not in TRIGGER_STATUSES:
        raise InvalidTrigger(f"status must be one of {', '.join(TRIGGER_STATUSES)}")
    if armed_by is not None and not _SAFE_IRI.match(armed_by):
        raise InvalidTrigger(f"armed_by {armed_by!r} is not a usable absolute IRI")
    arming = f' || STRENDS(STR(?p), "armedBy")' if armed_by else ""
    new_arm = f"<{node}> <{HEV_NAMESPACE}armedBy> <{armed_by}> ." if armed_by else ""
    return f"""DELETE {{ GRAPH <{registry}> {{ <{node}> ?p ?o }} }}
WHERE {{ GRAPH <{registry}> {{
  <{node}> ?p ?o .
  FILTER( STRENDS(STR(?p), "triggerStatus") || STRENDS(STR(?p), "/status") || STRENDS(STR(?p), "#status"){arming} )
}} }} ;
INSERT DATA {{ GRAPH <{registry}> {{
  <{node}> <{HEV_NAMESPACE}triggerStatus> {literal(status)} .
  {new_arm}
}} }}"""
