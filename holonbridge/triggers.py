"""Named triggers — condition-driven CommandEvents, distinct from the scheduler.

**Why this is not the scheduler.** The scheduler's ``Task`` model already
reserves a ``trigger_type`` field and a ``StateTrigger`` vocabulary term
(``scheduler/vocab.py``'s ``TRIGGER_STATE``) — but nothing in the scheduler
ever evaluates one: ``Task.due()`` explicitly returns ``False`` for any
non-``TemporalTrigger`` task, so a ``StateTrigger`` task can never fire on
its own. This module is what that reserved vocabulary was clearly meant
for and never got: a registry of conditions, evaluated either because a
write just happened (``TriggerKind.STATE``, reusing the scheduler's own
``TRIGGER_STATE`` constant) or because a periodic sweep asked
(``TriggerKind.TEMPORAL``, ``TRIGGER_TEMPORAL``) — the same distinction the
scheduler's own vocabulary already drew, finally implemented on both sides.

**Why a trigger is not a rule.** A named rule (``named_rules.py``) runs once,
whenever invoked, and materialises whatever its CONSTRUCT currently derives.
A trigger decides *whether* a rule should run at all, for *which* focus
nodes, and *only once per focus node* until the condition goes false and
true again — none of which a rule has any way to express on its own. The
condition is an ordinary named SELECT (this module places no constraints on
it beyond projecting a ``?focus`` column); the action is an ordinary named
rule, invoked exactly the way ``routes/named_rules.py`` already invokes one,
bound with ``$this=focus`` — no new binding mechanism, no new rule dialect.

**Why firing is edge-triggered, not level-triggered.** Re-running a
condition on every write or every sweep and re-proposing every still-true
match would flood the candidate queue with duplicates of the same proposal.
``urn:{dataset}:trigger-log`` (a new graph role — see ``conn.GRAPH_ROLES``)
is a minimal per-(trigger, focus) firing ledger: an ``ASK`` against it is
what turns "still true" into "was already handled", the same edge-detection
job ``hev:invalidatedAt``/``hev:supersedes`` chains do for fluent history,
scoped here to trigger firings instead.

**Re-arming.** An edge needs a falling side as well as a rising one. Each
evaluation compares the ledger's live firings for a trigger against the
focus nodes its condition matches *now*; a firing whose focus no longer
matches is stamped ``hev:clearedAt`` and stops counting as "already
handled", so the next time that focus matches the trigger fires for it
again. Firings are never deleted — a cleared firing stays in the ledger as
history, and the "already fired" ``ASK`` simply ignores it. Two cautions
follow from this. A condition query that *fails* clears nothing (an
evaluation that could not run says nothing about whether the condition went
false), and a condition must not carry its own ``LIMIT``: a focus that is
still true but pushed out of a truncated result would look like one that
went false, and be re-armed. If a re-armed focus still has a ``Pending``
candidate from its earlier proposal, no second one is staged until that
candidate has been approved or rejected — re-arming must not be able to
flood the review queue, which is the problem the ledger exists to prevent.

**Why ``reviewRequired`` defaults matter.** A trigger whose rule targets
``holons`` — the dataset's own ground truth — mutates the thing everyone
else reads through ``get_holon``. The default (``review_required=True``)
never touches that graph: the bound CONSTRUCT is computed via the named
rule's own ``dry_run`` and staged as a ``holon:CandidateStatus`` proposal in
the new ``urn:{dataset}:candidates`` graph, exactly parallel to the Pass-2
confidence-gate pattern already used for LLM-generated holons. Approving a
candidate is nothing more than merging the exact Turtle it staged — no
second code path, no privileged bypass. ``reviewRequired=False`` runs the
rule for real, through the same ``execute_named_rule`` every manually
invoked rule already goes through; it is not a different, less-audited
write path, only an automatic invocation of the ordinary one.

**Deliberately out of scope for this pass** (see the PR description): no
Toolset/persona reachability filtering on the trigger registry itself (the
firing path runs as a system process, not on behalf of any calling
persona, so this only affects the read-only list/get routes — flagged here
the same way PR #9's Tier 3 was flagged and deferred, not silently
dropped); no predicate-derivation for the ``fluent.py`` write hook, which
currently evaluates every active ``StateTrigger`` after any fluent update
rather than narrowing by ``touched_predicates`` — correct, just not
optimised, and ``touched_predicates`` is left as a real, usable filter for
a caller (or a future optimisation of that hook) that already knows which
predicate changed.
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .acl import Animus
from .conn import Conn
from .fuseki import FusekiClient, FusekiError
from .ingest import write_turtle_to_graph
from .named_queries import NamedQuery, apply_query_params, load_named_queries
from .named_rules import (
    NamedRule,
    RuleError,
    RuleSuspended,
    bind_rule,
    execute_named_rule,
    load_named_rules,
)
from .params import ParameterError
from .rdfutil import collect, local_name, pick, truthy
from .scheduler.vocab import TRIGGER_STATE, TRIGGER_TEMPORAL
from .trigger_authz import TriggerDenied, authorize_definition
from .turtle import escape_literal

log = logging.getLogger("holonbridge.triggers")

HEV_NAMESPACE = "https://w3id.org/holon/event/"

#: Imported, not redefined -- these are the exact two constants the
#: scheduler's own vocabulary already reserved (see the module docstring).
#: Re-exported here so callers of this module don't also need to reach into
#: ``scheduler.vocab`` directly.
TRIGGER_KINDS = (TRIGGER_STATE, TRIGGER_TEMPORAL)

TRIGGER_STATUSES = ("Active", "Suspended", "Deprecated")

TRIGGER_CLASS_SUFFIX = "NamedTrigger"

_TRIGGER_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "identifier"),
    "label": ("label", "title"),
    "description": ("description", "comment"),
    "trigger_kind": ("triggerKind", "kind"),
    "condition": ("condition", "conditionQuery"),
    "on_fire_rule": ("onFire", "onFireRule", "rule"),
    "review_required": ("reviewRequired",),
    "status": ("triggerStatus", "status"),
    "armed_by": ("armedBy",),
    # CHANGED 2026-11-01: an agent action instead of a rule (hev:onFireAgent).
    "agent_endpoint": ("onFireAgent",),
    "agent_task": ("agentTask",),
    "agent_context": ("agentContext",),
    "agent_target_graph": ("agentTargetGraph",),
}
_WATCHED_PREDICATE_LINKS = ("watchedPredicate", "watchedPredicates")


@dataclass
class Trigger:
    """A trigger as loaded from ``urn:{dataset}:named-triggers``."""

    id: str
    iri: str
    trigger_kind: str
    condition_id: str
    on_fire_rule_id: str
    review_required: bool = True
    status: str = "Active"
    label: str = ""
    description: str = ""
    watched_predicates: frozenset[str] = field(default_factory=frozenset)
    #: The Person who last registered or re-activated it. The trigger runs as
    #: the bridge, but only while this person could do what it does: their
    #: access is re-checked at firing (``trigger_authz``). Empty for a trigger
    #: that was hand-authored rather than registered through the API.
    armed_by: str = ""
    #: Set instead of ``on_fire_rule_id``: the id of an ``hb:AgentEndpoint`` to
    #: place a call to, with the task text, an optional context query (a named
    #: SELECT/CONSTRUCT run when the call is placed) and an optional graph the
    #: answer is proposed for. See ``agents/trigger_action.py``.
    agent_endpoint: str = ""
    agent_task: str = ""
    agent_context: str = ""
    agent_target_graph: str = ""

    @property
    def runnable(self) -> bool:
        return self.status == "Active"

    @property
    def is_agent(self) -> bool:
        return bool(self.agent_endpoint)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "label": self.label or self.id,
            "description": self.description,
            "triggerKind": self.trigger_kind,
            "condition": self.condition_id,
            "onFire": self.on_fire_rule_id,
            "reviewRequired": self.review_required,
            "triggerStatus": self.status,
            "watchedPredicates": sorted(self.watched_predicates),
            "armedBy": self.armed_by or None,
            "onFireAgent": {
                "endpoint": self.agent_endpoint,
                "task": self.agent_task,
                "context": self.agent_context or None,
                "targetGraph": self.agent_target_graph or None,
            }
            if self.is_agent
            else None,
        }


@dataclass
class TriggerLoadResult:
    triggers: list[Trigger]
    warnings: list[str] = field(default_factory=list)

    def by_id(self, trigger_id: str) -> Trigger | None:
        for trig in self.triggers:
            if trig.id == trigger_id:
                return trig
        return None


@dataclass
class TriggerFiring:
    """One matched focus node, and what happened with it."""

    trigger_id: str
    focus: str
    #: "proposed" | "executed" | "requested" (an agent call was placed) |
    #: "skipped-pending-candidate" | "skipped-open-call" |
    #: "skipped-definer-unauthorised" | "failed"
    outcome: str
    candidate_iri: str | None = None
    detail: str = ""
    call_iri: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggerId": self.trigger_id,
            "focus": self.focus,
            "outcome": self.outcome,
            "candidate": self.candidate_iri,
            "call": self.call_iri,
            "detail": self.detail,
        }


# --- loading -------------------------------------------------------------------


def _triggers_query(graph: str) -> str:
    return f"""SELECT ?t ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?t a ?type .
    FILTER( STRENDS(STR(?type), "{TRIGGER_CLASS_SUFFIX}") )
    ?t ?p ?o .
  }}
}}"""


def _watched_predicates_query(graph: str) -> str:
    links = " || ".join(
        f'STRENDS(STR(?link), "{name}")' for name in _WATCHED_PREDICATE_LINKS
    )
    return f"""SELECT ?t ?pred
