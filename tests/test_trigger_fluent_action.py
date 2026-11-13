"""``hev:onFireFluentQuery``: a trigger whose action is a set of fluent updates.

Same technique as ``test_trigger_rearm.py``: ``LedgerStub`` models the firing
ledger in memory, and this module adds a trigger and a pair of named queries to
it. ``update_fluent`` is replaced by a recorder (its own behaviour is covered by
``test_fluent_*``), so these tests are about what the trigger asks it to do, who
it does it as, and what is recorded when it goes wrong.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from holonbridge import fluent_trigger_action as action_mod
from holonbridge import triggers as triggers_mod
from holonbridge.conn import Conn
from holonbridge.fluent import FluentError, Operation
from holonbridge.fluent_trigger_action import (
    MAX_ROWS,
    FluentActionInvalid,
    authorize_fluent_trigger,
    bind_action,
    parse_rows,
)
from holonbridge.named_queries import NamedQuery
from holonbridge.trigger_authz import TriggerDenied
from holonbridge.triggers import TRIGGER_STATE, evaluate_triggers, load_named_triggers

from .test_trigger_rearm import LedgerStub, only
from .test_triggers import HB, HEV, _row

TICK = "urn:ds:named-trigger:tick"
ISLA = "urn:ds:person:isla"
DEFINER = "urn:ds:person:definer"
CLOCK = "urn:ds:fluent:clock"
GATE = "urn:ds:fluent:gate"
XSD = "http://www.w3.org/2001/XMLSchema#"


def fluent_row(**cells) -> dict:
    out: dict = {}
    for key, value in cells.items():
        out[key] = value if isinstance(value, dict) else {"type": "literal", "value": str(value)}
    return out


def iri(value: str) -> dict:
    return {"type": "uri", "value": value}


def typed(value, datatype: str) -> dict:
    return {"type": "literal", "value": str(value), "datatype": f"{XSD}{datatype}"}


class FluentStub(LedgerStub):
    """LedgerStub plus a fluent trigger, its action query and canned rows."""

    def __init__(self) -> None:
        super().__init__()
        self.action_rows: list[dict] = [
            fluent_row(fluent=iri(CLOCK), value=typed(5, "integer")),
        ]
        self.action_runs = 0
        self.add_trigger(
            "tick",
            condition="select-something",
            onFireFluentQuery="advance-clock",
            reviewRequired="false",
            armedBy=DEFINER,
        )
        self.query_rows.append(
            _row(q="urn:ds:named-query:advance-clock", type=f"{HB}NamedQuery", p=f"{HB}id", o="advance-clock")
        )
        self.query_rows.append(
            _row(
                q="urn:ds:named-query:advance-clock",
                type=f"{HB}NamedQuery",
                p=f"{HB}sparql",
                o='SELECT ?fluent ?value WHERE { BIND(<urn:ds:fluent:clock> AS ?fluent) BIND(5 AS ?value) } # "fluent-rows"',
            )
        )
        # Only the tick trigger and the condition it needs: drop the others so
        # their (unrelated) firings do not clutter the assertions.
        self.trigger_rows = [r for r in self.trigger_rows if r["t"]["value"] == TICK]

    def add_trigger(self, ident: str, **props: str) -> None:
        iri_ = f"urn:ds:named-trigger:{ident}"
        base = {"id": ident, "triggerKind": "StateTrigger", "triggerStatus": "Active", **props}
        for key, value in base.items():
            self.trigger_rows.append(_row(t=iri_, type=f"{HEV}NamedTrigger", p=f"{HEV}{key}", o=value))

    async def select(self, conn, query, *, default_graph=None):
        if '"fluent-rows"' in query:
            self.action_runs += 1
            return {"results": {"bindings": list(self.action_rows)}}
        return await super().select(conn, query, default_graph=default_graph)


class Recorder:
    """Stands in for ``update_fluent``: records each call, can fail on one."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_on: int | None = None  # 1-based call number

    async def __call__(self, client, conn, **kwargs):
        self.calls.append(kwargs)
        if self.fail_on == len(self.calls):
            raise FluentError("expected value did not match")


@pytest.fixture
def stub() -> FluentStub:
    return FluentStub()


@pytest.fixture
def conn() -> Conn:
    return Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(action_mod, "update_fluent", rec)
    monkeypatch.setattr(triggers_mod, "_recheck_cache", {})
    return rec


def query(sparql: str, ident: str = "q") -> NamedQuery:
    return NamedQuery(id=ident, iri=f"urn:ds:named-query:{ident}", sparql=sparql, vocabulary="hb")


