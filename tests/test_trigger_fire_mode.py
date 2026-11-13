"""``hev:fireMode``: ``Edge`` (default) and ``EveryEvaluation``.

Built on ``LedgerStub`` from ``test_trigger_rearm.py``, which keeps a small
in-memory firing ledger, so these tests can see firings accumulate or not.
"""

from __future__ import annotations

import pytest

from holonbridge.conn import Conn
from holonbridge.trigger_registry import InvalidTrigger, render_trigger_turtle
from holonbridge.triggers import (
    TRIGGER_STATE,
    evaluate_triggers,
    list_candidates,
    load_named_triggers,
    reject_candidate,
)

from .test_trigger_rearm import ISLA, MICHAELA, LedgerStub, only
from .test_triggers import HEV, TRIGGER_TYPE, _row

AUTO_EVERY = "urn:ds:named-trigger:auto-every"
REVIEW_EVERY = "urn:ds:named-trigger:review-every"


@pytest.fixture
def stub() -> LedgerStub:
    stub = LedgerStub()

    def add(iri: str, **props: str) -> None:
        for key, value in props.items():
            stub.trigger_rows.append(_row(t=iri, type=TRIGGER_TYPE, p=f"{HEV}{key}", o=value))

    # Same condition and rule as the fixture's auto-log trigger, but every evaluation.
    add(
        AUTO_EVERY, id="auto-every", triggerKind="StateTrigger", condition="select-something",
        onFire="log-rule", reviewRequired="false", triggerStatus="Active", fireMode="EveryEvaluation",
    )
    add(
        REVIEW_EVERY, id="review-every", triggerKind="StateTrigger", condition="select-underage-varsity",
        onFire="flag-violation", reviewRequired="true", triggerStatus="Active", fireMode="EveryEvaluation",
    )
    return stub


@pytest.fixture
def conn() -> Conn:
    return Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")


async def test_edge_stays_the_default_and_fires_once(stub, conn):
    loaded = await load_named_triggers(stub, conn)
    assert loaded.by_id("auto-log").fire_mode == "Edge"

    runs = []
    for _ in range(3):
        runs.append(only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "auto-log"))

    assert [len(r) for r in runs] == [1, 0, 0]


async def test_every_evaluation_fires_on_every_evaluation(stub, conn):
    loaded = await load_named_triggers(stub, conn)
    assert loaded.by_id("auto-every").every_evaluation

    runs = []
    for _ in range(3):
        runs.append(only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "auto-every"))

    assert [[f.outcome for f in r] for r in runs] == [["executed"]] * 3


async def test_every_evaluation_keeps_one_live_firing_per_focus(stub, conn):
    for _ in range(4):
        await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)

    assert len(stub.live(AUTO_EVERY, MICHAELA)) == 1
    # The earlier three are history, stamped cleared, not deleted.
    assert len([e for e in stub.ledger if e["trigger"] == AUTO_EVERY]) == 4


async def test_every_evaluation_follows_the_condition_when_a_focus_stops_matching(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    stub.focus_rows["something"] = []

    later = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "auto-every")

    assert later == []
    assert stub.live(AUTO_EVERY) == []


async def test_every_evaluation_with_review_does_not_stack_candidates(stub, conn):
    for _ in range(3):
        await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)

    review = [c for c in await list_candidates(stub, conn) if c.trigger_iri == REVIEW_EVERY]
    assert len(review) == 1  # the pending-candidate guard still holds

    await reject_candidate(stub, conn, review[0])
    again = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "review-every")
    assert [f.outcome for f in again] == ["proposed"]


async def test_an_unknown_fire_mode_is_skipped_with_a_warning(stub, conn):
    for key, value in (("id", "odd"), ("triggerKind", "StateTrigger"), ("condition", "select-something"),
                       ("onFire", "log-rule"), ("fireMode", "Sometimes")):
        stub.trigger_rows.append(
            _row(t="urn:ds:named-trigger:odd", type=TRIGGER_TYPE, p=f"{HEV}{key}", o=value)
        )
    loaded = await load_named_triggers(stub, conn)

    assert loaded.by_id("odd") is None
    assert any("unrecognised fireMode" in w for w in loaded.warnings)


async def test_an_agent_trigger_cannot_be_every_evaluation(stub, conn):
    for key, value in (("id", "agent-every"), ("triggerKind", "StateTrigger"), ("condition", "select-something"),
                       ("onFireAgent", "ep"), ("agentTask", "look"), ("fireMode", "EveryEvaluation")):
        stub.trigger_rows.append(
            _row(t="urn:ds:named-trigger:agent-every", type=TRIGGER_TYPE, p=f"{HEV}{key}", o=value)
        )
    loaded = await load_named_triggers(stub, conn)

    assert loaded.by_id("agent-every") is None
    assert any("onFireAgent" in w for w in loaded.warnings)


def _render(**over):
    args = dict(
        node="urn:ds:named-trigger:t", trigger_id="t", trigger_kind="StateTrigger", condition="c",
        on_fire="r", review_required=False, status="Active", label=None, description=None,
        watched_predicates=[], registered_at="2026-11-13T00:00:00",
    )
    args.update(over)
    return render_trigger_turtle(**args)


def test_registration_writes_fire_mode_only_when_it_is_not_the_default():
    assert "fireMode" not in _render()
    assert 'fireMode> "EveryEvaluation"' in _render(fire_mode="EveryEvaluation")


def test_registration_refuses_an_unknown_mode_and_an_agent_in_every_mode():
    with pytest.raises(InvalidTrigger):
        _render(fire_mode="Sometimes")
    with pytest.raises(InvalidTrigger):
        _render(on_fire=None, agent_endpoint="ep", agent_task="look", fire_mode="EveryEvaluation")
