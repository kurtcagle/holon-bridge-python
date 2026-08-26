"""Named-trigger tests.

Same technique as ``test_named_rules.py``'s ``RuleStub``: a stub
``FusekiClient`` that answers each query by recognising its shape (the
loaders here, and the ones this module reuses from ``named_queries.py``/
``named_rules.py``, each generate a handful of fixed, distinguishable
``SELECT`` forms), rather than implementing a triplestore.

Run standalone against a reconstructed package tree (conn.py, fuseki.py,
rdfutil.py, params.py, turtle.py, sparql_kind.py, named_queries.py,
named_rules.py, scheduler/vocab.py, triggers.py — all real file content,
fetched from the repo, not reduced) plus a test-only stub
``scheduler/__init__.py`` so ``from .scheduler.vocab import ...`` resolves
without needing ``runner.py``/``proposer.py``/``store.py`` and everything
*they* import — the real ``scheduler/__init__.py`` is untouched by this PR
and is exercised for real via ``server.py``'s router wiring instead. Not
yet run against a full checkout with the FastAPI route layer (routes/triggers.py) 
included — same caveat PR #7/#8/#9 each carried forward; see the PR
description.
"""

from __future__ import annotations

import re

import pytest

from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiError
from holonbridge.named_rules import RuleError
from holonbridge.triggers import (
    CandidateError,
    TRIGGER_STATE,
    TRIGGER_TEMPORAL,
    approve_candidate,
    evaluate_triggers,
    get_candidate,
    list_candidates,
    load_named_triggers,
    reject_candidate,
)

HEV = "https://w3id.org/holon/event/"
HB = "https://w3id.org/holonbridge/"
TRIGGER_TYPE = f"{HEV}NamedTrigger"


def _row(**cells: str) -> dict:
    return {key: {"value": value} for key, value in cells.items()}


def trigger_rows() -> list[dict]:
    rows: list[dict] = []

    def trig(iri: str, **props: str) -> None:
        for key, value in props.items():
            rows.append(_row(t=iri, type=TRIGGER_TYPE, p=f"{HEV}{key}", o=value))

    trig(
        "urn:ds:named-trigger:min-age-violation",
        id="min-age-violation",
        label="Under-age player on an adult team",
        triggerKind="StateTrigger",
        condition="select-underage-varsity",
        onFire="flag-violation",
        reviewRequired="true",
        triggerStatus="Active",
    )
    trig(
        # triggerKind given as a full IRI, not a bare local name -- exercises
        # the local_name() branch in load_named_triggers.
        "urn:ds:named-trigger:varsity-promotion",
        id="varsity-promotion",
        label="Player aged into Varsity eligibility",
        triggerKind=f"{HEV}TemporalTrigger",
        condition="select-newly-eligible",
        onFire="propose-promotion",
        triggerStatus="Active",
        # reviewRequired omitted -- must default to True
    )
    trig(
        "urn:ds:named-trigger:auto-log",
        id="auto-log",
        label="Auto-executing, low-stakes",
        triggerKind="StateTrigger",
        condition="select-something",
        onFire="log-rule",
        reviewRequired="false",
        triggerStatus="Active",
    )
    trig(
        "urn:ds:named-trigger:broken-condition",
        id="broken-condition",
        triggerKind="StateTrigger",
        condition="no-such-query",
        onFire="flag-violation",
        triggerStatus="Active",
    )
    trig(
        "urn:ds:named-trigger:broken-rule",
        id="broken-rule",
        triggerKind="StateTrigger",
        condition="select-underage-varsity",
        onFire="no-such-rule",
        triggerStatus="Active",
    )
    trig(
        "urn:ds:named-trigger:suspended-one",
        id="suspended-one",
        triggerKind="StateTrigger",
        condition="select-underage-varsity",
        onFire="flag-violation",
        triggerStatus="Suspended",
    )
    trig(
        "urn:ds:named-trigger:bad-kind",
        id="bad-kind",
        triggerKind="NotARealKind",
        condition="select-something",
        onFire="log-rule",
        triggerStatus="Active",
    )
    trig(
        # no condition/onFire at all
        "urn:ds:named-trigger:homeless",
        id="homeless",
        triggerKind="StateTrigger",
        triggerStatus="Active",
    )
    return rows


