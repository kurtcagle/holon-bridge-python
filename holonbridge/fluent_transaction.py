"""All-or-nothing updates to several fluents.

:func:`holonbridge.fluent.update_fluent` changes one fluent. Some changes are
not one fluent: a train leaves one station and the next one's occupancy
changes, a character crosses a door and loses a key. Done as two calls, a
failure or a lost race between them leaves the world half moved. This module
applies a set of fluent operations as one unit.

How the single-fluent guarantee extends
---------------------------------------

``update_fluent`` is a compare-and-set: its ledger append and its scene change
carry the same guard ("the current value is still what I read"), sit in one
SPARQL request, and so either both happen or neither does. A transaction keeps
exactly that shape with one change: there is **one** guard, the conjunction of
every member's guard, and it is shared by

1. a single ``INSERT`` that appends every member's ``hevt:StateAssertion`` and
   one ``hevt:Transaction`` record, and
2. a single ``DELETE/INSERT`` that moves every member's scene value.

A member whose guard no longer holds makes the whole conjunction fail, so
neither statement writes anything. It cannot be done with one statement per
member: the first scene change would break the next member's guard, and a
failure in the middle could not be undone. Because the ledger statement does not
touch the scene graph, both statements see the same state, as they do for one
fluent. Fuseki runs the request in one transaction.

What it does not do
-------------------

* ``FluentOp.expect`` makes a member conditional on the value it read: a
  mismatch refuses the whole transaction (not retried, since a fresh read
  would only confirm it).
* One operation per fluent. Two on the same fluent have no defined order against
  each other's guard, so they are refused rather than guessed at.
* It is not a distributed transaction: everything is in one dataset.
* A pre-commit SHACL gate (when the dataset has shapes) validates the **whole**
  candidate on scratch copies first: every member's new assertion, and with
  ``FLUENT_GATE_SCOPE=holder`` each member's holder neighbourhood. A shape that
  relates two members (a train's location and the station's occupancy) sees both
  new values. Nothing live is touched if it fails.
* State triggers are evaluated once, after the commit.

Each ledger entry carries ``hevt:inTransaction <tx>`` so the entries of one
unit can be found again; the ``hevt:Transaction`` record holds the caller, the
valid and transaction times and, for a portal crossing, ``hevt:viaPortal`` and
``hevt:traveller``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import sequence
from . import shacl as shacl_mod
from .conn import Conn
from .fluent import (
    GATE_SCOPE_ASSERTION,
    GATE_SCOPES,
    _PREFIXES,
    FluentError,
    FluentUpdateResult,
    Operation,
    TypedValue,
    _CandidateDidNotFire,
    _evaluate_state_triggers,
    _is_list_member,
    _plan,
    _precommit_check_many,
    _read_current,
    _read_operation_mode,
    _render_input,
    _VALID_OPS,
    _where,
)
from .fuseki import FusekiClient
from .turtle import escape_literal

log = logging.getLogger("holonbridge.fluent_transaction")

MAX_OPS = 20


@dataclass(frozen=True)
class FluentOp:
    """One member of a transaction. Meanings of ``value`` are as for
    :func:`holonbridge.fluent.update_fluent`."""

    fluent: str
    operation: Operation
    value: Any = None
    is_iri: bool = False
    description: str | None = None
    #: If set, the fluent's current value must be exactly this when the
    #: transaction reads it, or the whole transaction is refused. The read is the
    #: one the guard then pins, so the check cannot be overtaken by a concurrent
    #: writer (a portal uses it for "the traveller is at the origin").
    expect: TypedValue | None = None


@dataclass(frozen=True)
class TransactionResult:
    transaction: str
    results: list[FluentUpdateResult]


_VAR = re.compile(r"\?(existingValue|existingAssertion|anyExisting)\b")


def _suffixed(text: str, index: int) -> str:
    """Give the per-fluent variables of one member's guard (and scene delete
    pattern) a suffix, so several members can sit in one WHERE group."""
    return _VAR.sub(lambda m: f"?{m.group(1)}{index}", text)


async def update_fluents(
    client: FusekiClient,
    conn: Conn,
    *,
    ops: list[FluentOp],
    asserted_datetime: datetime | None = None,
    asserted_by: str | None = None,
    description: str | None = None,
    via_portal: str | None = None,
    traveller: str | None = None,
    attempts: int = 8,
    gate_scope: str = GATE_SCOPE_ASSERTION,
) -> TransactionResult:
    """Apply ``ops`` as one unit: all of them, or none (see the module docs)."""
    if gate_scope not in GATE_SCOPES:
        raise FluentError(f"gate_scope must be one of {', '.join(GATE_SCOPES)}, not {gate_scope!r}")
    if not ops:
        raise FluentError("a transaction needs at least one operation")
    if len(ops) > MAX_OPS:
        raise FluentError(f"a transaction takes at most {MAX_OPS} operations, not {len(ops)}")
    seen: set[str] = set()
    for op in ops:
        if op.fluent in seen:
            raise FluentError(
                f"<{op.fluent}> appears more than once; a transaction changes each fluent once"
            )
        seen.add(op.fluent)

    modes = {}
    for op in ops:
        mode = await _read_operation_mode(client, conn, fluent=op.fluent)
        if op.operation not in _VALID_OPS[mode]:
            raise FluentError(
                f"{op.operation.value} is not valid for a {mode.value} fluent (<{op.fluent}>)"
            )
        modes[op.fluent] = mode

    asserted_dt = asserted_datetime or datetime.now(timezone.utc)
    events_graph = conn.graph("events")
    scene_graph = conn.scene_graph

    shapes_turtle = await client.get_graph(conn, conn.shapes_graph)
    gated = bool(shapes_turtle.strip())
    if gated:
        try:
            shacl_mod.ensure_enforceable(shapes_turtle)
        except ValueError as exc:
            raise FluentError(str(exc)) from exc

    fluents = [op.fluent for op in ops]

    for _ in range(attempts):
        planned = []
        for op in ops:
            current, current_assertion = await _read_current(client, conn, fluent=op.fluent)
            if op.expect is not None and (
                current is None
                or current.kind != op.expect.kind
                or current.lexical != op.expect.lexical
            ):
                held = "no value" if current is None else f"{current.lexical}"
                raise FluentError(
                    f"<{op.fluent}> holds {held}, not the expected {op.expect.lexical}; "
                    "nothing was written"
                )
            if op.operation is Operation.LIST_REMOVE and not await _is_list_member(
                client, conn, fluent=op.fluent, value=op.value, is_iri=op.is_iri
            ):
                raise FluentError(
                    f"<{op.fluent}> does not have {op.value} in its list; nothing to remove"
                )
            minted = await sequence.mint(
                client,
                conn,
                name="event",
                purpose=f"hevt:StateAssertion for <{op.fluent}>, operation {op.operation.value}, in a transaction",
                authorised_by=asserted_by,
            )
            new_value, guard, scene_delete, scene_insert = _plan(
                modes[op.fluent], op.operation, current, op.value, op.fluent, op.is_iri, minted.iri
            )
            planned.append((op, current, current_assertion, minted, new_value, guard, scene_delete, scene_insert))

        tx = await sequence.mint(
            client,
            conn,
            name="transaction",
            purpose=f"hevt:Transaction over {len(ops)} fluent(s)",
            authorised_by=asserted_by,
        )

        inserted_at = datetime.now(timezone.utc).isoformat()
        ledger_blocks: list[str] = []
        guards: list[str] = []
        scene_deletes: list[str] = []
        scene_inserts: list[str] = []
        for index, (op, current, current_assertion, minted, new_value, guard, s_del, s_ins) in enumerate(planned):
            clauses = [
                "a hevt:StateAssertion",
                f"hevt:oldValue {current.as_term()}" if current is not None else "",
                f"hevt:deltaValue {_render_input(op.value)}"
                if op.operation in (Operation.INSERT, Operation.REMOVE)
                else "",
                f"hevt:hasValue {new_value.as_term()}",
                f"hevt:operation hevt:{op.operation.value}",
                f"hevt:forFluent <{op.fluent}>",
                f'hevt:assertedDateTime "{asserted_dt.isoformat()}"^^xsd:dateTime',
                f'hevt:insertedAt "{inserted_at}"^^xsd:dateTime',
                f"hevt:sequenceId <{minted.iri}>",
                f"hevt:inTransaction <{tx.iri}>",
                f"hevt:assertedBy <{asserted_by}>" if asserted_by else "",
                _description(op.description or description),
                f"hevt:supersedes <{current_assertion}>" if current_assertion else "",
            ]
            ledger_blocks.append(f"<{minted.iri}>\n    " + " ;\n    ".join(c for c in clauses if c) + " .")
            if guard.strip():
                guards.append(_suffixed(guard, index))
            if s_del.strip():
                scene_deletes.append(_suffixed(s_del, index))
            if s_ins.strip():
                scene_inserts.append(s_ins)

        tx_clauses = [
            "a hevt:Transaction",
            f'hevt:assertedDateTime "{asserted_dt.isoformat()}"^^xsd:dateTime',
            f'hevt:insertedAt "{inserted_at}"^^xsd:dateTime',
            f"hevt:assertedBy <{asserted_by}>" if asserted_by else "",
            f"hevt:viaPortal <{via_portal}>" if via_portal else "",
            f"hevt:traveller <{traveller}>" if traveller else "",
            _description(description),
        ]
        ledger_blocks.append(f"<{tx.iri}>\n    " + " ;\n    ".join(c for c in tx_clauses if c) + " .")

        where = _where(scene_graph, "\n    ".join(guards))
        ledger_insert = f"""INSERT {{
  GRAPH <{events_graph}> {{
    {chr(10).join(ledger_blocks)}
  }}
}}
{where}"""
        scene_update = f"""DELETE {{
  GRAPH <{scene_graph}> {{
    {chr(10).join(scene_deletes)}
  }}
}}
INSERT {{
  GRAPH <{scene_graph}> {{
    {chr(10).join(scene_inserts)}
  }}
}}
{where}"""

        if gated:
            try:
                report = await _precommit_check_many(
                    client,
                    conn,
                    fluents=fluents,
                    events_graph=events_graph,
                    scene_graph=scene_graph,
                    ledger_insert=ledger_insert,
                    scene_update=scene_update,
                    shapes_turtle=shapes_turtle,
                    gate_scope=gate_scope,
                )
            except _CandidateDidNotFire:
                continue
            if report.blocking:
                raise FluentError(
                    f"transaction over {len(ops)} fluent(s) rejected by shapes graph "
                    f"<{conn.shapes_graph}> ({len(report.blocking)} violation(s)); nothing "
                    "live was touched -- checked on a scratch copy first",
                    report=report,
                )

        await client.update(conn, f"{_PREFIXES}\n{ledger_insert}\n;\n{scene_update}")

        if not await _transaction_exists(client, conn, events_graph=events_graph, tx=tx.iri):
            # The shared guard did not hold: another writer changed one of the
            # members between our read and our write, and so NOTHING was written
            # (neither statement fires). Retry from fresh reads.
            continue

        await _evaluate_state_triggers(client, conn, fluent=fluents[0])

        return TransactionResult(
            transaction=tx.iri,
            results=[
                FluentUpdateResult(
                    fluent=op.fluent,
                    operation=op.operation,
                    old_value=current,
                    new_value=new_value,
                    assertion_iri=minted.iri,
                    superseded=current_assertion,
                    sequence_id=minted.iri,
                )
                for op, current, current_assertion, minted, new_value, *_ in planned
            ],
        )

    raise FluentError(
        f"could not apply the transaction over {len(ops)} fluent(s) after {attempts} attempts "
        "(contention from a concurrent writer); nothing was written"
    )


def _description(text: str | None) -> str:
    return f'holon:description "{escape_literal(text)}"@en' if text else ""


async def _transaction_exists(
    client: FusekiClient, conn: Conn, *, events_graph: str, tx: str
) -> bool:
    query = f"""{_PREFIXES}
SELECT ?tx WHERE {{
  GRAPH <{events_graph}> {{ <{tx}> a hevt:Transaction . BIND(<{tx}> AS ?tx) }}
}} LIMIT 1"""
    results = await client.select(conn, query)
    return bool(results.get("results", {}).get("bindings", []))
