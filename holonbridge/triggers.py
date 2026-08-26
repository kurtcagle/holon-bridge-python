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

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .conn import Conn
from .fuseki import FusekiClient, FusekiError
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

    @property
    def runnable(self) -> bool:
        return self.status == "Active"

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
    outcome: str  # "proposed" | "executed" | "skipped-already-fired" | "failed"
    candidate_iri: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggerId": self.trigger_id,
            "focus": self.focus,
            "outcome": self.outcome,
            "candidate": self.candidate_iri,
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
        if not condition_id or not rule_id:
            warnings.append(f"<{iri}> is missing condition and/or onFire; skipped")
            continue

        trigger_id = pick(props, _TRIGGER_FIELDS["id"]) or local_name(iri)
        triggers.append(
            Trigger(
                id=trigger_id,
                iri=iri,
                trigger_kind=kind,
                condition_id=condition_id,
                on_fire_rule_id=rule_id,
                review_required=truthy(pick(props, _TRIGGER_FIELDS["review_required"]))
                if pick(props, _TRIGGER_FIELDS["review_required"]) is not None
                else True,
                status=pick(props, _TRIGGER_FIELDS["status"]) or "Active",
                label=pick(props, _TRIGGER_FIELDS["label"]) or trigger_id,
                description=pick(props, _TRIGGER_FIELDS["description"]) or "",
                watched_predicates=frozenset(watched.get(iri, set())),
            )
        )

    triggers.sort(key=lambda t: t.id)
    return TriggerLoadResult(triggers=triggers, warnings=warnings)


# --- firing ledger (edge detection) --------------------------------------------


def _already_fired_ask(log_graph: str, trigger_iri: str, focus: str) -> str:
    return f"""PREFIX hev: <{HEV_NAMESPACE}>
ASK {{
  GRAPH <{log_graph}> {{
    ?firing hev:firedTrigger <{trigger_iri}> ;
            hev:firedFor <{focus}> .
  }}
}}"""


async def _record_firing(
    client: FusekiClient,
    conn: Conn,
    *,
    log_graph: str,
    trigger: Trigger,
    focus: str,
    outcome: str,
    candidate_iri: str | None,
) -> str:
    firing_iri = f"{conn.graph('trigger-log')}:{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    clauses = [
        "a hev:TriggerFiring",
        f"hev:firedTrigger <{trigger.iri}>",
        f"hev:firedFor <{focus}>",
        f'hev:firedAt "{now}"^^xsd:dateTime',
        f'hev:outcome "{escape_literal(outcome)}"',
        f"hev:producedCandidate <{candidate_iri}>" if candidate_iri else "",
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
    iri: str
    trigger_iri: str
    focus: str
    firing_iri: str
    target_graph: str
    turtle: str
    rule_id: str
    status: str
    proposed_at: str

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
            trigger_iri=pick(props, ("proposedByTrigger",)) or "",
            focus=pick(props, ("proposedFor",)) or "",
            firing_iri=pick(props, ("proposedFromFiring",)) or "",
            target_graph=pick(props, ("proposedTargetGraph",)) or "",
            turtle=pick(props, ("proposedTurtle",)) or "",
            rule_id=pick(props, ("proposedRule",)) or "",
            status=pick(props, ("candidateStatusValue",)) or "Pending",
            proposed_at=pick(props, ("proposedAt",)) or "",
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


async def _set_candidate_status(
    client: FusekiClient, conn: Conn, *, candidate: Candidate, status: str
) -> None:
    registry = conn.graph("candidates")
    await client.update(
        conn,
        f"""PREFIX hev: <{HEV_NAMESPACE}>
DELETE {{ GRAPH <{registry}> {{ <{candidate.iri}> hev:candidateStatusValue ?old }} }}
INSERT {{ GRAPH <{registry}> {{ <{candidate.iri}> hev:candidateStatusValue "{status}" }} }}
WHERE  {{ GRAPH <{registry}> {{ <{candidate.iri}> hev:candidateStatusValue ?old }} }}""",
    )


async def approve_candidate(
    client: FusekiClient, conn: Conn, candidate: Candidate
) -> None:
    """Merge exactly the Turtle the candidate staged into its target graph.

    Deliberately always a merge (Graph Store Protocol POST), regardless of
    the underlying rule's own declared write mode. An unreviewed action
    that later gets a human's approval should never be more destructive
    than strictly additive — Replace/Sync/Supersede semantics involve
    whole-graph reconciliation against *current* state, and the live graph
    may well have changed between proposal and approval, so replaying a
    reconciliation computed at proposal time isn't obviously correct
    (unlike an add, which is safe regardless of what else changed
    meanwhile). A trigger whose rule genuinely needs Replace/Sync/Supersede
    semantics should be registered with ``reviewRequired: false`` instead,
    which runs the rule for real, through its own declared write mode, via
    ``execute_named_rule`` — this function is only ever the reviewed path.
    """
    if candidate.status != "Pending":
        raise CandidateError(f"candidate is already {candidate.status}")
    if candidate.turtle.strip():
        await client.post_graph(conn, candidate.target_graph, candidate.turtle)
    await _set_candidate_status(client, conn, candidate=candidate, status="Approved")


async def reject_candidate(
    client: FusekiClient, conn: Conn, candidate: Candidate
) -> None:
    if candidate.status != "Pending":
        raise CandidateError(f"candidate is already {candidate.status}")
    await _set_candidate_status(client, conn, candidate=candidate, status="Rejected")


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


async def evaluate_triggers(
    client: FusekiClient,
    conn: Conn,
    *,
    kind: str,
    touched_predicates: frozenset[str] | None = None,
) -> list[TriggerFiring]:
    """Evaluate every Active trigger of ``kind`` and act on new matches.

    ``touched_predicates``, when supplied, narrows evaluation to triggers
    that declared at least one matching ``watchedPredicate`` — an available
    optimisation a caller that knows what changed can opt into. The
    ``fluent.py`` write hook does not supply it yet (see the module
    docstring); every other caller is free to.

    For each trigger, each currently-matching focus node not already
    recorded in the firing log gets: a ``dry_run`` bind of the trigger's
    rule with ``$this=focus`` (validate/authorise — nothing written yet),
    then either a staged ``CandidateStatus`` proposal (``review_required``)
    or a real ``execute_named_rule`` call (not), then a firing-log record
    either way, so the same focus node is never proposed or executed twice
    for the same trigger while its condition remains true.
    """
    load = await load_named_triggers(client, conn)
    triggers = [t for t in load.triggers if t.runnable and t.trigger_kind == kind]
    if touched_predicates is not None:
        triggers = [
            t
            for t in triggers
            if not t.watched_predicates or t.watched_predicates & touched_predicates
        ]

    if not triggers:
        return []

    queries = await load_named_queries(client, conn)
    rules = await load_named_rules(client, conn)
    log_graph = conn.graph("trigger-log")

    firings: list[TriggerFiring] = []
    for trig in triggers:
        condition = queries.by_id(trig.condition_id)
        rule = rules.by_id(trig.on_fire_rule_id)
        if condition is None:
            firings.append(
                TriggerFiring(
                    trig.id, "", "failed", detail=f"unknown condition {trig.condition_id!r}"
                )
            )
            continue
        if rule is None:
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

        for focus in matches:
            already = await client.select(
                conn, _already_fired_ask(log_graph, trig.iri, focus)
            )
            if already.get("boolean"):
                continue  # edge-triggered: this (trigger, focus) pair already fired

            try:
                if trig.review_required:
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