def watched_predicate_rows() -> list[dict]:
    return [
        _row(
            t="urn:ds:named-trigger:min-age-violation",
            pred="https://w3id.org/sportsleague/rosterMemberOf",
        )
    ]


def named_query_rows() -> list[dict]:
    rows: list[dict] = []

    def q(iri: str, sparql: str, **props: str) -> None:
        rows.append(_row(q=iri, type=f"{HB}NamedQuery", p=f"{HB}id", o=props["id"]))
        rows.append(_row(q=iri, type=f"{HB}NamedQuery", p=f"{HB}sparql", o=sparql))

    q(
        "urn:ds:named-query:select-underage-varsity",
        "SELECT ?focus WHERE { ?focus <urn:test:marker> \"underage\" }",
        id="select-underage-varsity",
    )
    q(
        "urn:ds:named-query:select-newly-eligible",
        "SELECT ?focus WHERE { ?focus <urn:test:marker> \"eligible\" }",
        id="select-newly-eligible",
    )
    q(
        "urn:ds:named-query:select-something",
        "SELECT ?focus WHERE { ?focus <urn:test:marker> \"something\" }",
        id="select-something",
    )
    return rows


def named_rule_rows() -> list[dict]:
    rows: list[dict] = []

    def r(iri: str, **props: str) -> None:
        for key, value in props.items():
            rows.append(_row(r=iri, type=f"{HB}NamedRule", p=f"{HB}{key}", o=value))

    r(
        "urn:ds:named-rule:flag-violation",
        id="flag-violation",
        construct="CONSTRUCT { $this <urn:test:flagged> true } WHERE { }",
        targetGraph="urn:ds:candidates-target",
        writeMode="Append",
        ruleStatus="Active",
    )
    r(
        "urn:ds:named-rule:propose-promotion",
        id="propose-promotion",
        construct="CONSTRUCT { $this <urn:test:promoted> true } WHERE { }",
        targetGraph="urn:ds:candidates-target",
        writeMode="Append",
        ruleStatus="Active",
    )
    r(
        "urn:ds:named-rule:log-rule",
        id="log-rule",
        construct="CONSTRUCT { $this <urn:test:logged> true } WHERE { }",
        targetGraph="urn:ds:log-target",
        writeMode="Append",
        ruleStatus="Active",
    )
    return rows


_CANDIDATE_CREATE = re.compile(
    r"GRAPH <[^>]+> \{ <([^>]+)> a holon:CandidateStatus ;\s*"
    r"hev:proposedByTrigger <([^>]+)> ;\s*"
    r"hev:proposedFor <([^>]+)> ;\s*"
    r"hev:proposedFromFiring <([^>]+)> ;\s*"
    r'hev:proposedTargetGraph "([^"]*)" ;\s*'
    r'hev:proposedTurtle "([^"]*)" ;\s*'
    r'hev:proposedRule "([^"]*)" ;'
)
_CANDIDATE_STATUS_SET = re.compile(
    r'INSERT \{ GRAPH <[^>]+> \{ <([^>]+)> hev:candidateStatusValue "([^"]+)" \} \}'
)

_UNESCAPE = {"\\\\": "\\", '\\"': '"', "\\n": "\n", "\\r": "\r", "\\t": "\t"}
_UNESCAPE_PATTERN = re.compile("|".join(re.escape(k) for k in _UNESCAPE))


def _unescape_literal(text: str) -> str:
    """Reverse ``escape_literal`` -- what a real store's own SELECT would
    hand back (the semantic value), not the escaped-for-Turtle-syntax
    lexical form this stub captured verbatim off the update text."""
    return _UNESCAPE_PATTERN.sub(lambda m: _UNESCAPE[m.group(0)], text)