WHERE {{
  GRAPH <{graph}> {{
    ?t a ?type .
    FILTER( STRENDS(STR(?type), "{TRIGGER_CLASS_SUFFIX}") )
    ?t ?link ?pred .
    FILTER( {links} )
  }}
}}"""


async def load_named_triggers(
    client: FusekiClient, conn: Conn, *, graph: str | None = None
) -> TriggerLoadResult:
    """Load every registered trigger from ``urn:{dataset}:named-triggers``.

    Degrades the same way the query and rule registries do: an unreachable
    registry yields an empty result with a warning rather than a failed
    request.
    """
    registry = graph or conn.graph("named-triggers")
    warnings: list[str] = []

    try:
        rows = (await client.select(conn, _triggers_query(registry)))["results"]["bindings"]
    except (FusekiError, KeyError) as exc:
        message = f"named-trigger load from <{registry}> failed: {exc}"
        log.warning(message)
        return TriggerLoadResult(triggers=[], warnings=[message])

    grouped = collect(rows, "t")

    watched: dict[str, set[str]] = {}
    try:
        pred_rows = (await client.select(conn, _watched_predicates_query(registry)))[
            "results"
        ]["bindings"]
        for row in pred_rows:
            watched.setdefault(row["t"]["value"], set()).add(row["pred"]["value"])
    except (FusekiError, KeyError) as exc:
        message = f"watched-predicate metadata load failed: {exc}; triggers evaluate unfiltered"
        log.warning(message)
        warnings.append(message)

    triggers: list[Trigger] = []
    for iri, props in grouped.items():
        kind_raw = pick(props, _TRIGGER_FIELDS["trigger_kind"]) or ""
        kind = local_name(kind_raw) if kind_raw.startswith("http") else kind_raw
        if kind not in TRIGGER_KINDS:
            warnings.append(
                f"<{iri}> has unrecognised triggerKind {kind_raw!r}; skipped "
                f"(expected one of {', '.join(TRIGGER_KINDS)})"
            )
            continue

        condition_id = pick(props, _TRIGGER_FIELDS["condition"])
        rule_id = pick(props, _TRIGGER_FIELDS["on_fire_rule"])
        agent_endpoint = pick(props, _TRIGGER_FIELDS["agent_endpoint"]) or ""
        agent_task = pick(props, _TRIGGER_FIELDS["agent_task"]) or ""
        if not condition_id or not (rule_id or agent_endpoint):
            warnings.append(f"<{iri}> is missing condition and/or onFire; skipped")
            continue
        if rule_id and agent_endpoint:
            warnings.append(f"<{iri}> names both onFire and onFireAgent; skipped")
            continue
        if agent_endpoint and not agent_task.strip():
            warnings.append(f"<{iri}> has onFireAgent but no agentTask; skipped")
            continue

        trigger_id = pick(props, _TRIGGER_FIELDS["id"]) or local_name(iri)
        triggers.append(
            Trigger(
                id=trigger_id,
                iri=iri,
                trigger_kind=kind,
                condition_id=condition_id,
                on_fire_rule_id=rule_id or "",
                agent_endpoint=agent_endpoint,
                agent_task=agent_task,
                agent_context=pick(props, _TRIGGER_FIELDS["agent_context"]) or "",
                agent_target_graph=pick(props, _TRIGGER_FIELDS["agent_target_graph"]) or "",
                review_required=truthy(pick(props, _TRIGGER_FIELDS["review_required"]))
                if pick(props, _TRIGGER_FIELDS["review_required"]) is not None
                else True,
                status=pick(props, _TRIGGER_FIELDS["status"]) or "Active",
                label=pick(props, _TRIGGER_FIELDS["label"]) or trigger_id,
                description=pick(props, _TRIGGER_FIELDS["description"]) or "",
                watched_predicates=frozenset(watched.get(iri, set())),
                armed_by=pick(props, _TRIGGER_FIELDS["armed_by"]) or "",
            )
        )

    triggers.sort(key=lambda t: t.id)
    return TriggerLoadResult(triggers=triggers, warnings=warnings)


# --- firing ledger (edge detection) --------------------------------------------


def _already_fired_ask(log_graph: str, trigger_iri: str, focus: str) -> str:
    """Is there a *live* firing for this (trigger, focus)? A firing stamped
    ``hev:clearedAt`` (see :func:`_rearm`) no longer counts."""
    return f"""PREFIX hev: <{HEV_NAMESPACE}>
