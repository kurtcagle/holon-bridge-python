"""Fluent-variable state transitions.

A fluent's current value lives in the *scene* graph as ``holon:currentValue``
-- a single destructively-updated triple (or, for list-mode fluents, a
destructively-updated set of ``holon:hasListItem`` triples). Every
transition that produces that value is also recorded, append-only and
forever, as an ``hevt:StateAssertion`` in the *events* graph, chained to its
predecessor via ``hevt:supersedes``.

``scene`` is a graph role distinct from ``projections`` -- the latter is
already the live ProjectionHook registry (see
:mod:`holonbridge.projection`), an unrelated feature. Do not conflate them.

Both the ledger append and the scene mutation happen in one SPARQL Update
request, so Jena executes them in one transaction -- a reader can never
observe a ledger entry with no matching scene update, or vice versa.

Concurrency follows the exact compare-and-set pattern already proven in
:mod:`holonbridge.sequence`: read the current value, build a guarded
DELETE/INSERT/WHERE that only fires if that value hasn't changed underneath
us, and retry on a losing race.

Init is not a separate operation. ``Operation.SET`` on a fluent with no
existing ``holon:currentValue`` simply matches no prior triple in its WHERE
clause and inserts fresh -- the same code path, not a special case. Clear
and Unset are deliberately not primitives here -- see :func:`prior_value`
for how a caller builds a "revert" from an ordinary Set.

Gating. ``sparql_update`` -- the only tool that can execute this module's
atomic multi-graph transaction -- has no ``shapes_graph`` hook of its own
(unlike ``push_turtle``/``ingest``, which validate before a single-graph
write). Confirmed by testing directly: a hand-crafted violation of
``holon:StateAssertionNonOverlapShape`` inserted via raw ``sparql_update``
met zero resistance. So this module validates itself, after the fact: once
a transition's write is confirmed, the fluent's own current-state
neighbourhood (never the whole ledger, which only grows) is checked against
whatever shapes exist in the dataset's shapes graph. A blocking violation is
compensated -- the just-inserted ledger entry is deleted and scene state is
restored to what it was -- and raised as a :class:`FluentError`. This is
detect-and-compensate, not prevent-before-write: there is a real, if brief,
window where the bad state exists in the store before the compensation
lands. A caller that cannot tolerate that window at all needs a different
mechanism (a SPARQL-level CONSTRUCT-based pre-check the caller runs before
ever calling this function) -- not something this module can close on its
own without the underlying tool gaining a real pre-write hook.

An absent or empty shapes graph is not an error -- it means no gate is
configured for this dataset, which is the common case, and transitions
proceed unchecked exactly as before this was added.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from . import sequence
from . import shacl as shacl_mod
from .conn import Conn
from .fuseki import FusekiClient
from .turtle import escape_literal

HOLON_NS = "https://w3id.org/holon/"
HEVT_NS = "https://w3id.org/holon/event/"

XSD_DECIMAL = "http://www.w3.org/2001/XMLSchema#decimal"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"
XSD_DATE = "http://www.w3.org/2001/XMLSchema#date"
XSD_DATETIME = "http://www.w3.org/2001/XMLSchema#dateTime"

_PREFIXES = f"""PREFIX holon: <{HOLON_NS}>
PREFIX hevt:  <{HEVT_NS}>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
"""


class FluentError(RuntimeError):
    """A fluent update was rejected -- wrong mode/operation pairing, a delta
    operation with no prior value, a post-write SHACL violation that was
    compensated, or exhausted retry attempts."""

    def __init__(self, message: str, *, report: "shacl_mod.ValidationReport | None" = None) -> None:
        super().__init__(message)
        self.report = report


class OperationMode(str, Enum):
    NUMERIC = "NumericAccumulator"
    DATE = "DateAccumulator"
    SET_VALUE = "SetValueMode"
    LIST = "ListAccumulator"


class Operation(str, Enum):
    SET = "Set"
    INSERT = "Insert"
    REMOVE = "Remove"
    LIST_INSERT = "ListInsert"
    LIST_REMOVE = "ListRemove"


# Which operations are legal for which mode. Checked before any query runs.
_VALID_OPS: dict[OperationMode, frozenset[Operation]] = {
    OperationMode.NUMERIC: frozenset({Operation.SET, Operation.INSERT, Operation.REMOVE}),
    OperationMode.DATE: frozenset({Operation.SET, Operation.INSERT, Operation.REMOVE}),
    OperationMode.SET_VALUE: frozenset({Operation.SET}),
    OperationMode.LIST: frozenset({Operation.LIST_INSERT, Operation.LIST_REMOVE}),
}


@dataclass(frozen=True)
class TypedValue:
    """A value as SPARQL JSON actually reports it -- kind distinguishes an
    IRI from a literal, which a bare string cannot."""

    kind: Literal["uri", "literal"]
    lexical: str
    datatype: str | None = None

    def as_term(self) -> str:
        """Render as a Turtle term for use in a query or update."""
        if self.kind == "uri":
            return f"<{self.lexical}>"
        if self.datatype:
            return f'"{escape_literal(self.lexical)}"^^<{self.datatype}>'
        return f'"{escape_literal(self.lexical)}"'

    @classmethod
    def from_binding(cls, binding: dict[str, Any]) -> "TypedValue":
        kind = "uri" if binding.get("type") == "uri" else "literal"
        return cls(
            kind=kind,
            lexical=binding["value"],
            datatype=binding.get("datatype"),
        )


@dataclass(frozen=True)
class FluentUpdateResult:
    fluent: str
    operation: Operation
    old_value: TypedValue | None
    new_value: TypedValue
    assertion_iri: str
    superseded: str | None
    sequence_id: str


def _render_input(value: Any, *, is_iri: bool = False) -> str:
    """Render a caller-supplied Python value (not a value read back from
    the store) as a Turtle term."""
    if is_iri:
        return f"<{value}>"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f'"{value}"^^xsd:integer'
    if isinstance(value, Decimal):
        return f'"{value}"^^xsd:decimal'
    if isinstance(value, float):
        return f'"{value}"^^xsd:decimal'
    if isinstance(value, datetime):
        return f'"{value.isoformat()}"^^xsd:dateTime'
    if isinstance(value, date):
        return f'"{value.isoformat()}"^^xsd:date'
    return f'"{escape_literal(str(value))}"'


async def _read_operation_mode(
    client: FusekiClient, conn: Conn, *, fluent: str
) -> OperationMode:
    query = f"""{_PREFIXES}
