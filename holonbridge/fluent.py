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
us, and retry on a losing race. The WHERE clause's guard pattern is
explicitly wrapped in ``GRAPH <scene_graph> { ... }`` -- earlier versions
of this module omitted that wrapper, which only worked because the Fuseki
datasets used for testing happen to have union-default-graph enabled. On a
dataset without it, an unwrapped guard silently matches nothing. Found by
hand-tracing the exact query text during a live pre-commit test.

``holon:currentAssertion`` on the scene graph is what lets a transition
find its predecessor to supersede in O(1), with no events-graph scan --
but an earlier version of ``_plan`` never actually wrote it: the scalar
branch's ``scene_insert``/``scene_delete`` touched only
``holon:currentValue``. That is a severe bug, not a cosmetic gap --
confirmed live, not just reasoned through: with a shapes graph configured,
a Set followed by an Insert produced two StateAssertions with neither
superseding the other (since ``current_assertion`` read back as ``None``
forever), which ``holon:StateAssertionNonOverlapShape`` correctly flags as
a violation -- meaning every legitimate second-or-later transition on any
gated dataset would be rejected by the very gate meant to protect it. Fixed
by having ``_plan`` accept the new transition's own ``assertion_iri`` (so
``sequence.mint`` now runs before ``_plan``, not after -- minting doesn't
depend on the plan, so this reordering is free) and write/replace
``holon:currentAssertion`` alongside ``holon:currentValue`` in the scalar
branch. List-mode fluents don't need this: membership has no single
"current assertion" to point at in the first place.

Init is not a separate operation. ``Operation.SET`` on a fluent with no
existing ``holon:currentValue`` simply matches no prior triple in its WHERE
clause and inserts fresh -- the same code path, not a special case. Clear
and Unset are deliberately not primitives here -- see :func:`prior_value`
for how a caller builds a "revert" from an ordinary Set.