ASK {{
  GRAPH <{log_graph}> {{
    ?firing hev:firedTrigger <{trigger_iri}> ;
            hev:firedFor <{focus}> .
    FILTER NOT EXISTS {{ ?firing hev:clearedAt ?cleared }}
  }}
}}"""


def _live_firings_query(log_graph: str, trigger_iri: str) -> str:
    return f"""PREFIX hev: <{HEV_NAMESPACE}>
SELECT ?firing ?focus
WHERE {{
  GRAPH <{log_graph}> {{
    ?firing hev:firedTrigger <{trigger_iri}> ;
            hev:firedFor ?focus .
    FILTER NOT EXISTS {{ ?firing hev:clearedAt ?cleared }}
  }}
}}"""


def _clear_firings_update(log_graph: str, firing_iris: list[str]) -> str:
    now = datetime.now(timezone.utc).isoformat()
    triples = "\n".join(
        f'    <{iri}> hev:clearedAt "{now}"^^xsd:dateTime .' for iri in firing_iris
    )
    return f"""PREFIX hev: <{HEV_NAMESPACE}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT DATA {{
  GRAPH <{log_graph}> {{
{triples}
  }}
}}"""


def _pending_candidate_ask(candidates_graph: str, trigger_iri: str, focus: str) -> str:
    return f"""PREFIX hev: <{HEV_NAMESPACE}>
PREFIX holon: <https://w3id.org/holon/>
ASK {{
  GRAPH <{candidates_graph}> {{
    ?candidate a holon:CandidateStatus ;
               hev:proposedByTrigger <{trigger_iri}> ;
               hev:proposedFor <{focus}> ;
               hev:candidateStatusValue "Pending" .
  }}
}}"""


async def _rearm(
    client: FusekiClient,
    conn: Conn,
    *,
    log_graph: str,
    trigger: Trigger,
    matches: set[str],
) -> int:
    """Stamp ``hev:clearedAt`` on every live firing whose focus no longer
    matches the trigger's condition, and return how many were cleared.

    The falling edge of the edge-triggered contract (see the module
    docstring). Called only after the condition ran successfully, with the
    full set of currently-matching focus nodes.
    """
    rows = (await client.select(conn, _live_firings_query(log_graph, trigger.iri)))[
        "results"
    ]["bindings"]
    stale = sorted(
        {row["firing"]["value"] for row in rows if row["focus"]["value"] not in matches}
    )
    if not stale:
        return 0
    await client.update(conn, _clear_firings_update(log_graph, stale))
    log.info(
        "trigger %s re-armed %d focus node(s) whose condition no longer holds",
        trigger.id,
        len(stale),
    )
    return len(stale)


async def _record_firing(
    client: FusekiClient,
    conn: Conn,
    *,
    log_graph: str,
    trigger: Trigger,
    focus: str,
    outcome: str,
    candidate_iri: str | None,
    firing_iri: str | None = None,
    call_iri: str | None = None,
) -> str:
    """Record one firing. ``firing_iri`` lets the caller mint the IRI first and
    hand the same one to the candidate it stages, so ``hev:proposedFromFiring``
    points at the firing that was actually recorded. ``call_iri`` is the agent
    call an agent trigger placed (``hev:requestedCall``)."""
    firing_iri = firing_iri or f"{log_graph}:{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    clauses = [
        "a hev:TriggerFiring",
        f"hev:firedTrigger <{trigger.iri}>",
        f"hev:firedFor <{focus}>",
        f'hev:firedAt "{now}"^^xsd:dateTime',
        f'hev:outcome "{escape_literal(outcome)}"',
        f"hev:producedCandidate <{candidate_iri}>" if candidate_iri else "",
        f"hev:requestedCall <{call_iri}>" if call_iri else "",
    ]
    body = " ;\n    ".join(c for c in clauses if c)
    await client.update(
        conn,
        f"""PREFIX hev: <{HEV_NAMESPACE}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT DATA {{
  GRAPH <{log_graph}> {{
    <{firing_iri}> {body} .
  }}
}}""",
    )
    return firing_iri


# --- candidate proposals ---------------------------------------------------------


async def _write_candidate(
    client: FusekiClient,
    conn: Conn,
    *,
    trigger: Trigger,
    focus: str,
    firing_iri: str,
    proposed_turtle: str,
    target_graph: str,
) -> str:
    """Stage a rule's already-computed CONSTRUCT output as a review candidate.

    Stores the *materialised Turtle*, not the CONSTRUCT query text that
    produced it — the CONSTRUCT already ran (read-only, via
    ``client.construct``, no live graph touched) by the time this is
    called, so what a reviewer sees is the literal triples that would be
    added, not a query they'd have to mentally execute to find out. This
    also makes ``approve_candidate`` correct: a SPARQL UPDATE endpoint
    cannot accept a CONSTRUCT query, only an update operation or (as used
    here) a Graph Store Protocol merge — the earlier draft of this
    function staged the bound CONSTRUCT text itself, which would have sent
    a query form to the update endpoint and failed on the first real
    approval. Caught by tracing the approve path before this shipped, not
    by a test catching it after.

    Mirrors the Pass-2 confidence-gate shape (``holon:CandidateStatus``) —
    a candidate here is the same kind of thing, just proposed by a trigger
    instead of an LLM generation pass.
    """
    candidates_graph = conn.graph("candidates")
    candidate_id = uuid.uuid4().hex
    candidate_iri = f"{conn.scoped('candidates', candidate_id)}"
    now = datetime.now(timezone.utc).isoformat()
    await client.update(
        conn,
        f"""PREFIX hev: <{HEV_NAMESPACE}>
