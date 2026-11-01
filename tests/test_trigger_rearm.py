"""Trigger re-arming: the falling edge of the edge-triggered contract.

``test_triggers.py``'s ``TriggerStub`` models candidates but treats the firing
ledger as a canned ``already_fired`` set, which is enough for "a fired focus is
skipped" but cannot show a ledger *changing*. ``LedgerStub`` below extends it
with a small in-memory ledger: it parses the ``INSERT DATA`` that records a
firing and the one that stamps ``hev:clearedAt``, and answers the ledger's
``ASK``/``SELECT`` forms from that state, honouring the same "live = not
cleared" rule the real queries state.
"""

from __future__ import annotations

import re

import pytest

from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiError
from holonbridge.triggers import (
    TRIGGER_STATE,
    evaluate_triggers,
    list_candidates,
    reject_candidate,
)

from .test_triggers import TriggerStub

MIN_AGE = "urn:ds:named-trigger:min-age-violation"
AUTO_LOG = "urn:ds:named-trigger:auto-log"
ISLA = "urn:ds:person:isla"
MICHAELA = "urn:ds:person:michaela"

_FIRING = re.compile(
    r"<([^>]+)> a hev:TriggerFiring ; hev:firedTrigger <([^>]+)> ; hev:firedFor <([^>]+)>"
)
_CLEARED = re.compile(r"<([^>]+)> hev:clearedAt")
_ASK_FIRED = re.compile(r"hev:firedTrigger <([^>]+)> ; hev:firedFor <([^>]+)>")
_SELECT_LIVE = re.compile(r"hev:firedTrigger <([^>]+)> ; hev:firedFor \?focus")
_ASK_PENDING = re.compile(r"hev:proposedByTrigger <([^>]+)> ; hev:proposedFor <([^>]+)>")


class LedgerStub(TriggerStub):
    def __init__(self) -> None:
        super().__init__()
        self.ledger: list[dict] = []
        self.fail_conditions_containing: str | None = None

    def live(self, trigger: str, focus: str | None = None) -> list[dict]:
        return [
            entry
            for entry in self.ledger
            if entry["trigger"] == trigger
            and not entry["cleared"]
            and (focus is None or entry["focus"] == focus)
        ]

    async def select(self, conn, query, *, default_graph=None):
        flat = " ".join(query.split())

        if self.fail_conditions_containing and self.fail_conditions_containing in flat:
            raise FusekiError(500, "condition blew up")

        if flat.startswith("PREFIX hev:") and "ASK {" in flat and "firedTrigger" in flat:
            trigger, focus = _ASK_FIRED.search(flat).groups()
            return {"boolean": bool(self.live(trigger, focus))}

        if "SELECT ?firing ?focus" in flat:
            trigger = _SELECT_LIVE.search(flat).group(1)
            return {
                "results": {
                    "bindings": [
                        {"firing": {"value": e["iri"]}, "focus": {"value": e["focus"]}}
                        for e in self.live(trigger)
                    ]
                }
            }

        if "ASK {" in flat and "proposedByTrigger" in flat:
            trigger, focus = _ASK_PENDING.search(flat).groups()
            return {
                "boolean": any(
                    c.get("proposedByTrigger") == trigger
                    and c.get("proposedFor") == focus
                    and c.get("candidateStatusValue") == "Pending"
                    for c in self._candidates.values()
                )
            }

        return await super().select(conn, query, default_graph=default_graph)

    async def update(self, conn, update):
        await super().update(conn, update)
        flat = " ".join(update.split())

        recorded = _FIRING.search(flat)
        if recorded:
            iri, trigger, focus = recorded.groups()
            self.ledger.append(
                {"iri": iri, "trigger": trigger, "focus": focus, "cleared": False}
            )

        if "hev:clearedAt" in flat and flat.lstrip().startswith("PREFIX"):
            cleared = set(_CLEARED.findall(flat))
            for entry in self.ledger:
                if entry["iri"] in cleared:
                    entry["cleared"] = True


@pytest.fixture
def stub() -> LedgerStub:
    return LedgerStub()


@pytest.fixture
def conn() -> Conn:
    return Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")


def only(firings, trigger_id: str):
    return [f for f in firings if f.trigger_id == trigger_id]


async def test_a_matching_focus_fires_once_while_its_condition_holds(stub, conn):
    first = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "min-age-violation")
    second = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "min-age-violation")
    assert [f.outcome for f in first] == ["proposed"]
    assert second == []
    assert len(stub.live(MIN_AGE, ISLA)) == 1


async def test_a_focus_that_stops_matching_is_cleared_not_deleted(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    stub.focus_rows["underage"] = []

    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)

    assert stub.live(MIN_AGE) == []
    # History is kept: the firing is still in the ledger, stamped as cleared.
    assert [e for e in stub.ledger if e["trigger"] == MIN_AGE and e["cleared"]]
    assert any("hev:clearedAt" in u and "INSERT DATA" in u for u in stub.updates)


async def test_a_cleared_focus_fires_again_when_it_matches_again(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    stub.focus_rows["underage"] = []
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)

    # The first proposal gets resolved before the condition comes back, so the
    # pending-candidate guard has nothing to hold the new edge back for.
    (first,) = await list_candidates(stub, conn, status="Pending")
    await reject_candidate(stub, conn, first)

    stub.focus_rows["underage"] = [ISLA]
    again = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "min-age-violation")

    assert [f.outcome for f in again] == ["proposed"]
    assert len(stub.live(MIN_AGE, ISLA)) == 1
    assert len(await list_candidates(stub, conn)) == 2


async def test_rearming_does_not_stack_a_second_candidate_on_a_pending_one(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    stub.focus_rows["underage"] = []
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)

    stub.focus_rows["underage"] = [ISLA]
    again = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "min-age-violation")

    assert [f.outcome for f in again] == ["skipped-pending-candidate"]
    assert len(await list_candidates(stub, conn)) == 1
    # No new live firing was recorded for the skipped focus either, so it is
    # picked up once the earlier candidate is resolved.
    assert stub.live(MIN_AGE, ISLA) == []


async def test_a_focus_that_is_still_true_is_never_cleared(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    stub.focus_rows["underage"] = [ISLA, "urn:ds:person:noor"]

    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)

    assert len(stub.live(MIN_AGE, ISLA)) == 1
    assert len(stub.live(MIN_AGE, "urn:ds:person:noor")) == 1
    assert not any(e["cleared"] for e in stub.ledger if e["trigger"] == MIN_AGE)


async def test_a_failing_condition_clears_nothing(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    assert stub.live(MIN_AGE, ISLA)

    stub.fail_conditions_containing = '"underage"'
    firings = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "min-age-violation")

    assert [f.outcome for f in firings] == ["failed"]
    assert len(stub.live(MIN_AGE, ISLA)) == 1


async def test_auto_executing_triggers_rearm_too(stub, conn):
    first = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "auto-log")
    assert [f.outcome for f in first] == ["executed"]

    stub.focus_rows["something"] = []
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)
    assert stub.live(AUTO_LOG) == []

    stub.focus_rows["something"] = [MICHAELA]
    again = only(await evaluate_triggers(stub, conn, kind=TRIGGER_STATE), "auto-log")
    assert [f.outcome for f in again] == ["executed"]


async def test_the_candidate_points_at_the_firing_that_was_recorded(stub, conn):
    await evaluate_triggers(stub, conn, kind=TRIGGER_STATE)

    (candidate,) = await list_candidates(stub, conn)
    (entry,) = stub.live(MIN_AGE, ISLA)
    assert candidate.firing_iri == entry["iri"]
