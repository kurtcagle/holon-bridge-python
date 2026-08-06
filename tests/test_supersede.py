"""Tests for the Supersede write mode.

Verification status, same as in the implementation: these confirm the
Python logic against a stub that models the SPARQL results _apply_supersede
expects. They do not, and cannot, confirm that Jena actually parses the
``<<( s p o )>>`` annotation syntax the implementation emits -- that needs a
live run against a real store, which has not happened yet as of this
writing.

Tested against ``_apply_supersede`` directly rather than through
``execute_named_rule``: the full rule pipeline first runs the rule's own
CONSTRUCT and round-trips the result through Turtle via ``post_graph``,
neither of which a plain dict-based stub can do without embedding a real
SPARQL engine. ``_apply_supersede`` is the actual unit of new logic here --
everything upstream of it (CONSTRUCT execution, scratch population) is
unchanged, already-tested machinery shared with Append/Replace/Sync.

Deliberately not attempted here: a genuine concurrent-contention test. This
stub applies writes immediately with no real race to simulate faithfully.
The retry loop is implemented on the same shape as sequence.mint's proven
compare-and-set, but that loop's *retry* behaviour specifically is
unverified by this file -- only the single-attempt logic is.
"""

from __future__ import annotations

import pytest

from holonbridge.conn import Conn
from holonbridge.named_rules import RuleError, _apply_supersede

TARGET = "urn:ds:target"
SCRATCH = "urn:ds:rule-scratch:test"


def _uri(value: str) -> dict:
    return {"type": "uri", "value": value}


def _lit(value: str) -> dict:
    return {"type": "literal", "value": value}


class SupersedeStub:
    """Answers exactly the query shapes _apply_supersede issues.

    State is two flat triple lists (scratch, target) plus a set tracking
    which (s, p, o) triples in target have been marked superseded -- a
    stand-in for the real RDF 1.2 annotation, since this stub does not
    parse or evaluate triple-term syntax at all.
    """

    def __init__(self, scratch_triples, target_triples) -> None:
        self.scratch = list(scratch_triples)
        self.target = list(target_triples)
        self.superseded: set[tuple[str, str, str]] = set()
        self.calls: list[str] = []
        self.fail_next_update = False

    def _live_target(self):
        return [
            t for t in self.target
            if (t[0]["value"], t[1]["value"], t[2]["value"]) not in self.superseded
        ]

    async def select(self, conn, query: str) -> dict:  # noqa: ANN001
        self.calls.append(query)

        if "SELECT DISTINCT ?s ?p" in query:
            seen: list[tuple[str, str]] = []
            bindings = []
            for s, p, _ in self.scratch:
                key = (s["value"], p["value"])
                if key not in seen:
                    seen.append(key)
                    bindings.append({"s": s, "p": p})
            return {"results": {"bindings": bindings}}

        if f"<{SCRATCH}>" in query:
            objs = []
            seen_vals: list[str] = []
            for _, _, o in self.scratch:
                if o["value"] not in seen_vals:
                    seen_vals.append(o["value"])
                    objs.append(o)
            return {"results": {"bindings": [{"o": o} for o in objs]}}

        if f"<{TARGET}>" in query:
            return {"results": {"bindings": [{"o": t[2]} for t in self._live_target()]}}

        raise AssertionError(f"unrecognised query shape:\n{query}")

    async def update(self, conn, query: str) -> None:  # noqa: ANN001
        self.calls.append(query)
        assert "INSERT" in query
        if self.fail_next_update:
            self.fail_next_update = False
            return  # guard silently matched nothing -- simulates a lost race

        # Apply the write the same way the real guarded UPDATE would: mark
        # any currently-live old value for each (s, p) touched as
        # superseded, then add the new one to target.
        touched = {(s["value"], p["value"]) for s, p, _ in self.scratch}
        for s_val, p_val in touched:
            for t in self._live_target():
                if t[0]["value"] == s_val and t[1]["value"] == p_val:
                    self.superseded.add((t[0]["value"], t[1]["value"], t[2]["value"]))
            new_o = next(
                o for s, p, o in self.scratch if s["value"] == s_val and p["value"] == p_val
            )
            self.target.append((_uri(s_val), _uri(p_val), new_o))


def make_conn() -> Conn:
    return Conn(base_url="http://localhost:3030", dataset="ds", overridden=False, bank_name="local")


async def test_a_brand_new_pair_is_inserted_with_nothing_to_supersede():
    stub = SupersedeStub(
        scratch_triples=[(_uri("urn:a"), _uri("urn:p"), _lit("NEW"))],
        target_triples=[],
    )
    added, removed = await _apply_supersede(stub, make_conn(), TARGET, SCRATCH)
    assert added == 1
    assert removed == 0
    assert not stub.superseded  # nothing existed, so nothing was marked


async def test_superseding_an_existing_value_does_not_delete_it():
    """The defining property of Supersede: the old value stays in the graph."""
    stub = SupersedeStub(
        scratch_triples=[(_uri("urn:a"), _uri("urn:p"), _lit("NEW"))],
        target_triples=[(_uri("urn:a"), _uri("urn:p"), _lit("OLD"))],
    )
    added, removed = await _apply_supersede(stub, make_conn(), TARGET, SCRATCH)

    assert added == 1
    assert removed == 0  # Supersede never deletes -- removed is always 0
    values_in_target = [t[2]["value"] for t in stub.target]
    assert "OLD" in values_in_target, "the superseded value must not be deleted"
    assert "NEW" in values_in_target
    assert ("urn:a", "urn:p", "OLD") in stub.superseded


async def test_a_value_already_current_is_a_no_op():
    stub = SupersedeStub(
        scratch_triples=[(_uri("urn:a"), _uri("urn:p"), _lit("SAME"))],
        target_triples=[(_uri("urn:a"), _uri("urn:p"), _lit("SAME"))],
    )
    added, removed = await _apply_supersede(stub, make_conn(), TARGET, SCRATCH)
    assert added == 0
    assert removed == 0
    assert not stub.superseded
    assert not any("INSERT" in q for q in stub.calls)


async def test_an_ambiguous_multi_valued_construct_is_refused():
    """Supersede's default identity key is (subject, predicate). A rule whose
    CONSTRUCT derives more than one value for the same pair is refused rather
    than guessed at -- there is no generic way to know which one replaces
    which.
    """
    stub = SupersedeStub(
        scratch_triples=[
            (_uri("urn:a"), _uri("urn:p"), _lit("ONE")),
            (_uri("urn:a"), _uri("urn:p"), _lit("TWO")),
        ],
        target_triples=[],
    )
    with pytest.raises(RuleError, match="distinct values"):
        await _apply_supersede(stub, make_conn(), TARGET, SCRATCH)


async def test_a_lost_race_is_retried():
    """The compare-and-set loop retries when the guarded write matches nothing
    -- mirroring sequence.mint exactly. One simulated loss, then success.
    """
    stub = SupersedeStub(
        scratch_triples=[(_uri("urn:a"), _uri("urn:p"), _lit("NEW"))],
        target_triples=[(_uri("urn:a"), _uri("urn:p"), _lit("OLD"))],
    )
    stub.fail_next_update = True
    added, removed = await _apply_supersede(stub, make_conn(), TARGET, SCRATCH)
    assert added == 1
    assert ("urn:a", "urn:p", "OLD") in stub.superseded