PREFIX holon: <https://w3id.org/holon/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT DATA {{
  GRAPH <{candidates_graph}> {{
    <{candidate_iri}> a holon:CandidateStatus ;
      hev:proposedByTrigger <{trigger.iri}> ;
      hev:proposedFor <{focus}> ;
      hev:proposedFromFiring <{firing_iri}> ;
      hev:proposedTargetGraph "{escape_literal(target_graph)}" ;
      hev:proposedTurtle "{escape_literal(proposed_turtle)}" ;
      hev:proposedRule "{escape_literal(trigger.on_fire_rule_id)}" ;
      hev:candidateStatusValue "Pending" ;
      hev:proposedAt "{now}"^^xsd:dateTime .
  }}
}}""",
    )
    return candidate_iri


async def propose_pipeline_candidate(
    client: FusekiClient,
    conn: Conn,
    *,
    pipeline_id: str,
    stage_id: str,
    message_iri: str,
    turtle: str,
    target_graph: str,
) -> str:
    """Stage a pipeline stage's caller-supplied Turtle as a review candidate.

    Sibling to :func:`_write_candidate`: same ``holon:CandidateStatus``
    class and the same reviewed-then-merge contract via
    :func:`approve_candidate`, but proposed by a pipeline stage rather than
    a Named Trigger firing. The bridge never calls an LLM itself -- a
    pipeline's ``llm`` stages are always recorded ``Deferred`` (see
    ``pipeline.py``'s module docstring); whatever produced ``turtle``
    outside the bridge (a human, an external completion, a CNL parse)
    submits it here, and from this point on it is gated exactly like every
    other write, through the ordinary ``approve_candidate`` path -- this
    function itself never touches ``target_graph``.
    """
    candidates_graph = conn.graph("candidates")
    candidate_id = uuid.uuid4().hex
    candidate_iri = conn.scoped("candidates", candidate_id)
    now = datetime.now(timezone.utc).isoformat()
    await client.update(
        conn,
        f"""PREFIX hev: <{HEV_NAMESPACE}>
PREFIX holon: <https://w3id.org/holon/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT DATA {{
  GRAPH <{candidates_graph}> {{
    <{candidate_iri}> a holon:CandidateStatus ;
      hev:proposedByPipeline "{escape_literal(pipeline_id)}" ;
      hev:proposedFromStage "{escape_literal(stage_id)}" ;
      hev:proposedFromMessage <{message_iri}> ;
      hev:proposedTargetGraph "{escape_literal(target_graph)}" ;
      hev:proposedTurtle "{escape_literal(turtle)}" ;
      hev:candidateStatusValue "Pending" ;
      hev:proposedAt "{now}"^^xsd:dateTime .
  }}
}}""",
    )
    return candidate_iri


async def propose_agent_candidate(
    client: FusekiClient,
    conn: Conn,
    *,
    call_iri: str,
    agent_id: str,
    turtle: str,
    target_graph: str,
) -> str:
    """Stage an agent's reply as a review candidate.

    Third sibling of :func:`_write_candidate` and
    :func:`propose_pipeline_candidate`, with the same contract: the Turtle sits
    in the candidates graph, ``Pending``, until a person approves it through
    :func:`approve_candidate` -- the ordinary ACL-checked, SHACL-gated write.
    Whatever an external agent says is only ever a proposal; this function
    never touches ``target_graph``.

    ``call_iri`` is the ``hb:AgentCall`` that produced it, so a reviewer can go
    from the proposal to exactly what was asked and by whom.
    """
    candidates_graph = conn.graph("candidates")
    candidate_iri = conn.scoped("candidates", uuid.uuid4().hex)
    now = datetime.now(timezone.utc).isoformat()
    await client.update(
        conn,
        f"""PREFIX hev: <{HEV_NAMESPACE}>
PREFIX holon: <https://w3id.org/holon/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
INSERT DATA {{
  GRAPH <{candidates_graph}> {{
    <{candidate_iri}> a holon:CandidateStatus ;
      hev:proposedByAgentCall <{call_iri}> ;
      hev:proposedByAgent "{escape_literal(agent_id)}" ;
      hev:proposedTargetGraph "{escape_literal(target_graph)}" ;
      hev:proposedTurtle "{escape_literal(turtle)}" ;
      hev:candidateStatusValue "Pending" ;
      hev:proposedAt "{now}"^^xsd:dateTime .
  }}
}}""",
    )
    return candidate_iri


def _candidates_query(graph: str) -> str:
    return f"""PREFIX hev: <{HEV_NAMESPACE}>
