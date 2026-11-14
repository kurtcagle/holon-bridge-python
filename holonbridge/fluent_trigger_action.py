"""The fluent action of a named trigger (``hev:onFireFluentQuery``).

A rule-based trigger stages a CONSTRUCT; an agent trigger places a call. This
action *changes fluents*: for each focus its condition matches, a named SELECT
(the "action query", bound to the focus) returns rows, and each row is applied
through :func:`holonbridge.fluent_transaction.update_fluents` as the person who
armed the trigger. It is how something inside the bridge can advance state, for example
"every tick, move each train on to its next stop", where before an outside
driver had to call ``update_fluent``.

Row columns of the action query:

====================  =========  ==============================================
``?fluent``           required   IRI of the ``holon:Fluent`` to update
``?value``            required   the new value (an IRI if the binding is one, or
                                 if ``?isIri`` is true; otherwise a literal,
                                 typed from its datatype)
``?operation``        optional   ``Set`` (default), ``Insert``, ``Remove``,
                                 ``ListInsert`` or ``ListRemove``
``?isIri``            optional   true to read a string ``?value`` as an IRI
``?assertedAt``       optional   valid time, ISO 8601 (a simulated clock's
                                 "now"); no timezone means UTC
``?description``      optional   free text recorded on the ledger entry
====================  =========  ==============================================

**No review.** A fluent update is a guarded compare-and-set, not a merge of
Turtle, so it cannot be staged as a candidate and approved later. A trigger with
this action must therefore be registered with ``reviewRequired=false``. The
definer's accountability comes from the checks below instead.

**Authority.** The definer must be able to do by hand what the trigger does: read
what the condition and action query read, and write the events, scene and
sequences graphs, which is what ``POST /fluent/update`` asks of a caller. The
same questions are asked again at every firing (see ``trigger_authz``).

**All rows or none.** Every row is checked (columns, IRIs, operation, timestamp,
a cap of :data:`MAX_ROWS`) before anything is applied, and the rows are then
applied as one transaction (:mod:`holonbridge.fluent_transaction`): if any row
cannot be applied, or a concurrent writer changed one of the fluents first,
nothing is written and the firing is a ``failed`` one, tried again on the next
evaluation. There is no ``partial`` outcome and an ``Insert`` is never half
applied. The price is that the rows of one firing share one valid time (give
every row the same ``?assertedAt``, or none), may not name the same fluent twice,
and are capped at the transaction limit.

**No cascade within one evaluation.** The updates run inside the trigger
evaluation, so the post-write hook does not evaluate state triggers for them
(``triggers._evaluating``). A chain "arrival fires a move, which causes the next
arrival" therefore advances one step per evaluation, not recursively.

This module has no HTTP in it. The routes and the evaluator call in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from .conn import Conn
from .delegation import DelegationError, safe_iri
from .fluent import FluentError, Operation
from .fluent_transaction import MAX_OPS, FluentOp, update_fluents
from .fuseki import FusekiClient, FusekiError
from .named_queries import NamedQuery
from .sequence import SequenceError
from .sparql_kind import form
from .trigger_authz import (
    PROBE_FOCUS,
    TriggerDenied,
    authorize_reads,
    bound_condition,
    require_grant,
)

if TYPE_CHECKING:  # pragma: no cover
    from .triggers import Trigger

#: Most rows one firing may apply: the most one transaction takes. A runaway
#: query should fail loudly, not write thousands of ledger entries.
MAX_ROWS = MAX_OPS

_XSD = "http://www.w3.org/2001/XMLSchema#"
_INTEGERS = {f"{_XSD}{n}" for n in ("integer", "int", "long", "short", "byte", "nonNegativeInteger",
                                     "positiveInteger", "negativeInteger", "nonPositiveInteger",
                                     "unsignedInt", "unsignedLong", "unsignedShort", "unsignedByte")}
_DECIMALS = {f"{_XSD}{n}" for n in ("decimal", "double", "float")}
_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no", ""}


class FluentActionInvalid(Exception):
    """The action query, or what it returned, cannot be applied. Raised before
    any row is applied."""


@dataclass(frozen=True)
class FluentRow:
    fluent: str
    operation: Operation
    value: Any
    is_iri: bool
    asserted_at: datetime | None
    description: str | None


@dataclass(frozen=True)
class FluentOutcome:
    """What a firing did: ``applied`` of ``total`` rows. The rows are one
    transaction, so this is all of them or the firing raised."""

    applied: int
    total: int


def bind_action(query: NamedQuery, focus: str | None) -> str:
    """The action query's SPARQL, with ``focus`` bound if it declares one.

    Like a context query: ``focus`` is supplied only to a query that declares a
    ``focus`` parameter (datatype ``anyURI``, so it renders as an IRI). It must
    be a SELECT.
    """
    from .agents.service import CallInvalid, bind_context  # noqa: PLC0415 - import cycle

    try:
        sparql = bind_context(query, focus)
    except CallInvalid as exc:
        raise FluentActionInvalid(str(exc).replace("context query", "action query")) from exc
    if form(sparql) != "SELECT":
        raise FluentActionInvalid(
            f"action query {query.id!r} is a {form(sparql)}; it must be a SELECT returning "
            "?fluent and ?value rows"
        )
    return sparql


def _cell(row: dict, name: str) -> dict | None:
    cell = row.get(name)
    return cell if isinstance(cell, dict) and "value" in cell else None


def _truthy(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise FluentActionInvalid(f"?isIri {text!r} is not a boolean")


def _timestamp(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise FluentActionInvalid(f"?assertedAt {text!r} is not an ISO 8601 date-time") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _typed(cell: dict) -> tuple[Any, bool]:
    """``(value, is_iri)`` for a ?value binding."""
    text = str(cell["value"])
    if cell.get("type") == "uri":
        return text, True
    datatype = cell.get("datatype", "")
    try:
        if datatype in _INTEGERS:
            return int(text), False
        if datatype in _DECIMALS:
            return Decimal(text), False
        if datatype == f"{_XSD}boolean":
            return _truthy(text), False
        if datatype == f"{_XSD}dateTime":
            return _timestamp(text), False
        if datatype == f"{_XSD}date":
            return date.fromisoformat(text.strip()), False
    except (ValueError, InvalidOperation, FluentActionInvalid) as exc:
        raise FluentActionInvalid(f"?value {text!r} is not a valid {datatype.rsplit('#', 1)[-1]}") from exc
    return text, False


def _safe(value: str, what: str) -> str:
    try:
        return safe_iri(value)
    except DelegationError as exc:
        raise FluentActionInvalid(f"{what}: {exc}") from exc


def parse_rows(bindings: list[dict]) -> list[FluentRow]:
    """Turn the action query's result rows into :class:`FluentRow` values.

    Checks everything and raises :class:`FluentActionInvalid` naming the row, so a
    bad row is found before any good one is applied. An empty result is fine: the
    condition matched but there is nothing to do.
    """
    if len(bindings) > MAX_ROWS:
        raise FluentActionInvalid(
            f"the action query returned {len(bindings)} rows; one firing applies at most {MAX_ROWS}"
        )
    rows: list[FluentRow] = []
    for index, binding in enumerate(bindings, start=1):
        label = f"row {index}"
        try:
            fluent_cell = _cell(binding, "fluent")
            value_cell = _cell(binding, "value")
            if fluent_cell is None:
                raise FluentActionInvalid("no ?fluent")
            if value_cell is None:
                raise FluentActionInvalid("no ?value")
            fluent = _safe(str(fluent_cell["value"]), "?fluent")

            op_cell = _cell(binding, "operation")
            op_text = str(op_cell["value"]).strip() if op_cell else Operation.SET.value
            try:
                operation = Operation(op_text)
            except ValueError as exc:
                raise FluentActionInvalid(
                    f"?operation {op_text!r} is not one of {', '.join(o.value for o in Operation)}"
                ) from exc

            value, is_iri = _typed(value_cell)
            flag = _cell(binding, "isIri")
            if flag is not None and _truthy(str(flag["value"])):
                if not isinstance(value, str):
                    raise FluentActionInvalid("?isIri is true but ?value is not a string")
                is_iri = True
            if is_iri:
                value = _safe(str(value), "?value")

            at_cell = _cell(binding, "assertedAt")
            desc_cell = _cell(binding, "description")
            rows.append(
                FluentRow(
                    fluent=fluent,
                    operation=operation,
                    value=value,
                    is_iri=is_iri,
                    asserted_at=_timestamp(str(at_cell["value"])) if at_cell else None,
                    description=str(desc_cell["value"]) if desc_cell else None,
                )
            )
        except FluentActionInvalid as exc:
            raise FluentActionInvalid(f"{label}: {exc}") from exc
    return rows


async def authorize_fluent_trigger(
    query_fn,
    conn: Conn,
    person: str | None,
    *,
    condition: NamedQuery,
    action_query: NamedQuery,
    review_required: bool,
) -> list[str]:
    """What the definer must be able to do for a fluent trigger to be theirs.

    Asked at registration, on re-activation and (cached) at firing. Returns
    non-fatal warnings.
    """
    if person is None:
        raise TriggerDenied(401, "unresolved identity")
    if review_required:
        raise TriggerDenied(
            400,
            {
                "error": "fluent_action_cannot_be_reviewed",
                "message": "a fluent update is a compare-and-set, not Turtle that can wait in the "
                "candidate queue; register this trigger with review_required=false",
            },
        )

    warnings: list[str] = []
    condition_sparql = bound_condition(condition)
    if form(condition_sparql) != "SELECT":
        raise TriggerDenied(
            400,
            {
                "error": "condition_not_select",
                "message": f"condition {condition.id!r} is a {form(condition_sparql)}; "
                "a trigger condition must be a SELECT projecting ?focus",
            },
        )
    try:
        action_sparql = bind_action(action_query, PROBE_FOCUS)
    except FluentActionInvalid as exc:
        raise TriggerDenied(
            400, {"error": "action_query_not_bindable", "message": str(exc)}, reason=str(exc)
        ) from exc
    if "?fluent" not in action_sparql and "$fluent" not in action_sparql:
        warnings.append(f"action query {action_query.id!r} never mentions ?fluent; it will apply nothing")
    if "focus" not in action_query.declared:
        warnings.append(
            f"action query {action_query.id!r} declares no 'focus' parameter, so every firing "
            "runs it unbound and applies the same rows for every matching focus"
        )

    await authorize_reads(
        query_fn, conn, person, label="condition", ident=condition.id, sparql=condition_sparql
    )
    await authorize_reads(
        query_fn, conn, person, label="action_query", ident=action_query.id, sparql=action_sparql
    )
    for graph in (conn.graph("events"), conn.scene_graph, conn.graph("sequences")):
        await require_grant(query_fn, conn, person, graph)
    return warnings


async def fire_fluent(
    client: FusekiClient,
    conn: Conn,
    trig: "Trigger",
    focus: str,
    *,
    action_query: NamedQuery,
) -> FluentOutcome:
    """Run the action query for ``focus`` and apply its rows as the definer.

    Raises :class:`FluentActionInvalid` if the query or a row is unusable or the
    transaction cannot be applied; nothing is written then, and the focus fires
    again on the next evaluation.
    """
    if not trig.armed_by:
        raise FluentActionInvalid(
            "a fluent action runs as the person who armed the trigger and this one has no "
            "recorded definer"
        )
    sparql = bind_action(action_query, focus)
    result = await client.select(conn, sparql)
    rows = parse_rows(result.get("results", {}).get("bindings", []))

    # A traveller's position changes only by crossing a portal (holonbridge/portal.py).
    # Checked for every row before any is applied, like the rest of the validation.
    from .portal import governing_portals, portal_governed_message  # noqa: PLC0415

    for number, row in enumerate(rows, start=1):
        governed = await governing_portals(client, conn, row.fluent)
        if governed:
            raise FluentActionInvalid(f"row {number}: {portal_governed_message(row.fluent, governed)}")

    if not rows:
        return FluentOutcome(0, 0)

    seen: set[str] = set()
    for number, row in enumerate(rows, start=1):
        if row.fluent in seen:
            raise FluentActionInvalid(
                f"row {number}: <{row.fluent}> appears in more than one row; the rows of a firing "
                "are one transaction, which changes each fluent once"
            )
        seen.add(row.fluent)
    times = {row.asserted_at for row in rows}
    if len(times) > 1:
        raise FluentActionInvalid(
            "the rows give different ?assertedAt values; the rows of a firing are one transaction "
            "with one valid time, so give every row the same ?assertedAt or none"
        )

    try:
        await update_fluents(
            client,
            conn,
            ops=[
                FluentOp(
                    fluent=row.fluent,
                    operation=row.operation,
                    value=row.value,
                    is_iri=row.is_iri,
                    description=row.description,
                )
                for row in rows
            ],
            asserted_by=trig.armed_by,
            asserted_datetime=times.pop(),
            description=f"trigger {trig.id}",
        )
    except (FluentError, SequenceError, FusekiError) as exc:
        reason = re.sub(r"\s+", " ", str(exc)).strip()[:300]
        raise FluentActionInvalid(f"none of the {len(rows)} row(s) was applied: {reason}") from exc
    return FluentOutcome(len(rows), len(rows))