# --- parse_rows ----------------------------------------------------------------


def test_parse_rows_reads_the_columns():
    (row,) = parse_rows(
        [
            fluent_row(
                fluent=iri(CLOCK),
                value=typed(7, "integer"),
                operation="Insert",
                assertedAt="2026-11-13T09:00:00",
                description="tick",
            )
        ]
    )
    assert row.fluent == CLOCK
    assert row.operation is Operation.INSERT
    assert row.value == 7 and not row.is_iri
    assert row.asserted_at == datetime(2026, 11, 13, 9, tzinfo=timezone.utc)
    assert row.description == "tick"


def test_parse_rows_defaults_to_set_and_types_the_value():
    rows = parse_rows(
        [
            fluent_row(fluent=iri(CLOCK), value=typed("1.5", "decimal")),
            fluent_row(fluent=iri(GATE), value=iri("urn:ds:state:open")),
            fluent_row(fluent=iri(GATE), value="urn:ds:state:open", isIri="true"),
            fluent_row(fluent=iri(GATE), value=typed("true", "boolean")),
        ]
    )
    assert [r.operation for r in rows] == [Operation.SET] * 4
    assert rows[0].value == Decimal("1.5")
    assert (rows[1].value, rows[1].is_iri) == ("urn:ds:state:open", True)
    assert (rows[2].value, rows[2].is_iri) == ("urn:ds:state:open", True)
    assert rows[3].value is True


def test_parse_rows_empty_result_is_fine():
    assert parse_rows([]) == []


@pytest.mark.parametrize(
    "binding, message",
    [
        (fluent_row(value=typed(1, "integer")), "no ?fluent"),
        (fluent_row(fluent=iri(CLOCK)), "no ?value"),
        (fluent_row(fluent=iri(CLOCK), value="x", operation="Multiply"), "?operation"),
        (fluent_row(fluent=iri(CLOCK), value=typed("abc", "integer")), "not a valid integer"),
        (fluent_row(fluent=iri(CLOCK), value="x", assertedAt="yesterday"), "?assertedAt"),
        (fluent_row(fluent=iri("urn:bad iri"), value="x"), "?fluent"),
        (fluent_row(fluent=iri(CLOCK), value=iri("urn:bad>iri")), "?value"),
    ],
)
def test_parse_rows_names_the_row_and_the_problem(binding, message):
    good = fluent_row(fluent=iri(CLOCK), value=typed(1, "integer"))
    with pytest.raises(FluentActionInvalid) as caught:
        parse_rows([good, binding])
    assert str(caught.value).startswith("row 2:")
    assert message in str(caught.value)


def test_parse_rows_caps_the_firing():
    rows = [fluent_row(fluent=iri(CLOCK), value=typed(1, "integer"))] * (MAX_ROWS + 1)
    with pytest.raises(FluentActionInvalid, match="at most"):
        parse_rows(rows)


# --- bind_action ---------------------------------------------------------------


def test_bind_action_requires_a_select():
    with pytest.raises(FluentActionInvalid, match="must be a SELECT"):
        bind_action(query("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"), None)


def test_bind_action_accepts_a_select_without_a_focus_parameter():
    sparql = bind_action(query("SELECT ?fluent ?value WHERE { ?fluent <urn:p> ?value }"), ISLA)
    assert sparql.startswith("SELECT")


# --- authorisation -------------------------------------------------------------


async def test_a_reviewed_fluent_trigger_is_refused():
    cond = query("SELECT ?focus WHERE { ?focus a <urn:T> }", "cond")
    act = query("SELECT ?fluent ?value WHERE { ?fluent <urn:p> ?value }", "act")
    with pytest.raises(TriggerDenied) as caught:
        await authorize_fluent_trigger(
            None, Conn("http://x", "ds", False, "local"), DEFINER,
            condition=cond, action_query=act, review_required=True,
        )
    assert caught.value.status == 400
    assert caught.value.detail["error"] == "fluent_action_cannot_be_reviewed"