SELECT ?mode WHERE {{
  GRAPH <{conn.holons_graph}> {{
    <{fluent}> holon:fluentProperty ?prop .
  }}
  GRAPH <{conn.ontology_graph}> {{
    ?prop holon:fluentOperationMode ?modeIri .
  }}
  BIND(STRAFTER(STR(?modeIri), "{HOLON_NS}") AS ?mode)
}} LIMIT 1"""
    results = await client.select(conn, query)
    bindings = results.get("results", {}).get("bindings", [])
    if not bindings:
        raise FluentError(
            f"<{fluent}>'s FluentProperty has no holon:fluentOperationMode "
            "declared -- this is a modelling gap, not something to default "
            "silently from"
        )
    return OperationMode(bindings[0]["mode"]["value"])


async def _read_current(
    client: FusekiClient, conn: Conn, *, fluent: str
) -> tuple[TypedValue | None, str | None]:
    """Return (current value, current StateAssertion IRI), typed properly
    from the SPARQL JSON binding rather than sniffed from its string form.
    (None, None) means this fluent has never been set."""
    query = f"""{_PREFIXES}
SELECT ?value ?assertion WHERE {{
  GRAPH <{conn.scene_graph}> {{
    <{fluent}> holon:currentValue ?value .
    OPTIONAL {{ <{fluent}> holon:currentAssertion ?assertion . }}
  }}
}} LIMIT 1"""
    results = await client.select(conn, query)
    bindings = results.get("results", {}).get("bindings", [])
    if not bindings:
        return None, None
    row = bindings[0]
    value = TypedValue.from_binding(row["value"])
    assertion = row.get("assertion", {}).get("value")
    return value, assertion


async def _validate_transition(
    client: FusekiClient, conn: Conn, *, fluent: str
) -> "shacl_mod.ValidationReport | None":
    """Check this fluent's own current-state neighbourhood -- its
    non-invalidated, non-superseded StateAssertion(s), which should be
    exactly one -- against whatever shapes exist in the dataset's shapes
    graph. Scoped deliberately: validating the whole events graph on every
    transition would grow with the ledger forever; this stays O(1) per
    fluent regardless of how long its history gets.

    Returns None (nothing to check) when the shapes graph is empty or
    absent, rather than treating an unconfigured gate as a violation --
    most datasets will not have fluent shapes declared, and that is a
    legitimate, common state, not an error.
    """
    shapes_turtle = await client.get_graph(conn, conn.shapes_graph)
    if not shapes_turtle.strip():
        return None

    scope_query = f"""{_PREFIXES}
