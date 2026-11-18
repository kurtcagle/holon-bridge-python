"""A trigger's fluent action can deactivate a holon: lifecycle rows (``?holon``, ``?state``).

Real ``fire_fluent``, ``prepare_transition`` and ``update_fluents`` against rdflib's
SPARQL engine, with the world from ``test_lifecycle``. The action query is a SELECT that
BINDs its rows. Only the grant check is stubbed.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("pyshacl")

from holonbridge import acl as acl_mod
from holonbridge import fluent_transaction as tx_mod
from holonbridge import lifecycle as lc
from holonbridge.fluent import FluentError
from holonbridge.fluent_trigger_action import (
    FluentActionInvalid,
    LifecycleRow,
    fire_fluent,
    parse_rows,
)
from holonbridge.named_queries import NamedQuery
from holonbridge.params import Parameter

from .test_fluent_ledger_race import ALICE
from .test_lifecycle import (  # noqa: F401  (autouse fixture is re-exported for this module)
    OTHER,
    SCORE,
    TEAM,
    _no_triggers,
    _world,
    go,
    ledger_entries,
    score_of,
)

TRIG = SimpleNamespace(id="wind-down", armed_by=ALICE)


def query(*rows: str) -> NamedQuery:
    """A SELECT whose result is ``rows``; each row is the text of a ``VALUES`` tuple body."""
    focus = Parameter(name="focus", datatype="http://www.w3.org/2001/XMLSchema#anyURI")
    return NamedQuery(
        id="wind-down",
        iri="urn:q:wind-down",
        vocabulary="hb",
        params=[focus],
        sparql="\n".join(rows),
    )


def lifecycle_query(*pairs: tuple[str, str], reason: str | None = None) -> NamedQuery:
    blocks = []
    for holon, state in pairs:
        extra = f'BIND("{reason}" AS ?reason)' if reason else ""
        blocks.append(f'{{ BIND(<{holon}> AS ?holon) BIND("{state}" AS ?state) {extra} }}')
    return query("SELECT ?holon ?state ?reason WHERE { " + " UNION ".join(blocks) + " }")


@pytest.fixture
def grants(monkeypatch):
    asked: list[str] = []

    async def allowed(_query_fn, _holons, *, person, target):
        asked.append(target)
        return SimpleNamespace(allowed=True, matched_grant=None, reason="test")

    monkeypatch.setattr(acl_mod, "check_write", allowed)
    return asked


async def fire(store, conn, action):
    return await fire_fluent(store, conn, TRIG, TEAM, action_query=action)


# --- reading the rows ----------------------------------------------------------------


def test_a_lifecycle_row_is_read_when_allowed():
    cells = {
        "holon": {"type": "uri", "value": TEAM},
        "state": {"type": "literal", "value": "Inactive"},
        "reason": {"type": "literal", "value": "season over"},
    }
    (row,) = parse_rows([cells], allow_lifecycle=True)
    assert row == LifecycleRow(TEAM, "Inactive", None, "season over")


def test_the_state_may_be_given_as_the_hb_iri():
    cells = {
        "holon": {"type": "uri", "value": TEAM},
        "state": {"type": "uri", "value": lc.TOMBSTONED},
    }
    (row,) = parse_rows([cells], allow_lifecycle=True)
    assert row.state == "Tombstoned"


def test_a_lifecycle_row_is_refused_where_lifecycle_is_not_allowed():
    """Portal effect rows are parsed with the default, which has no ?holon."""
    cells = {
        "holon": {"type": "uri", "value": TEAM},
        "state": {"type": "literal", "value": "Inactive"},
    }
    with pytest.raises(FluentActionInvalid, match="no \\?fluent"):
        parse_rows([cells])


@pytest.mark.parametrize(
    ("cells", "message"),
    [
        (
            {"holon": {"type": "uri", "value": TEAM}},
            "row 1: a lifecycle row has \\?holon but no \\?state",
        ),
        (
            {
                "holon": {"type": "uri", "value": TEAM},
                "state": {"type": "literal", "value": "Closed"},
            },
            "row 1: \\?state 'Closed' is not one of",
        ),
    ],
)
def test_a_bad_lifecycle_row_names_the_row_and_the_problem(cells, message):
    with pytest.raises(FluentActionInvalid, match=message):
        parse_rows([cells], allow_lifecycle=True)


# --- firing ---------------------------------------------------------------------------


async def test_a_lifecycle_row_deactivates_the_holon_as_the_definer(grants):
    conn, store = _world()
    outcome = await fire(store, conn, lifecycle_query((TEAM, "Inactive"), reason="energy settled"))

    assert (outcome.applied, outcome.total, outcome.skipped) == (1, 1, 0)
    state = await lc.read_state(store, conn, TEAM)
    assert state.state == "Inactive"

    q = f"""SELECT ?d ?by WHERE {{ GRAPH <{conn.graph("events")}> {{
      <{state.assertion}> <https://w3id.org/holon/description> ?d ;
          <https://w3id.org/holon/event/assertedBy> ?by }} }}"""
    (row,) = list(store.ds.query(q))
    assert str(row.d) == "lifecycle: Inactive (energy settled) [trigger wind-down]"
    assert str(row.by) == ALICE


async def test_the_definer_needs_the_holons_graph_and_the_ontology_graph_the_first_time(grants):
    conn, store = _world()
    await fire(store, conn, lifecycle_query((TEAM, "Inactive")))
    assert set(grants) == {conn.holons_graph, conn.ontology_graph}
    grants.clear()
    await fire(store, conn, lifecycle_query((OTHER, "Inactive")))
    assert set(grants) == {conn.holons_graph}


async def test_a_definer_without_the_grant_deactivates_nothing(monkeypatch):
    conn, store = _world()

    async def denied(_q, _h, *, person, target):
        return SimpleNamespace(allowed=False, matched_grant=None, reason="no grant")

    monkeypatch.setattr(acl_mod, "check_write", denied)
    with pytest.raises(FluentActionInvalid, match="write access"):
        await fire(store, conn, lifecycle_query((TEAM, "Inactive")))
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"
    assert not await lc.vocabulary_declared(store, conn)


async def test_a_holon_already_in_the_state_is_skipped(grants):
    conn, store = _world()
    first = await go(store, conn, "Inactive")
    outcome = await fire(store, conn, lifecycle_query((TEAM, "Inactive")))
    assert (outcome.applied, outcome.total, outcome.skipped) == (0, 1, 1)
    assert ledger_entries(store, conn, first.fluent) == 1


async def test_a_trigger_can_reactivate_too(grants):
    conn, store = _world()
    await go(store, conn, "Tombstoned")
    await fire(store, conn, lifecycle_query((TEAM, "Active")))
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"


async def test_an_unknown_holon_fails_the_firing_and_names_the_row(grants):
    conn, store = _world()
    with pytest.raises(FluentActionInvalid, match="row 1: .*nobody.*not a holon"):
        await fire(store, conn, lifecycle_query(("urn:ex:nobody", "Inactive")))


async def test_two_rows_for_one_holon_are_refused(grants):
    conn, store = _world()
    with pytest.raises(FluentActionInvalid, match="more than one row"):
        await fire(store, conn, lifecycle_query((TEAM, "Inactive"), (TEAM, "Tombstoned")))
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"


async def test_two_holons_are_deactivated_in_one_transaction(grants):
    conn, store = _world()
    outcome = await fire(store, conn, lifecycle_query((TEAM, "Inactive"), (OTHER, "Tombstoned")))
    assert outcome.applied == 2
    assert (await lc.read_state(store, conn, TEAM)).state == "Inactive"
    assert (await lc.read_state(store, conn, OTHER)).state == "Tombstoned"


async def test_final_fluent_values_and_the_deactivation_land_together(grants):
    conn, store = _world()
    action = query(
        "SELECT ?fluent ?value ?operation ?holon ?state WHERE { "
        f'{{ BIND(<{SCORE}> AS ?fluent) BIND(7 AS ?value) BIND("Insert" AS ?operation) }} UNION '
        f'{{ BIND(<{TEAM}> AS ?holon) BIND("Inactive" AS ?state) }} }}'
    )
    outcome = await fire(store, conn, action)
    assert outcome.applied == 2
    assert score_of(store, conn) == Decimal(17)
    assert (await lc.read_state(store, conn, TEAM)).state == "Inactive"


async def test_if_the_transaction_fails_the_holon_stays_active(grants, monkeypatch):
    conn, store = _world()

    async def refuse(*_a, **_k):
        raise FluentError("a concurrent writer got there first")

    monkeypatch.setattr("holonbridge.fluent_trigger_action.update_fluents", refuse)
    with pytest.raises(FluentActionInvalid, match="none of the 1 row"):
        await fire(store, conn, lifecycle_query((TEAM, "Inactive")))
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"
    assert tx_mod is not None


async def test_a_fluent_row_for_an_inactive_holder_is_still_refused(grants):
    conn, store = _world()
    await go(store, conn, "Inactive")
    action = query(
        "SELECT ?fluent ?value ?operation WHERE { "
        f'BIND(<{SCORE}> AS ?fluent) BIND(1 AS ?value) BIND("Insert" AS ?operation) }}'
    )
    with pytest.raises(FluentActionInvalid, match="Inactive"):
        await fire(store, conn, action)
    assert score_of(store, conn) == Decimal(10)


async def test_a_trigger_cannot_write_the_lifecycle_fluent_as_an_ordinary_row(grants):
    """The route around ?holon is closed: naming the lifecycle fluent is still governed."""
    conn, store = _world()
    change = await go(store, conn, "Inactive")
    await go(store, conn, "Active")
    action = query(
        "SELECT ?fluent ?value ?isIri WHERE { "
        f'BIND(<{change.fluent}> AS ?fluent) BIND("{lc.INACTIVE}" AS ?value) BIND("true" AS ?isIri) }}'
    )
    with pytest.raises(FluentActionInvalid, match="lifecycle state"):
        await fire(store, conn, action)
    assert (await lc.read_state(store, conn, TEAM)).state == "Active"


async def test_an_empty_result_does_nothing(grants):
    conn, store = _world()
    outcome = await fire(store, conn, query("SELECT ?holon ?state WHERE { FILTER (false) }"))
    assert (outcome.applied, outcome.total) == (0, 0)