SELECT ?c ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?c a <https://w3id.org/holon/CandidateStatus> ; ?p ?o .
  }}
}}"""


@dataclass
class Candidate:
    """A staged write proposal, pending review.

    CHANGED: gained an optional pipeline-shaped provenance set
    (``pipeline_id``/``stage_id``/``message_iri``), alongside the original
    trigger-shaped set (``trigger_iri``/``focus``/``firing_iri``/``rule_id``)
    -- see :func:`propose_pipeline_candidate`. Exactly one provenance set is
    populated per candidate; the other stays all-empty-string. Both sets are
    optional (default ``""``) so existing trigger-only construction sites
    (all of which already pass every field by keyword) need no changes.
    ``pipeline_id``/``stage_id`` are the plain registry ids a caller already
    addresses a pipeline/stage by (``Manifest.by_id``/``PipelineNode.id``),
    not resolved node IRIs; ``message_iri`` is a real resource IRI, since a
    Message is only ever addressed that way.
    """

    iri: str
    target_graph: str
    turtle: str
    status: str
    proposed_at: str
    trigger_iri: str = ""
    focus: str = ""
    firing_iri: str = ""
    rule_id: str = ""
    pipeline_id: str = ""
    stage_id: str = ""
    message_iri: str = ""
    #: Third provenance set: proposed by an external agent in answer to an
    #: ``hb:AgentCall`` (see :func:`propose_agent_candidate`).
    agent_call_iri: str = ""
    agent_id: str = ""
    #: The decision record, written once by :func:`approve_candidate` or
    #: :func:`reject_candidate` and empty while the candidate is Pending:
    #: who decided (``hev:approvedBy`` / ``hev:rejectedBy``), when, why, and,
    #: for an admin acting as someone else, the real person.
    decided_by: str = ""
    decided_at: str = ""
    decision_reason: str = ""
    decided_by_real: str = ""
    decision_id: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "iri": self.iri,
            "trigger": self.trigger_iri,
            "focus": self.focus,
            "firing": self.firing_iri,
            "targetGraph": self.target_graph,
            "rule": self.rule_id,
            "status": self.status,
            "proposedAt": self.proposed_at,
            "pipeline": self.pipeline_id,
            "stage": self.stage_id,
            "message": self.message_iri,
            "agentCall": self.agent_call_iri,
            "agent": self.agent_id,
            "decidedBy": self.decided_by,
            "decidedAt": self.decided_at,
            "decisionReason": self.decision_reason,
            "decidedByReal": self.decided_by_real,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "turtle": self.turtle}


async def list_candidates(
    client: FusekiClient, conn: Conn, *, status: str | None = None
) -> list[Candidate]:
    registry = conn.graph("candidates")
    try:
        rows = (await client.select(conn, _candidates_query(registry)))["results"]["bindings"]
    except (FusekiError, KeyError):
        return []

    out: list[Candidate] = []
    for iri, props in collect(rows, "c").items():
        cand = Candidate(
            iri=iri,
            target_graph=pick(props, ("proposedTargetGraph",)) or "",
            turtle=pick(props, ("proposedTurtle",)) or "",
            status=pick(props, ("candidateStatusValue",)) or "Pending",
            proposed_at=pick(props, ("proposedAt",)) or "",
            trigger_iri=pick(props, ("proposedByTrigger",)) or "",
            focus=pick(props, ("proposedFor",)) or "",
            firing_iri=pick(props, ("proposedFromFiring",)) or "",
            rule_id=pick(props, ("proposedRule",)) or "",
            pipeline_id=pick(props, ("proposedByPipeline",)) or "",
            stage_id=pick(props, ("proposedFromStage",)) or "",
            message_iri=pick(props, ("proposedFromMessage",)) or "",
            agent_call_iri=pick(props, ("proposedByAgentCall",)) or "",
            agent_id=pick(props, ("proposedByAgent",)) or "",
            decided_by=pick(props, ("approvedBy",)) or pick(props, ("rejectedBy",)) or "",
            decided_at=pick(props, ("decidedAt",)) or "",
            decision_reason=pick(props, ("decisionReason",)) or "",
            decided_by_real=pick(props, ("decidedByReal",)) or "",
            decision_id=pick(props, ("decisionId",)) or "",
        )
        if status is None or cand.status == status:
            out.append(cand)
    out.sort(key=lambda c: c.proposed_at)
    return out


async def get_candidate(client: FusekiClient, conn: Conn, candidate_iri: str) -> Candidate | None:
    for cand in await list_candidates(client, conn):
        if cand.iri == candidate_iri:
            return cand
    return None


class CandidateError(RuntimeError):
    """A candidate cannot be resolved as requested."""


#: Longest reason a reviewer may record with a decision.
MAX_DECISION_REASON_CHARS = 1000

_UNSAFE_IRI_CHARS = set('<>"{}|^`\\ \t\r\n')


def _decision_iri(value: str) -> str:
    """An identity IRI that is safe to write between angle brackets."""
    if not value or any(ch in _UNSAFE_IRI_CHARS for ch in value):
        raise CandidateError("the reviewer identity is not a plain IRI")
    return value


def _check_decision_inputs(animus: Animus | None, reason: str | None) -> None:
    """Refuse a bad reason or reviewer identity before anything is written."""
    if len((reason or "").strip()) > MAX_DECISION_REASON_CHARS:
        raise CandidateError(f"the reason is longer than {MAX_DECISION_REASON_CHARS} characters")
    for value in (getattr(animus, "person", None), getattr(animus, "real_person", None)):
        if value:
            _decision_iri(value)


async def _decide_candidate(
    client: FusekiClient,
    conn: Conn,
    *,
    candidate: Candidate,
    status: str,
    animus: Animus | None,
    reason: str | None,
) -> None:
    """Close a Pending candidate and keep the decision on its record.

    The candidate used to carry only its status, overwritten in place, so
    once it read "Approved" or "Rejected" nothing said who decided, when or
    why (the write an approval triggers is audited, but separately and after
    the fact). Now one update replaces the status and adds the reviewer, the
    time, an optional reason and, when an admin is acting as someone else, the
    real person, using the same ``approvedBy`` / ``rejectedBy`` terms the
    agent-call ledger already uses.

    The update is guarded on the candidate still being Pending, and the
    result is read back: two reviewers who both saw Pending cannot both
    record a decision, and the loser gets a :class:`CandidateError` instead of
    a silent overwrite. The read back compares a per-decision id, not the
    timestamp, because stores normalise ``xsd:dateTime`` text differently.
    """
    registry = conn.graph("candidates")
    _check_decision_inputs(animus, reason)
    reason = (reason or "").strip()
    decision_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    who = "approvedBy" if status == "Approved" else "rejectedBy"
    parts = [
        f'hev:candidateStatusValue "{status}"',
        f'hev:decisionId "{decision_id}"',
        f'hev:decidedAt "{now}"^^xsd:dateTime',
    ]
    person = getattr(animus, "person", None)
    if person:
        parts.append(f"hev:{who} <{_decision_iri(person)}>")
    real = getattr(animus, "real_person", None)
    if getattr(animus, "acting_as", False) and real:
        parts.append(f"hev:decidedByReal <{_decision_iri(real)}>")
    if reason:
        parts.append(f'hev:decisionReason "{escape_literal(reason)}"')
    await client.update(
        conn,
        f"""PREFIX hev: <{HEV_NAMESPACE}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