class TriggerStub:
    """Answers each fixed query shape this module and its dependencies
    generate; records every write for assertions.

    Candidates are the one thing this stub actually has to behave like a
    small store for, rather than just pattern-matching a query shape and
    returning canned rows: the INSERT DATA this module writes for a staged
    candidate, and the DELETE/INSERT it writes on approve/reject, are
    parsed back out of the (fixed, module-controlled) update text into an
    in-memory dict, and ``_candidates_query``'s ``SELECT ?c ?p ?o`` shape
    is answered from that dict. Everything else stays a pure shape match.
    """

    def __init__(self) -> None:
        self.trigger_rows = trigger_rows()
        self.watched_rows = watched_predicate_rows()
        self.query_rows = named_query_rows()
        self.rule_rows = named_rule_rows()
        self.focus_rows: dict[str, list[str]] = {
            "underage": ["urn:ds:person:isla"],
            "eligible": ["urn:ds:person:isla"],
            "something": ["urn:ds:person:michaela"],
        }
        self.already_fired: set[tuple[str, str]] = set()
        self.updates: list[str] = []
        self.constructed: list[str] = []
        self.pushed: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        self.turtle = "<urn:test:s> <urn:test:p> <urn:test:o> .\n"
        self._candidates: dict[str, dict[str, str]] = {}

    async def select(self, conn, query, *, default_graph=None):
        if "ASK {" in query and "firedTrigger" in query:
            for trig_iri, focus in self.already_fired:
                if trig_iri in query and focus in query:
                    return {"boolean": True}
            return {"boolean": False}

        if "SELECT ?c ?p ?o" in query and "CandidateStatus" in query:
            rows = []
            for iri, fields in self._candidates.items():
                for pred, value in fields.items():
                    rows.append(_row(c=iri, p=f"{HEV}{pred}", o=value))
            return {"results": {"bindings": rows}}

        if "SELECT ?t ?pred" in query:
            return {"results": {"bindings": self.watched_rows}}
        if "SELECT ?t ?p ?o" in query:
            return {"results": {"bindings": self.trigger_rows}}

        if "SELECT ?q ?param ?p ?o" in query:
            return {"results": {"bindings": []}}
        if "SELECT ?q ?type ?p ?o" in query:
            return {"results": {"bindings": self.query_rows}}

        if "SELECT ?r ?param ?p ?o" in query:
            return {"results": {"bindings": []}}
        if "SELECT ?r ?p ?o" in query:
            return {"results": {"bindings": self.rule_rows}}

        if "COUNT(*)" in query:
            return {"results": {"bindings": [{"n": {"value": "1"}}]}}

        for marker, focus_list in self.focus_rows.items():
            if f'"{marker}"' in query:
                return {
                    "results": {
                        "bindings": [{"focus": {"type": "uri", "value": f}} for f in focus_list]
                    }
                }
        return {"results": {"bindings": []}}

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        self.constructed.append(query)
        return self.turtle

    async def update(self, conn, update):
        normalised = " ".join(update.split())
        self.updates.append(normalised)

        created = _CANDIDATE_CREATE.search(normalised)
        if created:
            iri, trigger_iri, focus, firing_iri, target_graph, turtle, rule_id = created.groups()
            self._candidates[iri] = {
                "proposedByTrigger": trigger_iri,
                "proposedFor": focus,
                "proposedFromFiring": firing_iri,
                "proposedTargetGraph": _unescape_literal(target_graph),
                "proposedTurtle": _unescape_literal(turtle),
                "proposedRule": _unescape_literal(rule_id),
                "candidateStatusValue": "Pending",
                "proposedAt": "2026-08-26T00:00:00+00:00",
            }
            return

        status_set = _CANDIDATE_STATUS_SET.search(normalised)
        if status_set:
            iri, status = status_set.groups()
            if iri in self._candidates:
                self._candidates[iri]["candidateStatusValue"] = status

    async def post_graph(self, conn, graph_iri, turtle):
        self.pushed.append((graph_iri, turtle))

    async def drop_graph(self, conn, graph_iri):
        self.dropped.append(graph_iri)

    def updates_matching(self, keyword: str) -> list[str]:
        return [u for u in self.updates if keyword in u]