async def test_the_definer_needs_read_on_both_queries_and_write_on_three_graphs(monkeypatch):
    reads: list[str] = []
    grants: list[str] = []

    async def fake_reads(query_fn, conn, person, *, label, ident, sparql):
        reads.append(label)

    async def fake_grant(query_fn, conn, person, graph, **kw):
        grants.append(graph)

    monkeypatch.setattr(action_mod, "authorize_reads", fake_reads)
    monkeypatch.setattr(action_mod, "require_grant", fake_grant)

    conn = Conn("http://x", "ds", False, "local")
    cond = query("SELECT ?focus WHERE { ?focus a <urn:T> }", "cond")
    act = query("SELECT ?fluent ?value WHERE { ?fluent <urn:p> ?value }", "act")
    warnings = await authorize_fluent_trigger(
        None, conn, DEFINER, condition=cond, action_query=act, review_required=False
    )

    assert reads == ["condition", "action_query"]
    assert grants == [conn.graph("events"), conn.scene_graph, conn.graph("sequences")]
    # No 'focus' parameter declared: said once, as a warning, not an error.
    assert any("declares no 'focus' parameter" in w for w in warnings)


async def test_an_action_query_that_never_projects_fluent_is_warned_about(monkeypatch):
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(action_mod, "authorize_reads", noop)
    monkeypatch.setattr(action_mod, "require_grant", noop)
    cond = query("SELECT ?focus WHERE { ?focus a <urn:T> }", "cond")
    act = query("SELECT ?x ?value WHERE { ?x <urn:p> ?value }", "act")
    warnings = await authorize_fluent_trigger(
        None, Conn("http://x", "ds", False, "local"), DEFINER,
        condition=cond, action_query=act, review_required=False,
    )
    assert any("never mentions ?fluent" in w for w in warnings)


# --- loading -------------------------------------------------------------------


async def test_the_loader_reads_a_fluent_trigger(stub, conn):
    result = await load_named_triggers(stub, conn)
    trig = result.by_id("tick")
    assert trig.is_fluent and not trig.is_agent
    assert trig.fluent_query == "advance-clock"
    assert trig.armed_by == DEFINER
    assert trig.summary()["onFireFluentQuery"] == "advance-clock"


async def test_a_fluent_trigger_that_is_not_explicitly_unreviewed_is_skipped(stub, conn):
    stub.trigger_rows = [r for r in stub.trigger_rows if r["p"]["value"] != f"{HEV}reviewRequired"]
    result = await load_named_triggers(stub, conn)
    assert result.by_id("tick") is None
    assert any("reviewRequired" in w for w in result.warnings)


async def test_a_trigger_with_two_actions_is_skipped(stub, conn):
    stub.add_trigger("both", condition="select-something", onFire="log-rule",
                     onFireFluentQuery="advance-clock", reviewRequired="false")
    result = await load_named_triggers(stub, conn)
    assert result.by_id("both") is None


# --- firing --------------------------------------------------------------------


async def test_a_matching_focus_applies_the_rows_as_the_definer(stub, conn, recorder):
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")

    assert [f.outcome for f in firings] == ["applied"]
    assert "1 of 1" in firings[0].detail
    (call,) = recorder.calls
    assert call["fluent"] == CLOCK and call["value"] == 5
    assert call["operation"] is Operation.SET
    assert call["asserted_by"] == DEFINER
    assert call["description"] == "trigger tick"
    assert len(stub.live(TICK)) == 1


async def test_an_edge_trigger_applies_once_while_the_condition_holds(stub, conn, recorder):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    again = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")

    assert again == []
    assert len(recorder.calls) == 1
    assert stub.action_runs == 1


async def test_rows_are_applied_in_order_and_carry_their_own_columns(stub, conn, recorder):
    stub.action_rows = [
        fluent_row(fluent=iri(CLOCK), value=typed(1, "integer"), operation="Insert", description="step"),
        fluent_row(fluent=iri(GATE), value=iri("urn:ds:state:open"), assertedAt="2026-11-13T09:00:00Z"),
    ]
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)

    first, second = recorder.calls
    assert (first["fluent"], first["operation"], first["description"]) == (CLOCK, Operation.INSERT, "step")
    assert second["fluent"] == GATE and second["is_iri"] is True
    assert second["asserted_datetime"] == datetime(2026, 11, 13, 9, tzinfo=timezone.utc)


async def test_a_bad_row_fails_the_firing_before_anything_is_applied(stub, conn, recorder):
    stub.action_rows = [
        fluent_row(fluent=iri(CLOCK), value=typed(1, "integer")),
        fluent_row(fluent=iri(GATE)),  # no ?value
    ]
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")

    assert [f.outcome for f in firings] == ["failed"]
    assert "row 2" in firings[0].detail
    assert recorder.calls == []
    # Not recorded, so it is tried again once the query is fixed.
    assert stub.live(TICK) == []