DELETE {{ GRAPH <{registry}> {{ <{candidate.iri}> hev:candidateStatusValue "Pending" }} }}
INSERT {{ GRAPH <{registry}> {{ <{candidate.iri}> {" ; ".join(parts)} }} }}
WHERE  {{ GRAPH <{registry}> {{ <{candidate.iri}> hev:candidateStatusValue "Pending" }} }}""",
    )
    fresh = await get_candidate(client, conn, candidate.iri)
    if fresh is None or fresh.decision_id != decision_id:
        raise CandidateError("the candidate was decided by someone else first")


async def approve_candidate(
    client: FusekiClient,
    conn: Conn,
    candidate: Candidate,
    *,
    animus: Animus,
    shapes_graph: str | None = None,
    reason: str | None = None,
    shacl_required: bool = False,
    shacl_delta: bool = False,
) -> dict[str, Any]:
    """Merge exactly the Turtle the candidate staged into its target graph.

    CHANGED: now routes through ``ingest.write_turtle_to_graph`` -- the
    same ACL-checked, SHACL-gated, write-locked path ``push_turtle``,
    ``create_holon``, ``create_message``, and ``/ingest`` all already use --
    instead of calling ``client.post_graph`` directly. Approving a
    candidate is a write like any other, and it had no ACL check of any
    kind before this; a reviewer's approval authorises merging a
    *specific, already-computed* payload, not a standing bypass of the
    write gate everything else in this codebase goes through. ``animus``
    is therefore now required, exactly as it is for every other call site
    of ``write_turtle_to_graph``.

    Deliberately always a merge (``mode="merge"``, Graph Store Protocol
    POST), regardless of the underlying rule's own declared write mode. An
    unreviewed action that later gets a human's approval should never be
    more destructive than strictly additive — Replace/Sync/Supersede
    semantics involve whole-graph reconciliation against *current* state,
    and the live graph may well have changed between proposal and
    approval, so replaying a reconciliation computed at proposal time
    isn't obviously correct (unlike an add, which is safe regardless of
    what else changed meanwhile). A trigger whose rule genuinely needs
    Replace/Sync/Supersede semantics should be registered with
    ``reviewRequired: false`` instead, which runs the rule for real,
    through its own declared write mode, via ``execute_named_rule`` — this
    function is only ever the reviewed path.

    CHANGED 2026-11-13 (SHACL on approval): ``shacl_required`` and
    ``shacl_delta`` are now passed through, and the approval route supplies the
    deployment's ``SHACL_REQUIRED`` and ``SHACL_DELTA`` settings, so approving a
    candidate is validated exactly as ``push_turtle`` and ``/ingest`` are. The
    shapes graph resolves as for those: an explicit ``shapes_graph`` is used as
    given, ``""`` means "do not validate this approval", and ``None`` falls back
    to the dataset's own shapes graph when ``shacl_required`` is true. With
    ``SHACL_REQUIRED`` off (the default) and no ``shapes_graph``, behaviour is
    unchanged: the merge is unvalidated. A violation refuses the approval with a
    422 and the candidate stays ``Pending``. Direct callers that pass neither
    keyword keep the old behaviour (no validation, full-graph mode).

    Returns whatever ``write_turtle_to_graph`` returns (the validation
    report, when one ran) rather than ``None``, so a caller can see what
    actually happened, not just that nothing raised.
    """
    if candidate.status != "Pending":
        raise CandidateError(f"candidate is already {candidate.status}")
    _check_decision_inputs(animus, reason)  # before the merge, so a bad input writes nothing

    if candidate.turtle.strip():
        result = await write_turtle_to_graph(
            turtle=candidate.turtle,
            graph_iri=candidate.target_graph,
            mode="merge",
            shapes_graph=shapes_graph,
            reduction_rule_id=None,
            conn=conn,
            client=client,
            shacl_required=shacl_required,
            shacl_delta=shacl_delta,
            animus=animus,
        )
    else:
        result = {
            "ok": True,
            "graph": candidate.target_graph,
            "dataset": conn.dataset,
            "mode": "merge",
            "validated": False,
            "validation": None,
        }

    await _decide_candidate(
        client, conn, candidate=candidate, status="Approved", animus=animus, reason=reason
    )
    return result


async def reject_candidate(
    client: FusekiClient,
    conn: Conn,
    candidate: Candidate,
    *,
    animus: Animus | None = None,
    reason: str | None = None,
) -> None:
    if candidate.status != "Pending":
        raise CandidateError(f"candidate is already {candidate.status}")
    await _decide_candidate(
        client, conn, candidate=candidate, status="Rejected", animus=animus, reason=reason
    )


# --- evaluation ------------------------------------------------------------------


async def _run_condition(
    client: FusekiClient, conn: Conn, query: NamedQuery
) -> list[str]:
    """Run a trigger's condition query and return the distinct ``?focus``
    bindings. The condition is an ordinary named SELECT — this is the only
    place this module imposes a shape on it (a ``?focus`` projection)."""
    bound = apply_query_params(query, {})
    results = await client.select(conn, bound.sparql)
    bindings = results.get("results", {}).get("bindings", [])
    focus: list[str] = []
    for row in bindings:
        cell = row.get("focus")
        if cell and cell["value"] not in focus:
            focus.append(cell["value"])
    return focus


# --- re-checking the definer at firing -------------------------------------------

#: How long a verdict on a trigger's definer is reused. The re-check is several
#: queries per trigger and a state trigger is evaluated after every write, so it
#: cannot run each time; this bounds how long a revoked grant keeps working.
DEFINER_RECHECK_TTL_SECONDS = 30.0

_recheck_cache: dict[tuple, tuple[float, str | None]] = {}
_clock: Callable[[], float] = time.monotonic


def clear_definer_cache() -> None:
    """Forget cached verdicts (tests; also useful after an administrative change)."""
    _recheck_cache.clear()


async def _definer_denied(
    client: FusekiClient,
    conn: Conn,
    trig: "Trigger",
    condition: NamedQuery,
    rule: NamedRule | None,
    *,
    endpoint: Any = None,
    context_query: NamedQuery | None = None,
) -> str | None:
    """Why this trigger may not fire right now, or ``None`` if it may.

    A trigger runs as the bridge, but it was accepted on the strength of one
    person's access. If that person can no longer do what it does -- the grant
    was revoked, the delegation expired, the person was removed -- the trigger
    must stop, not carry on with authority nobody holds any more. Nothing is
    written to the ledger for a skipped focus, so it fires normally if access is
    restored.

    A trigger with no recorded definer was hand-authored, before registration
    existed. It runs as it always did unless ``TRIGGERS_REQUIRE_DEFINER`` is set.
    """
    if not trig.armed_by:
        from .config import get_settings  # noqa: PLC0415 - avoids an import cycle

        if get_settings().triggers_require_definer:
            return "no recorded definer, and TRIGGERS_REQUIRE_DEFINER is set"
        return None

    if trig.is_agent:
        key = (
            conn.base_url, conn.dataset, trig.iri, trig.armed_by, trig.review_required,
            condition.sparql, trig.agent_endpoint, trig.agent_target_graph,
            context_query.sparql if context_query else "",
            (endpoint.url, endpoint.status, endpoint.credential_ref, endpoint.signing_ref)
            if endpoint else None,
        )
    else:
        key = (
            conn.base_url, conn.dataset, trig.iri, trig.armed_by, trig.review_required,
            condition.sparql, rule.construct, rule.target_graph, rule.write_mode,
        )
    now = _clock()
    cached = _recheck_cache.get(key)
    if cached is not None and now - cached[0] < DEFINER_RECHECK_TTL_SECONDS:
        return cached[1]

    async def query_fn(query: str) -> dict:
        return await client.select(conn, query)

    verdict: str | None
    try:
        if trig.is_agent:
            from .agents.trigger_action import authorize_agent_trigger  # noqa: PLC0415

            await authorize_agent_trigger(
                query_fn, conn, trig.armed_by,
                condition=condition, endpoint=endpoint, endpoint_id=trig.agent_endpoint,
                context_query=context_query, target_graph=trig.agent_target_graph,
                review_required=trig.review_required,
            )
        else:
            await authorize_definition(
                query_fn, conn, trig.armed_by,
                condition=condition, rule=rule, review_required=trig.review_required,
            )
        verdict = None
    except TriggerDenied as exc:
        verdict = f"definer <{trig.armed_by}> may no longer do this: {exc.reason}"
    except (FusekiError, KeyError) as exc:
        # Could not find out. Fail closed for now, but do not cache it: the
        # next evaluation should ask again.
        return f"could not verify definer <{trig.armed_by}>: {exc}"

    _recheck_cache[key] = (now, verdict)
    if verdict and trig.is_agent:
        # A new refusal, not a cached one: leave a trace in the audit log.
        from .agents.audit import record_trigger_refused  # noqa: PLC0415

        await record_trigger_refused(client, conn, trig, verdict)
    return verdict


# --- re-entrancy ------------------------------------------------------------------

#: True while a trigger evaluation is running in this task. A firing writes (it
#: executes a rule, or stages a candidate), and every write path calls the
#: post-write hook, which evaluates triggers: without this a trigger's own
#: output would re-enter the evaluator it is running inside. Writes made *by* a
#: firing therefore do not themselves fire state triggers; a rule whose output
#: should set off another trigger is picked up by that trigger's next
#: evaluation (the next ordinary write, or a TemporalTrigger sweep).
_evaluating: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "holonbridge_evaluating_triggers", default=False
)

TouchedPredicates = "frozenset[str] | Callable[[], frozenset[str] | None] | None"


async def evaluate_triggers(
    client: FusekiClient,
    conn: Conn,
    *,
    kind: str,
    touched_predicates: frozenset[str] | Callable[[], frozenset[str] | None] | None = None,
    trigger_ids: frozenset[str] | None = None,
) -> list[TriggerFiring]:
    """Evaluate every Active trigger of ``kind`` and act on new matches.

    Re-entrant calls (a firing's own writes reaching the post-write hook) return
    ``[]``; see ``_evaluating``.

    ``trigger_ids``, when supplied, restricts evaluation to those triggers.
    The manual evaluate route passes the one trigger it was asked about:
    evaluating by ``kind`` alone fired every other Active trigger of that kind
    as a side effect, which a caller authorised for one trigger had no claim to.

    ``touched_predicates``, when supplied, narrows evaluation to triggers
    that declared at least one matching ``watchedPredicate`` — an available
    optimisation a caller that knows what changed can opt into. It may be a
    callable, which is only called if some candidate trigger actually declares
    watched predicates (working out what a write touched can mean parsing it);
    a callable that returns ``None`` means "could not tell", and nothing is
    narrowed. A trigger that declares none is always evaluated.

    For each trigger, each currently-matching focus node not already
    recorded in the firing log gets: a ``dry_run`` bind of the trigger's
    rule with ``$this=focus`` (validate/authorise — nothing written yet),
    then either a staged ``CandidateStatus`` proposal (``review_required``)
    or a real ``execute_named_rule`` call (not), then a firing-log record
    either way, so the same focus node is never proposed or executed twice
    for the same trigger while its condition remains true.

    Before acting on matches, any live firing whose focus no longer matches
    is re-armed (see the module docstring's "Re-arming"), so a focus whose
    condition goes false and later true again fires again.
    """
    if _evaluating.get():
        return []
    token = _evaluating.set(True)
    try:
        return await _evaluate(
            client, conn, kind=kind, touched_predicates=touched_predicates, trigger_ids=trigger_ids
        )
    finally:
        _evaluating.reset(token)


async def _evaluate(
    client: FusekiClient,
    conn: Conn,
    *,
    kind: str,
    touched_predicates: frozenset[str] | Callable[[], frozenset[str] | None] | None,
    trigger_ids: frozenset[str] | None,
) -> list[TriggerFiring]:
    load = await load_named_triggers(client, conn)
    triggers = [t for t in load.triggers if t.runnable and t.trigger_kind == kind]
    if trigger_ids is not None:
        triggers = [t for t in triggers if t.id in trigger_ids]
    if touched_predicates is not None and any(t.watched_predicates for t in triggers):
        touched = touched_predicates() if callable(touched_predicates) else touched_predicates
        if touched is not None:
            triggers = [
                t
                for t in triggers
                if not t.watched_predicates or t.watched_predicates & touched
            ]

    if not triggers:
        return []

    queries = await load_named_queries(client, conn)
    rules = await load_named_rules(client, conn)
    log_graph = conn.graph("trigger-log")

    # Only agent triggers need the endpoint registry; do not read it otherwise.
    agent_endpoints: dict[str, Any] = {}
    if any(t.is_agent for t in triggers):
        from .agents.registry import load_endpoints  # noqa: PLC0415

        loaded = await load_endpoints(client, conn)
        agent_endpoints = {e.id: e for e in loaded.endpoints}

    firings: list[TriggerFiring] = []
    for trig in triggers:
        condition = queries.by_id(trig.condition_id)
        rule = None if trig.is_agent else rules.by_id(trig.on_fire_rule_id)
        agent_endpoint = agent_endpoints.get(trig.agent_endpoint) if trig.is_agent else None
        agent_context = (
            queries.by_id(trig.agent_context) if trig.is_agent and trig.agent_context else None
        )
        if condition is None:
            firings.append(
                TriggerFiring(
                    trig.id, "", "failed", detail=f"unknown condition {trig.condition_id!r}"
                )
            )
            continue
        if trig.is_agent and agent_endpoint is None:
            firings.append(
                TriggerFiring(
                    trig.id, "", "failed",
                    detail=f"unknown agent endpoint {trig.agent_endpoint!r}",
                )
            )
            continue
        if trig.is_agent and trig.agent_context and agent_context is None:
            firings.append(
                TriggerFiring(
                    trig.id, "", "failed",
                    detail=f"unknown context query {trig.agent_context!r}",
                )
            )
            continue
        if not trig.is_agent and rule is None:
            firings.append(
                TriggerFiring(
                    trig.id, "", "failed", detail=f"unknown rule {trig.on_fire_rule_id!r}"
                )
            )
            continue

        try:
            matches = await _run_condition(client, conn, condition)
        except FusekiError as exc:
            firings.append(
                TriggerFiring(trig.id, "", "failed", detail=f"condition query failed: {exc}")
            )
            continue

        # The falling edge: anything that fired before and no longer matches
        # is re-armed. Only reached when the condition actually ran, so a
        # failed evaluation (the ``continue`` above) never clears a firing.
        try:
            await _rearm(client, conn, log_graph=log_graph, trigger=trig, matches=set(matches))
        except (FusekiError, KeyError) as exc:
            # Not fatal: the worst case is a focus that stays "already
            # fired" until the next evaluation manages to clear it.
            log.warning("trigger %s could not be re-armed: %s", trig.id, exc)

        denial: str | None = None
        checked = False
        for focus in matches:
            already = await client.select(
                conn, _already_fired_ask(log_graph, trig.iri, focus)
            )
            if already.get("boolean"):
                continue  # edge-triggered: this (trigger, focus) pair already fired

            # Asked at most once per trigger per evaluation, and only when
            # there is something to fire, so a quiet evaluation costs nothing.
            if not checked:
                denial = await _definer_denied(
                    client, conn, trig, condition, rule,
                    endpoint=agent_endpoint, context_query=agent_context,
                )
                checked = True
                if denial:
                    log.warning("trigger %s not fired: %s", trig.id, denial)
            if denial:
                firings.append(
                    TriggerFiring(trig.id, focus, "skipped-definer-unauthorised", detail=denial)
                )
                continue

            try:
                if trig.is_agent:
                    from .agents.service import CallInvalid  # noqa: PLC0415
                    from .agents.trigger_action import fire_agent  # noqa: PLC0415

                    try:
                        call = await fire_agent(
                            client, conn, trig, focus,
                            endpoints=agent_endpoints, queries=queries,
                        )
                    except (CallInvalid, TriggerDenied) as exc:
                        reason = exc.reason if isinstance(exc, TriggerDenied) else str(exc)
                        log.warning("trigger %s could not place a call for <%s>: %s", trig.id, focus, reason)
                        firings.append(TriggerFiring(trig.id, focus, "failed", detail=reason))
                        continue
                    if call is None:
                        firings.append(
                            TriggerFiring(
                                trig.id, focus, "skipped-open-call",
                                detail="an earlier call for this focus is still open",
                            )
                        )
                        continue
                    await _record_firing(
                        client, conn, log_graph=log_graph, trigger=trig, focus=focus,
                        outcome="requested", candidate_iri=None, call_iri=call.iri,
                    )
                    firings.append(
                        TriggerFiring(
                            trig.id, focus, "requested",
                            detail=f"call {call.id} {call.status}", call_iri=call.iri,
                        )
                    )
                elif trig.review_required:
                    # A re-armed focus may still have the proposal from its
                    # previous firing waiting for review. Do not stack another
                    # on top of it; try again once that one is resolved.
                    outstanding = await client.select(
                        conn,
                        _pending_candidate_ask(conn.graph("candidates"), trig.iri, focus),
                    )
                    if outstanding.get("boolean"):
                        firings.append(
                            TriggerFiring(
                                trig.id,
                                focus,
                                "skipped-pending-candidate",
                                detail="an earlier proposal for this focus is still Pending",
                            )
                        )
                        continue

                    sparql = bind_rule(rule, {"$this": focus})
                    # Read-only: computes what the rule would produce
                    # without touching any live graph, the same operation
                    # execute_named_rule itself performs first.
                    turtle = await client.construct(conn, sparql)
                    firing_iri = f"{log_graph}:{uuid.uuid4().hex}"
                    candidate_iri = await _write_candidate(
                        client,
                        conn,
                        trigger=trig,
                        focus=focus,
                        firing_iri=firing_iri,
                        proposed_turtle=turtle,
                        target_graph=rule.target_graph,
                    )
                    await _record_firing(
                        client,
                        conn,
                        log_graph=log_graph,
                        trigger=trig,
                        focus=focus,
                        outcome="proposed",
                        candidate_iri=candidate_iri,
                        firing_iri=firing_iri,
                    )
                    firings.append(
                        TriggerFiring(trig.id, focus, "proposed", candidate_iri=candidate_iri)
                    )
                else:
                    await execute_named_rule(conn, client, rule, params={"$this": focus})
                    await _record_firing(
                        client,
                        conn,
                        log_graph=log_graph,
                        trigger=trig,
                        focus=focus,
                        outcome="executed",
                        candidate_iri=None,
                    )
                    firings.append(TriggerFiring(trig.id, focus, "executed"))
            except (RuleError, RuleSuspended, ParameterError, FusekiError) as exc:
                log.warning(
                    "trigger %s failed for focus <%s>: %s", trig.id, focus, exc
                )
                firings.append(
                    TriggerFiring(trig.id, focus, "failed", detail=str(exc))
                )

    return firings