Gating: true pre-commit prevention, not detect-and-compensate. An earlier
version of this module validated after the live write and rolled back on
a violation -- but ``sparql_update`` (the only tool that can execute this
module's atomic multi-graph transaction) has no ``shapes_graph`` hook of
its own, so there was a real, if brief, window where invalid state existed
in the store before compensation landed, and a second concurrent writer
could race the compensating write itself.

Both problems have the same root cause: validating *after* the graphs
that matter are already live. So this validates on scratch copies first.
``_precommit_check`` COPYs the affected graphs (events, scene) to scratch,
applies the *exact same* candidate SPARQL text against those copies (only
the graph IRIs are substituted -- nothing else differs from what would run
against the live graphs), scopes a check to just this fluent's own
current-state neighbourhood (never the whole ledger, which only grows --
this stays O(1) per fluent regardless of history length), and validates
that against the dataset's shapes graph. Only if that conforms does the
identical update run for real, against the live graphs. A rejected
transition never appears in ``events`` or ``scene`` -- not even briefly,
and there is nothing to compensate because nothing live was ever touched.
Verified directly, twice: once for a clean transition (confirmed a live
graph re-read shows exactly the validated state), and once for the
currentAssertion bug above (confirmed the failure live, not hypothesised),
after which the fix was re-verified the same way -- a Set-then-Insert
sequence on a gated dataset now produces a correctly superseded chain and
passes pre-commit validation at every step.

An absent or empty shapes graph is not an error -- it means no gate is
configured for this dataset, which is the common case, and transitions
proceed straight to the live commit, exactly as before this was added, at
no extra cost.

CHANGED 2026-08-26: a successful transition now also evaluates every
Active ``StateTrigger`` (see :mod:`holonbridge.triggers`) -- write-driven,
contextual condition checking, distinct from the scheduler's periodic
sweep of ``TemporalTrigger``s. This runs *after* the transition is fully
committed and confirmed (never inside the retry loop, and never able to
affect whether this transition itself succeeds), and a failure in trigger
evaluation is logged, never raised -- a broken or misconfigured trigger
must not be able to break the fluent write that happens to trip it. No
``touched_predicates`` narrowing is applied here yet: every active
StateTrigger is evaluated on every fluent transition, which is correct
but not the cheapest it could be -- see ``triggers.py``'s module
docstring for why that's an explicit, deferred choice rather than an
oversight.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger("holonbridge.fluent")

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
    operation with no prior value, a pre-commit SHACL violation (nothing
    live was touched), or exhausted retry attempts."""

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
    client: FusekiClient, conn: Conn, *, fluent: str, scene_graph: str | None = None
) -> tuple[TypedValue | None, str | None]:
    """Return (current value, current StateAssertion IRI), typed properly
    from the SPARQL JSON binding rather than sniffed from its string form.
    (None, None) means this fluent has never been set.

    ``scene_graph`` defaults to the live scene graph; a caller checking a
    scratch copy during pre-commit validation passes the scratch IRI
    instead, so this same helper works for both.
    """
    graph = scene_graph or conn.scene_graph
    query = f"""{_PREFIXES}
SELECT ?value ?assertion WHERE {{
  GRAPH <{graph}> {{
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


async def _precommit_check(
    client: FusekiClient,
    conn: Conn,
    *,
    fluent: str,
    events_graph: str,
    scene_graph: str,
    ledger_insert: str,
    scene_update: str,
    shapes_turtle: str,
) -> "shacl_mod.ValidationReport":
    """Validate the exact candidate transition before it ever touches a
    live graph.

    Copies ``events_graph`` and ``scene_graph`` to scratch graphs, applies
    ``ledger_insert``/``scene_update`` against those copies with only the
    graph IRIs substituted -- the SPARQL text is otherwise byte-for-byte
    what would run against the live graphs, so what gets validated is
    exactly the candidate transition, not an approximation of it. Then
    scopes a check to this fluent's own current-state neighbourhood (its
    non-invalidated, non-superseded StateAssertion(s), which should be
    exactly one) against ``shapes_turtle``.

    Everything scratch is dropped before returning, success or failure.
    """
    token = uuid.uuid4().hex
    scratch_events = f"urn:{conn.dataset}:scratch:fluent-{token}:events"
    scratch_scene = f"urn:{conn.dataset}:scratch:fluent-{token}:scene"

    trial_ledger = ledger_insert.replace(f"<{events_graph}>", f"<{scratch_events}>")
    trial_scene = scene_update.replace(f"<{scene_graph}>", f"<{scratch_scene}>")

    try:
        await client.update(conn, f"COPY SILENT GRAPH <{events_graph}> TO <{scratch_events}>")
        await client.update(conn, f"COPY SILENT GRAPH <{scene_graph}> TO <{scratch_scene}>")
        await client.update(conn, f"{_PREFIXES}\n{trial_ledger}\n;\n{trial_scene}")

        scope_query = f"""{_PREFIXES}
CONSTRUCT {{ ?assertion ?p ?o . }}
WHERE {{
  GRAPH <{scratch_events}> {{
    ?assertion hevt:forFluent <{fluent}> ; ?p ?o .
    FILTER NOT EXISTS {{ ?assertion hevt:invalidatedAt ?anyInvalidation }}
    FILTER NOT EXISTS {{ ?later hevt:supersedes ?assertion }}
  }}
}}"""
        scoped_turtle = await client.construct(conn, scope_query)

        validate_scratch = f"urn:{conn.dataset}:scratch:fluent-validate-{token}"
        try:
            await client.post_graph(conn, validate_scratch, scoped_turtle)
            report_turtle = await client.shacl_validate(
                conn, target_graph=validate_scratch, shapes_turtle=shapes_turtle
            )
        finally:
            await client.drop_graph(conn, validate_scratch)

        return shacl_mod.parse_report(report_turtle)
    finally:
        await client.drop_graph(conn, scratch_events)
        await client.drop_graph(conn, scratch_scene)


async def _evaluate_state_triggers(client: FusekiClient, conn: Conn, *, fluent: str) -> None:
    """Fire every Active StateTrigger after a successful, confirmed
    transition. Never allowed to affect the transition itself -- see the
    module docstring's 2026-08-26 note."""
    try:
        from .triggers import TRIGGER_STATE, evaluate_triggers  # noqa: PLC0415

        await evaluate_triggers(client, conn, kind=TRIGGER_STATE)
    except Exception:  # noqa: BLE001 - a trigger failure must not break the write
        log.exception(
            "StateTrigger evaluation failed after updating <%s> (transition already committed)",
            fluent,
        )


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
    destructive scene-graph update, in a single SPARQL request -- validated
    on scratch copies first when the dataset has a shapes graph configured
    (see the module docstring's "Gating" section), so a rejected transition
    never reaches the live graphs at all.

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

    shapes_turtle = await client.get_graph(conn, conn.shapes_graph)
    gated = bool(shapes_turtle.strip())

    for _ in range(attempts):
        current, current_assertion = await _read_current(client, conn, fluent=fluent)

        # Minted before _plan(), not after -- _plan() needs this transition's
        # own assertion IRI to write holon:currentAssertion in the scalar
        # branch. Minting never depended on the plan, so this ordering
        # costs nothing.
        minted = await sequence.mint(
            client,
            conn,
            name="event",
            purpose=f"hevt:StateAssertion for <{fluent}>, operation {operation.value}",
            authorised_by=asserted_by,
        )
        assertion_iri = minted.iri

        new_value, guard_clause, scene_delete, scene_insert = _plan(
            mode, operation, current, value, fluent, is_iri, assertion_iri
        )

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

        # The guard pattern is scoped to scene_graph explicitly -- never
        # rely on a default-graph query implicitly seeing named-graph
        # content, since that only happens on datasets configured with
        # union-default-graph. See the module docstring.
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
  GRAPH <{scene_graph}> {{
    {guard_clause}
  }}
}}"""

        if gated:
            report = await _precommit_check(
                client,
                conn,
                fluent=fluent,
                events_graph=events_graph,
                scene_graph=scene_graph,
                ledger_insert=ledger_insert,
                scene_update=scene_update,
                shapes_turtle=shapes_turtle,
            )
            if report.blocking:
                raise FluentError(
                    f"<{fluent}> {operation.value} rejected by shapes graph "
                    f"<{conn.shapes_graph}> ({len(report.blocking)} violation(s)); "
                    "nothing live was touched -- checked on a scratch copy "
                    "before this transition ever reached events or scene",
                    report=report,
                )

        full_update = f"{_PREFIXES}\n{ledger_insert}\n;\n{scene_update}"
        await client.update(conn, full_update)

        confirmed_value, confirmed_assertion = await _read_current(
            client, conn, fluent=fluent
        )
        if confirmed_assertion != assertion_iri:
            # Lost the race on the live commit: another writer's transition
            # landed between our read and our write. Retry from a fresh
            # read, same as sequence.mint(). This is a live-commit
            # concurrency loss, distinct from a pre-commit shapes
            # rejection above -- the latter is never worth retrying, since
            # the same violation would just recur.
            continue

        await _evaluate_state_triggers(client, conn, fluent=fluent)

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


def _plan(
    mode: OperationMode,
    operation: Operation,
    current: TypedValue | None,
    value: Any,
    fluent: str,
    is_iri: bool,
    assertion_iri: str,
) -> tuple[TypedValue, str, str, str]:
    """Compute (new_value, WHERE guard, scene DELETE pattern, scene INSERT
    pattern) for one transition. All arithmetic happens here, in Python --
    not pushed into SPARQL, since SPARQL 1.1 has no trustworthy native
    date-duration arithmetic and list membership isn't a scalar operation
    regardless.

    ``assertion_iri`` is this transition's own newly-minted StateAssertion
    IRI -- the scalar branch writes it as holon:currentAssertion alongside
    holon:currentValue, which is what lets the *next* transition find its
    predecessor to supersede in O(1). Omitting this was a real, severe bug
    (see the module docstring) -- without it, current_assertion always
    reads back None, hevt:supersedes never gets asserted past the first
    transition, and every StateAssertionNonOverlapShape check on a gated
    dataset fails from the second transition onward.

    The guard returned here is a bare pattern -- no GRAPH wrapper. The
    caller (update_fluent) wraps it in GRAPH <scene_graph> { ... } once,
    since every branch below needs the same wrapping and only the caller
    knows the graph IRI. A FILTER NOT EXISTS/EXISTS pattern nested inside
    that outer GRAPH block inherits its graph scope correctly per SPARQL
    semantics, so this works uniformly across the Init, delta, and list
    branches without each needing to know about graph scoping itself.
    """

    if operation in (Operation.LIST_INSERT, Operation.LIST_REMOVE):
        # List-mode fluents have no single "current assertion" -- membership
        # is a set, not a scalar state, so there's nothing here for
        # holon:currentAssertion to point at.
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
            f"    OPTIONAL {{ <{fluent}> holon:currentAssertion ?existingAssertion . }}\n"
            f"    FILTER (?existingValue = {current.as_term()})"
        )
        # ?existingAssertion may be unbound (a fluent set before this fix
        # shipped, with no currentAssertion triple at all) -- DELETE simply
        # matches nothing for that pattern in that case, which is correct:
        # there is nothing to delete.
        scene_delete = (
            f"<{fluent}> holon:currentValue ?existingValue .\n"
            f"  <{fluent}> holon:currentAssertion ?existingAssertion ."
        )

    scene_insert = (
        f"<{fluent}> holon:currentValue {new_typed.as_term()} ;\n"
        f"             holon:currentAssertion <{assertion_iri}> ."
    )
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