CONSTRUCT {{ ?assertion ?p ?o . }}
WHERE {{
  GRAPH <{conn.graph("events")}> {{
    ?assertion hevt:forFluent <{fluent}> ; ?p ?o .
    FILTER NOT EXISTS {{ ?assertion hevt:invalidatedAt ?anyInvalidation }}
    FILTER NOT EXISTS {{ ?later hevt:supersedes ?assertion }}
  }}
}}"""
    scoped_turtle = await client.construct(conn, scope_query)

    scratch = f"urn:{conn.dataset}:scratch:fluent-validate-{uuid.uuid4().hex}"
    try:
        await client.post_graph(conn, scratch, scoped_turtle)
        report_turtle = await client.shacl_validate(
            conn, target_graph=scratch, shapes_turtle=shapes_turtle
        )
    finally:
        await client.drop_graph(conn, scratch)

    return shacl_mod.parse_report(report_turtle)


async def update_fluent(
    client: FusekiClient,
    conn: Conn,
    *,
    fluent: str,
    operation: Operation,
    value: Any = None,
    is_iri: bool = False,
    asserted_datetime: datetime | None = None,
    asserted_by: str | None = None,
    description: str | None = None,
    attempts: int = 8,
) -> FluentUpdateResult:
    """Perform one fluent transition: an atomic ledger append plus a
    destructive scene-graph update, in a single SPARQL request, then a
    scoped post-write SHACL check (see the module docstring's "Gating"
    section for what this does and does not guarantee).

    ``value`` means different things per operation: the absolute new value
    for SET, the delta magnitude for INSERT/REMOVE, the member IRI/literal
    for LIST_INSERT/LIST_REMOVE.
    """
    mode = await _read_operation_mode(client, conn, fluent=fluent)
    if operation not in _VALID_OPS[mode]:
        raise FluentError(
            f"{operation.value} is not valid for a {mode.value} fluent "
            f"(<{fluent}>)"
        )

    asserted_dt = asserted_datetime or datetime.now(timezone.utc)
    events_graph = conn.graph("events")
    scene_graph = conn.scene_graph

    for _ in range(attempts):
        current, current_assertion = await _read_current(client, conn, fluent=fluent)

        new_value, guard_clause, scene_delete, scene_insert = _plan(
            mode, operation, current, value, fluent, is_iri
        )

        minted = await sequence.mint(
            client,
            conn,
            name="event",
            purpose=f"hevt:StateAssertion for <{fluent}>, operation {operation.value}",
            authorised_by=asserted_by,
        )
        assertion_iri = minted.iri

        clauses = [
            "a hevt:StateAssertion",
            f"hevt:oldValue {current.as_term()}" if current is not None else "",
            f"hevt:deltaValue {_render_input(value)}"
            if operation in (Operation.INSERT, Operation.REMOVE)
            else "",
            f"hevt:hasValue {new_value.as_term()}",
            f"hevt:operation hevt:{operation.value}",
            f"hevt:forFluent <{fluent}>",
            f'hevt:assertedDateTime "{asserted_dt.isoformat()}"^^xsd:dateTime',
            f"hevt:sequenceId <{minted.iri}>",
            f"hevt:assertedBy <{asserted_by}>" if asserted_by else "",
            f'holon:description "{escape_literal(description)}"@en' if description else "",
            f"hevt:supersedes <{current_assertion}>" if current_assertion else "",
        ]
        clause_body = " ;\n    ".join(c for c in clauses if c)

        ledger_insert = f"""INSERT DATA {{
  GRAPH <{events_graph}> {{
    <{assertion_iri}>
    {clause_body} .
  }}
}}"""

        scene_update = f"""DELETE {{
  GRAPH <{scene_graph}> {{
    {scene_delete}
  }}
}}
INSERT {{
  GRAPH <{scene_graph}> {{
    {scene_insert}
  }}
}}
WHERE {{
  {guard_clause}
}}"""

        full_update = f"{_PREFIXES}\n{ledger_insert}\n;\n{scene_update}"
        await client.update(conn, full_update)

        confirmed_value, confirmed_assertion = await _read_current(
            client, conn, fluent=fluent
        )
        if confirmed_assertion != assertion_iri:
            # Lost the race: another writer's transition landed between our
            # read and our write. Retry from a fresh read, same as
            # sequence.mint().
            continue

        report = await _validate_transition(client, conn, fluent=fluent)
        if report is not None and report.blocking:
            await _compensate(
                client,
                conn,
                fluent=fluent,
                assertion_iri=assertion_iri,
                prior_value=current,
                prior_assertion=current_assertion,
            )
            raise FluentError(
                f"<{fluent}> {operation.value} rejected by shapes graph "
                f"<{conn.shapes_graph}> ({len(report.blocking)} violation(s)); "
                "write was compensated -- scene and ledger are as if this "
                "transition never happened, except for the ledger entry "
                "itself, which nothing ever deletes (see below)",
                report=report,
            )

        return FluentUpdateResult(
            fluent=fluent,
            operation=operation,
            old_value=current,
            new_value=confirmed_value or new_value,
            assertion_iri=assertion_iri,
            superseded=current_assertion,
            sequence_id=minted.iri,
        )

    raise FluentError(
        f"could not update <{fluent}> after {attempts} attempts "
        "(contention from a concurrent writer)"
    )


async def _compensate(
    client: FusekiClient,
    conn: Conn,
    *,
    fluent: str,
    assertion_iri: str,
    prior_value: TypedValue | None,
    prior_assertion: str | None,
) -> None:
    """Undo a transition that failed post-write validation: restore scene
    to exactly what it held before, and mark (never delete) the rejected
    ledger entry.

    The ledger entry itself is NOT deleted -- this module's whole premise
    is that the ledger is append-only, and an entry that was written and
    then rejected is still a true fact ("this was attempted, and shown to
    violate a shape") worth keeping. It is marked hevt:invalidatedAt /
    hevt:invalidationReason instead, using the same correct-without-erasing
    mechanism the ledger already has for every other kind of correction.
    Only the *scene* graph -- which was always meant to be disposable and
    reconstructable -- gets rolled back outright.
    """
    now = datetime.now(timezone.utc).isoformat()
    scene_restore = (
        f"""INSERT {{
  GRAPH <{conn.scene_graph}> {{
    <{fluent}> holon:currentValue {prior_value.as_term()} ;
               holon:currentAssertion <{prior_assertion}> .
  }}
}}
WHERE {{ }}"""
        if prior_value is not None and prior_assertion is not None
        else "# nothing to restore -- this was an Init, so scene simply reverts to absent"
    )

    update = f"""{_PREFIXES}
DELETE {{
  GRAPH <{conn.scene_graph}> {{
    <{fluent}> holon:currentValue ?anyValue ;
               holon:currentAssertion ?anyAssertion .
  }}
}}
WHERE {{
  GRAPH <{conn.scene_graph}> {{
    <{fluent}> holon:currentValue ?anyValue .
    OPTIONAL {{ <{fluent}> holon:currentAssertion ?anyAssertion . }}
  }}
}}
;
{scene_restore}
;
INSERT DATA {{
  GRAPH <{conn.graph("events")}> {{
    <{assertion_iri}> hevt:invalidatedAt "{now}"^^xsd:dateTime ;
                       hevt:invalidationReason "rejected by post-write SHACL validation; scene state was compensated" .
  }}
}}"""
    await client.update(conn, update)


def _plan(
    mode: OperationMode,
    operation: Operation,
    current: TypedValue | None,
    value: Any,
    fluent: str,
    is_iri: bool,
) -> tuple[TypedValue, str, str, str]:
    """Compute (new_value, WHERE guard, scene DELETE pattern, scene INSERT
    pattern) for one transition. All arithmetic happens here, in Python --
    not pushed into SPARQL, since SPARQL 1.1 has no trustworthy native
    date-duration arithmetic and list membership isn't a scalar operation
    regardless."""

    if operation in (Operation.LIST_INSERT, Operation.LIST_REMOVE):
        member_term = _render_input(value, is_iri=is_iri)
        member_typed = TypedValue(
            kind="uri" if is_iri else "literal", lexical=str(value)
        )
        if operation is Operation.LIST_INSERT:
            guard = "# adding a member never conflicts with another add"
            return member_typed, guard, "", f"<{fluent}> holon:hasListItem {member_term} ."
        else:
            guard = f"<{fluent}> holon:hasListItem {member_term} ."
            return member_typed, guard, f"<{fluent}> holon:hasListItem {member_term} .", ""

    # Scalar modes: SET, INSERT, REMOVE.
    if operation is Operation.SET:
        new_typed = TypedValue(
            kind="uri" if is_iri else "literal",
            lexical=_lexical_form(value),
            datatype=_xsd_datatype(value) if not is_iri else None,
        )
    elif current is None:
        raise FluentError(
            f"<{fluent}> has no current value; {operation.value} requires "
            "one -- use Set to initialise"
        )
    elif mode is OperationMode.NUMERIC:
        old_num = Decimal(current.lexical)
        delta = Decimal(str(value))
        new_num = old_num + delta if operation is Operation.INSERT else old_num - delta
        new_typed = TypedValue(kind="literal", lexical=str(new_num), datatype=XSD_DECIMAL)
    elif mode is OperationMode.DATE:
        old_date = date.fromisoformat(current.lexical[:10])
        delta_days = value if isinstance(value, timedelta) else timedelta(days=int(value))
        new_date = old_date + delta_days if operation is Operation.INSERT else old_date - delta_days
        new_typed = TypedValue(kind="literal", lexical=new_date.isoformat(), datatype=XSD_DATE)
    else:
        raise FluentError(f"{operation.value} is not valid for {mode.value}")

    if current is None:
        guard = f"FILTER NOT EXISTS {{ <{fluent}> holon:currentValue ?anyExisting }}"
        scene_delete = ""
    else:
        guard = (
            f"<{fluent}> holon:currentValue ?existingValue .\n"
            f"  FILTER (?existingValue = {current.as_term()})"
        )
        scene_delete = f"<{fluent}> holon:currentValue ?existingValue ."

    scene_insert = f"<{fluent}> holon:currentValue {new_typed.as_term()} ."
    return new_typed, guard, scene_delete, scene_insert


def _lexical_form(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _xsd_datatype(value: Any) -> str | None:
    if isinstance(value, bool):
        return XSD_BOOLEAN
    if isinstance(value, int):
        return XSD_INTEGER
    if isinstance(value, (Decimal, float)):
        return XSD_DECIMAL
    if isinstance(value, datetime):
        return XSD_DATETIME
    if isinstance(value, date):
        return XSD_DATE
    return None


async def prior_value(
    client: FusekiClient, conn: Conn, *, fluent: str
) -> TypedValue | None:
    """The value the fluent held immediately before its current one --
    read from the ledger, for a caller building a Set-based 'revert'.
    Returns None if there is no prior transition."""
    query = f"""{_PREFIXES}
SELECT ?priorValue WHERE {{
  GRAPH <{conn.scene_graph}> {{
    <{fluent}> holon:currentAssertion ?current .
  }}
  GRAPH <{conn.graph("events")}> {{
    ?current hevt:supersedes ?priorAssertion .
    ?priorAssertion hevt:hasValue ?priorValue .
  }}
}} LIMIT 1"""
    results = await client.select(conn, query)
    bindings = results.get("results", {}).get("bindings", [])
    if not bindings:
        return None
    return TypedValue.from_binding(bindings[0]["priorValue"])