async def test_a_first_row_failure_is_a_failed_firing_and_is_retried(stub, conn, recorder):
    recorder.fail_on = 1
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")
    assert [f.outcome for f in firings] == ["failed"]
    assert stub.live(TICK) == []

    recorder.fail_on = None
    retry = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")
    assert [f.outcome for f in retry] == ["applied"]


async def test_a_failure_part_way_is_recorded_as_partial_and_not_retried(stub, conn, recorder):
    stub.action_rows = [
        fluent_row(fluent=iri(CLOCK), value=typed(1, "integer"), operation="Insert"),
        fluent_row(fluent=iri(GATE), value=typed(1, "integer"), operation="Insert"),
        fluent_row(fluent=iri(GATE), value=typed(1, "integer"), operation="Insert"),
    ]
    recorder.fail_on = 2
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")

    assert [f.outcome for f in firings] == ["partial"]
    assert "1 of 3" in firings[0].detail and "row 2 of 3" in firings[0].detail
    assert len(recorder.calls) == 2  # the third row was never attempted
    assert len(stub.live(TICK)) == 1

    again = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")
    assert again == []
    assert len(recorder.calls) == 2  # an Insert is not applied twice


async def test_an_empty_result_records_a_firing_that_did_nothing(stub, conn, recorder):
    stub.action_rows = []
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")
    assert [f.outcome for f in firings] == ["applied"]
    assert "0 of 0" in firings[0].detail
    assert recorder.calls == []


async def test_an_unknown_action_query_is_a_failure_not_a_crash(stub, conn, recorder):
    stub.query_rows = [r for r in stub.query_rows if "advance-clock" not in str(r)]
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")
    assert [f.outcome for f in firings] == ["failed"]
    assert "advance-clock" in firings[0].detail
    assert recorder.calls == []


async def test_a_definer_who_has_lost_access_applies_nothing(stub, conn, recorder, monkeypatch):
    async def denied(*args, **kwargs):
        return "definer <urn:ds:person:definer> may no longer do this: no write grant"

    monkeypatch.setattr(triggers_mod, "_definer_denied", denied)
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")

    assert [f.outcome for f in firings] == ["skipped-definer-unauthorised"]
    assert recorder.calls == [] and stub.action_runs == 0
    assert stub.live(TICK) == []


async def test_a_trigger_with_no_recorded_definer_cannot_write_fluents(stub, conn, recorder):
    stub.trigger_rows = [r for r in stub.trigger_rows if r["p"]["value"] != f"{HEV}armedBy"]
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")
    assert [f.outcome for f in firings] == ["failed"]
    assert "no recorded definer" in firings[0].detail
    assert recorder.calls == []


async def test_a_focus_that_stops_matching_is_rearmed(stub, conn, recorder):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    stub.focus_rows["something"] = []
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    assert stub.live(TICK) == []

    stub.focus_rows["something"] = ["urn:ds:person:michaela"]
    again = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")
    assert [f.outcome for f in again] == ["applied"]
    assert len(recorder.calls) == 2


async def test_every_evaluation_is_refused_for_a_fluent_trigger(stub, conn):
    stub.add_trigger("again", condition="select-something", onFireFluentQuery="advance-clock",
                     reviewRequired="false", fireMode="EveryEvaluation")
    result = await load_named_triggers(stub, conn)
    assert result.by_id("again") is None
    assert any("EveryEvaluation" in w and "onFireFluentQuery" in w for w in result.warnings)


def test_the_renderer_refuses_every_evaluation_for_a_fluent_trigger():
    from holonbridge.trigger_registry import InvalidTrigger, render_trigger_turtle

    with pytest.raises(InvalidTrigger, match="EveryEvaluation"):
        render_trigger_turtle(
            node="urn:n", trigger_id="t", trigger_kind="StateTrigger", condition="c", on_fire=None,
            review_required=False, status="Active", label=None, description=None,
            watched_predicates=[], registered_at="2026-11-13T00:00:00+00:00",
            fluent_query="advance-clock", fire_mode="EveryEvaluation",
        )


async def test_a_row_for_a_portal_governed_fluent_fails_the_firing(stub, conn, recorder, monkeypatch):
    """A traveller's position changes only by crossing a portal, so a trigger may not
    set it either; nothing is applied."""
    from holonbridge import portal as portal_mod

    async def governed(client, conn_, fluent):
        return ["customs"] if fluent == CLOCK else []

    monkeypatch.setattr(portal_mod, "governing_portals", governed)

    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "tick")

    assert [f.outcome for f in firings] == ["failed"]
    assert "customs" in firings[0].detail
    assert recorder.calls == []