@pytest.fixture
def stub() -> TriggerStub:
    return TriggerStub()


@pytest.fixture
def conn() -> Conn:
    return Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")


# --- loading -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loads_active_and_inactive_triggers(stub, conn):
    result = await load_named_triggers(stub, conn)
    ids = {t.id for t in result.triggers}
    assert "min-age-violation" in ids
    assert "suspended-one" in ids  # loaded, just not runnable
    assert not result.by_id("suspended-one").runnable


@pytest.mark.asyncio
async def test_trigger_kind_accepts_full_iri_form(stub, conn):
    result = await load_named_triggers(stub, conn)
    trig = result.by_id("varsity-promotion")
    assert trig.trigger_kind == TRIGGER_TEMPORAL


@pytest.mark.asyncio
async def test_review_required_defaults_true_when_absent(stub, conn):
    result = await load_named_triggers(stub, conn)
    assert result.by_id("varsity-promotion").review_required is True
    assert result.by_id("auto-log").review_required is False


@pytest.mark.asyncio
async def test_unrecognised_kind_is_skipped_with_a_warning(stub, conn):
    result = await load_named_triggers(stub, conn)
    assert "bad-kind" not in [t.id for t in result.triggers]
    assert any("unrecognised triggerKind" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_missing_condition_or_rule_is_skipped_with_a_warning(stub, conn):
    result = await load_named_triggers(stub, conn)
    assert "homeless" not in [t.id for t in result.triggers]
    assert any("missing condition and/or onFire" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_watched_predicates_load(stub, conn):
    result = await load_named_triggers(stub, conn)
    trig = result.by_id("min-age-violation")
    assert "https://w3id.org/sportsleague/rosterMemberOf" in trig.watched_predicates
    assert result.by_id("varsity-promotion").watched_predicates == frozenset()


# --- evaluation: matching, dedup, kind filtering --------------------------------


@pytest.mark.asyncio
async def test_only_matching_kind_is_evaluated(stub, conn):
    firings = await evaluate_triggers(stub, conn, kind=TRIGGER_TEMPORAL)
    ids = {f.trigger_id for f in firings}
    assert ids == {"varsity-promotion"}  # the only Active TemporalTrigger


@pytest.mark.asyncio
async def test_suspended_trigger_never_evaluated(stub, conn):
    firings = await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    assert "suspended-one" not in {f.trigger_id for f in firings}


@pytest.mark.asyncio
async def test_already_fired_focus_is_skipped(stub, conn):
    stub.already_fired.add(("min-age-violation", "urn:ds:person:isla"))
    firings = await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    min_age = [f for f in firings if f.trigger_id == "min-age-violation"]
    assert min_age == []  # the only match was already fired


@pytest.mark.asyncio
async def test_touched_predicates_narrows_to_declared_watchers(stub, conn):
    # min-age-violation watches rosterMemberOf; auto-log declares nothing
    # (so it always evaluates, per the "no predicates declared = unfiltered"
    # rule) -- only min-age-violation should be excluded here.
    firings = await evaluate_triggers(
        stub, conn, kind=TRIGGER_STATE, touched_predicates=frozenset({"urn:unrelated:predicate"})
    )
    ids = {f.trigger_id for f in firings}
    assert "min-age-violation" not in ids
    assert "auto-log" in ids


# --- evaluation: review_required -> candidate staging ---------------------------


@pytest.mark.asyncio
async def test_review_required_stages_a_candidate_not_a_live_write(stub, conn):
    firings = await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    hit = next(f for f in firings if f.trigger_id == "min-age-violation")
    assert hit.outcome == "proposed"
    assert hit.candidate_iri is not None
    # the rule's own target graph was never touched -- ADD SILENT does
    # appear in stub.updates from this same evaluate_triggers() pass, but
    # from auto-log's separate, legitimate reviewRequired=false execution
    # (target urn:ds:log-target) -- asserting no ADD SILENT at all would
    # incorrectly fail on that unrelated trigger's real write.
    assert not any("urn:ds:candidates-target" in g for g, _ in stub.pushed)
    assert not any(
        "urn:ds:candidates-target" in u for u in stub.updates_matching("ADD SILENT")
    )


@pytest.mark.asyncio
async def test_staged_candidate_carries_the_computed_turtle_not_a_query(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    candidates = await list_candidates(stub, conn)
    hit = next(c for c in candidates if c.trigger_iri.endswith("min-age-violation"))
    assert hit.turtle == stub.turtle
    assert "CONSTRUCT" not in hit.turtle  # it's the materialised output, not the query
    assert hit.status == "Pending"
    assert hit.target_graph == "urn:ds:candidates-target"


@pytest.mark.asyncio
async def test_candidate_construct_is_read_only(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    assert stub.constructed  # the CONSTRUCT did run
    # Nothing was merged into the candidate's own target graph. (A scratch
    # graph does appear in stub.pushed -- that's auto-log's separate,
    # legitimate execute_named_rule call in this same pass, an unrelated
    # trigger, not this one.)
    assert not any(g == "urn:ds:candidates-target" for g, _ in stub.pushed)


# --- evaluation: reviewRequired=false -> real execution -------------------------


@pytest.mark.asyncio
async def test_review_not_required_executes_for_real(stub, conn):
    firings = await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    hit = next(f for f in firings if f.trigger_id == "auto-log")
    assert hit.outcome == "executed"
    assert hit.candidate_iri is None
    # execute_named_rule's own Append-mode write actually ran
    assert stub.updates_matching("ADD SILENT")


# --- evaluation: failure paths ---------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_condition_is_reported_not_raised(stub, conn):
    firings = await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    hit = next(f for f in firings if f.trigger_id == "broken-condition")
    assert hit.outcome == "failed"
    assert "unknown condition" in hit.detail


@pytest.mark.asyncio
async def test_unknown_rule_is_reported_not_raised(stub, conn):
    firings = await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    hit = next(f for f in firings if f.trigger_id == "broken-rule")
    assert hit.outcome == "failed"
    assert "unknown rule" in hit.detail


# --- candidate review queue ------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_merges_the_staged_turtle_into_the_target_graph(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    candidates = await list_candidates(stub, conn, status="Pending")
    hit = next(c for c in candidates if c.trigger_iri.endswith("min-age-violation"))

    await approve_candidate(stub, conn, hit)

    assert (hit.target_graph, stub.turtle) in stub.pushed
    refreshed = await get_candidate(stub, conn, hit.iri)
    assert refreshed.status == "Approved"


@pytest.mark.asyncio
async def test_reject_never_touches_the_target_graph(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    candidates = await list_candidates(stub, conn, status="Pending")
    hit = next(c for c in candidates if c.trigger_iri.endswith("min-age-violation"))

    await reject_candidate(stub, conn, hit)

    # As above: a scratch-graph push does appear from auto-log's unrelated
    # execution in the same evaluate_triggers() pass -- what matters here
    # is that rejection specifically never touches this candidate's own
    # target graph.
    assert not any(g == hit.target_graph for g, _ in stub.pushed)
    refreshed = await get_candidate(stub, conn, hit.iri)
    assert refreshed.status == "Rejected"


@pytest.mark.asyncio
async def test_approving_a_non_pending_candidate_is_refused(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    candidates = await list_candidates(stub, conn, status="Pending")
    hit = next(c for c in candidates if c.trigger_iri.endswith("min-age-violation"))
    await approve_candidate(stub, conn, hit)

    stale = await get_candidate(stub, conn, hit.iri)  # now Approved
    with pytest.raises(CandidateError):
        await approve_candidate(stub, conn, stale)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
