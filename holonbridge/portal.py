"""``hb:Portal``: a door between two holons.

The Holon Graph Architecture separates two things that are easy to run together:

* the **boundary** (SHACL shapes) says what may exist *inside* a holon;
* a **portal** says how something may move *between* holons: a link with a lock.

A portal may consult a boundary ("you may enter only if your description
satisfies these shapes"); a boundary says nothing about portals. This module is
the second half, and it keeps the direction: the optional boundary check is one
input to the lock, never the other way round.

What a portal is here
---------------------

A registered resource in ``urn:{dataset}:portals``::

    hb:Portal
      hb:id "customs"                    short name
      hb:from <urn:x:Calais>             the holon the traveller leaves
      hb:to   <urn:x:Dover>              the holon it arrives in
      hb:positionProperty <urn:x:locatedIn>
                                         the holon:FluentProperty whose fluent says
                                         where a traveller is
      hb:lock "has-passport"             optional: a named SELECT, bound to the
                                         traveller as ``focus``; the door is open
                                         when it returns at least one row
      hb:boundaryShapes <urn:x:shapes>   optional: a graph of SHACL shapes the
                                         traveller's payload must satisfy
      hb:effect "declare-goods"          optional: a named SELECT, bound to the
                                         traveller, whose rows (?fluent ?value ...)
                                         are further fluent updates made in the
                                         same crossing
      hb:portalStatus "Active"|"Suspended"

Crossing
--------

:func:`traverse` finds the traveller's position fluent (the fluent linked from
the traveller whose property is ``hb:positionProperty``), then, in order:

1. checks the portal is Active and the traveller is at ``hb:from``;
2. if the portal has boundary shapes, validates the supplied payload against
   them (a missing payload is refused);
3. runs the lock query; no rows means the door is shut, and a ``?reason``
   column, if the query has one, is returned to the caller;
4. applies, **as one transaction** (:mod:`holonbridge.fluent_transaction`), the
   position change (``Set`` to ``hb:to``, only if still at ``hb:from``) plus
   every row of the effect query.

Step 4 is why this exists only now: a crossing changes more than one fluent, and
a half-made crossing (moved, but the toll not taken) is exactly what a lock must
not allow. The transaction record carries ``hevt:viaPortal`` and
``hevt:traveller``.

Governing the position property
-------------------------------

If a portal is registered for a position property, **no other route may change
a fluent of that property**: ``POST /fluent/update``, ``POST /fluent/update-many``
and a trigger's fluent action all refuse, naming the portal. Without that rule a
portal would be advice; with it, the only way a traveller's position moves is a
crossing that passed the lock. Delete or never register the portal and the
property is as free as any other. It is a property of the model, not a setting.

What it does not do
-------------------

* The lock is evaluated, then the transaction commits: they are not one atomic
  step. The position change is compare-and-set against ``hb:from``, so a
  traveller cannot be moved twice or from the wrong place, but a lock whose
  answer changes in between is decided on the earlier answer.
* Direct writes to the scene graph (``sparql_update``, ``push_turtle``) are not
  gated by anything in this module, as before.
* A crossing is not undone by a failure of something *after* it (state triggers
  run afterwards and cannot affect it).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import shacl as shacl_mod
from .conn import Conn
from .fluent import HOLON_NS, FluentError, Operation, TypedValue, _read_current
from .fluent_transaction import FluentOp, TransactionResult, update_fluents
from .fluent_trigger_action import FluentActionInvalid, bind_action, parse_rows
from .fuseki import FusekiClient, FusekiError
from .named_queries import load_named_queries
from .turtle import literal

log = logging.getLogger("holonbridge.portal")

HB_NAMESPACE = "https://w3id.org/holonbridge/"
PORTAL_CLASS = "Portal"
PORTAL_STATUSES = ("Active", "Suspended")

_XSD_DATETIME = "<http://www.w3.org/2001/XMLSchema#dateTime>"
_SAFE_IRI = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>"{}|\\^`]*$')
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


class PortalError(Exception):
    """A portal could not be defined or crossed. ``code`` is machine readable
    (``locked``, ``not_at_origin``, ``boundary_refused`` ...); ``status`` the
    HTTP status a route should answer with."""

    def __init__(self, code: str, message: str, *, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


@dataclass(frozen=True)
class Portal:
    iri: str
    id: str
    from_holon: str
    to_holon: str
    position_property: str
    lock: str = ""
    effect: str = ""
    boundary_shapes: str = ""
    status: str = "Active"
    label: str = ""
    description: str = ""
    registered_by: str = ""

    def as_dict(self) -> dict:
        out: dict[str, Any] = {
            "iri": self.iri,
            "id": self.id,
            "from": self.from_holon,
            "to": self.to_holon,
            "positionProperty": self.position_property,
            "status": self.status,
        }
        for key, value in (
            ("lock", self.lock),
            ("effect", self.effect),
            ("boundaryShapes", self.boundary_shapes),
            ("label", self.label),
            ("description", self.description),
            ("registeredBy", self.registered_by),
        ):
            if value:
                out[key] = value
        return out


@dataclass
class PortalLoadResult:
    portals: list[Portal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_id(self, ident: str) -> Portal | None:
        return next((p for p in self.portals if p.id == ident), None)


@dataclass(frozen=True)
class Crossing:
    portal: Portal
    traveller: str
    transaction: TransactionResult
    effects_applied: int


# --- storage ------------------------------------------------------------------


def portal_node(conn: Conn, portal_id: str) -> str:
    return conn.scoped("portals", portal_id)


def validate_definition(
    *, portal_id: str, from_holon: str, to_holon: str, position_property: str,
    boundary_shapes: str | None, status: str,
) -> None:
    if not _ID.match(portal_id or ""):
        raise PortalError("invalid_portal", f"portal id {portal_id!r} must be letters, digits, '_', '.' or '-'")
    for name, value in (("from", from_holon), ("to", to_holon), ("positionProperty", position_property)):
        if not value or not _SAFE_IRI.match(value):
            raise PortalError("invalid_portal", f"{name} {value!r} is not a usable absolute IRI")
    if from_holon == to_holon:
        raise PortalError("invalid_portal", "a portal joins two different holons")
    if boundary_shapes and not _SAFE_IRI.match(boundary_shapes):
        raise PortalError("invalid_portal", f"boundaryShapes {boundary_shapes!r} is not a usable absolute IRI")
    if status not in PORTAL_STATUSES:
        raise PortalError("invalid_portal", f"status must be one of {', '.join(PORTAL_STATUSES)}")


def render_portal_turtle(
    *, node: str, portal_id: str, from_holon: str, to_holon: str, position_property: str,
    lock: str | None, effect: str | None, boundary_shapes: str | None, status: str,
    label: str | None, description: str | None, registered_by: str | None, registered_at: str,
) -> str:
    validate_definition(
        portal_id=portal_id, from_holon=from_holon, to_holon=to_holon,
        position_property=position_property, boundary_shapes=boundary_shapes, status=status,
    )
    hb = HB_NAMESPACE
    pairs = [
        f"a <{hb}{PORTAL_CLASS}>",
        f"<{hb}id> {literal(portal_id)}",
        f"<{hb}from> <{from_holon}>",
        f"<{hb}to> <{to_holon}>",
        f"<{hb}positionProperty> <{position_property}>",
        f"<{hb}portalStatus> {literal(status)}",
    ]
    if lock:
        pairs.append(f"<{hb}lock> {literal(lock)}")
    if effect:
        pairs.append(f"<{hb}effect> {literal(effect)}")
    if boundary_shapes:
        pairs.append(f"<{hb}boundaryShapes> <{boundary_shapes}>")
    if label:
        pairs.append(f"<{hb}label> {literal(label)}")
    if description:
        pairs.append(f"<{hb}description> {literal(description)}")
    if registered_by:
        if not _SAFE_IRI.match(registered_by):
            raise PortalError("invalid_portal", f"registered_by {registered_by!r} is not a usable absolute IRI")
        pairs.append(f"<{hb}registeredBy> <{registered_by}>")
    pairs.append(f"<{hb}registeredAt> {literal(registered_at, datatype=_XSD_DATETIME)}")
    return f"<{node}>\n    " + " ;\n    ".join(pairs) + " ."


def registration_update(registry: str, node: str, turtle: str) -> str:
    return f"""DELETE WHERE {{ GRAPH <{registry}> {{ <{node}> ?p ?o }} }} ;
INSERT DATA {{ GRAPH <{registry}> {{
{turtle}
}} }}"""


def deletion_update(registry: str, node: str) -> str:
    return f"DELETE WHERE {{ GRAPH <{registry}> {{ <{node}> ?p ?o }} }}"


def status_update(registry: str, node: str, status: str) -> str:
    if status not in PORTAL_STATUSES:
        raise PortalError("invalid_portal", f"status must be one of {', '.join(PORTAL_STATUSES)}")
    return f"""DELETE WHERE {{ GRAPH <{registry}> {{ <{node}> <{HB_NAMESPACE}portalStatus> ?o }} }} ;
INSERT DATA {{ GRAPH <{registry}> {{ <{node}> <{HB_NAMESPACE}portalStatus> {literal(status)} }} }}"""


async def load_portals(
    client: FusekiClient, conn: Conn, *, graph: str | None = None
) -> PortalLoadResult:
    """Every registered portal. An unreachable registry degrades to an empty
    result with a warning, like the other registries."""
    registry = graph or conn.graph("portals")
    result = PortalLoadResult()
    query = f"""SELECT ?s ?p ?o WHERE {{
  GRAPH <{registry}> {{
    ?s a <{HB_NAMESPACE}{PORTAL_CLASS}> .
    ?s ?p ?o .
  }}
}}"""
    try:
        rows = (await client.select(conn, query))["results"]["bindings"]
    except (FusekiError, KeyError) as exc:
        message = f"portal load from <{registry}> failed: {exc}"
        log.warning(message)
        result.warnings.append(message)
        return result

    by_node: dict[str, dict[str, str]] = {}
    for row in rows:
        node = row["s"]["value"]
        predicate = row["p"]["value"]
        if not predicate.startswith(HB_NAMESPACE):
            continue
        by_node.setdefault(node, {})[predicate[len(HB_NAMESPACE):]] = row["o"]["value"]

    for node, props in sorted(by_node.items()):
        missing = [k for k in ("id", "from", "to", "positionProperty") if not props.get(k)]
        if missing:
            result.warnings.append(f"<{node}> is missing {', '.join(missing)}; skipped")
            continue
        result.portals.append(
            Portal(
                iri=node,
                id=props["id"],
                from_holon=props["from"],
                to_holon=props["to"],
                position_property=props["positionProperty"],
                lock=props.get("lock", ""),
                effect=props.get("effect", ""),
                boundary_shapes=props.get("boundaryShapes", ""),
                status=props.get("portalStatus", "Active"),
                label=props.get("label", ""),
                description=props.get("description", ""),
                registered_by=props.get("registeredBy", ""),
            )
        )
    return result


# --- governing a property -------------------------------------------------------


async def governing_portals(client: FusekiClient, conn: Conn, fluent: str) -> list[str]:
    """Ids of the portals that govern ``fluent``, i.e. whose position property is
    the fluent's. Non-empty means only a crossing may change it."""
    query = f"""PREFIX holon: <{HOLON_NS}>
SELECT ?portalId WHERE {{
  GRAPH <{conn.holons_graph}> {{ <{fluent}> holon:fluentProperty ?prop . }}
  GRAPH <{conn.graph("portals")}> {{
    ?portal a <{HB_NAMESPACE}{PORTAL_CLASS}> ;
            <{HB_NAMESPACE}positionProperty> ?prop ;
            <{HB_NAMESPACE}id> ?portalId .
  }}
}}"""
    # Not caught: failing open here would make a fluent writable exactly when the
    # portals graph cannot be read.
    bindings = (await client.select(conn, query)).get("results", {}).get("bindings", [])
    return sorted({b["portalId"]["value"] for b in bindings if "portalId" in b})


def portal_governed_message(fluent: str, portals: list[str]) -> str:
    return (
        f"<{fluent}> is the position of a traveller governed by portal(s) "
        f"{', '.join(portals)}; it can only change by crossing one (POST /portal/<id>/traverse)"
    )


# --- crossing -------------------------------------------------------------------


async def _position_fluent(
    client: FusekiClient, conn: Conn, *, traveller: str, prop: str
) -> str:
    query = f"""PREFIX holon: <{HOLON_NS}>
SELECT DISTINCT ?fluent WHERE {{
  GRAPH <{conn.holons_graph}> {{
    <{traveller}> ?link ?fluent .
    ?fluent holon:fluentProperty <{prop}> .
  }}
}}"""
    bindings = (await client.select(conn, query)).get("results", {}).get("bindings", [])
    fluents = sorted({b["fluent"]["value"] for b in bindings})
    if not fluents:
        raise PortalError(
            "traveller_has_no_position",
            f"<{traveller}> links to no fluent whose property is <{prop}>",
            status=400,
        )
    if len(fluents) > 1:
        raise PortalError(
            "traveller_position_ambiguous",
            f"<{traveller}> links to {len(fluents)} fluents of property <{prop}>: "
            f"{', '.join(fluents)}",
            status=400,
        )
    return fluents[0]


async def _rows(client: FusekiClient, conn: Conn, portal: Portal, query_id: str, traveller: str, *, label: str):
    queries = await load_named_queries(client, conn)
    named = queries.by_id(query_id)
    if named is None:
        raise PortalError(
            f"unknown_{label}", f"portal {portal.id!r} names {label} query {query_id!r}, which is not registered",
            status=409,
        )
    try:
        sparql = bind_action(named, traveller)
    except FluentActionInvalid as exc:
        raise PortalError(f"{label}_not_bindable", str(exc), status=409) from exc
    return (await client.select(conn, sparql)).get("results", {}).get("bindings", [])


async def traverse(
    client: FusekiClient,
    conn: Conn,
    portal: Portal,
    *,
    traveller: str,
    asserted_by: str,
    payload_turtle: str | None = None,
    asserted_datetime: datetime | None = None,
    description: str | None = None,
    gate_scope: str = "assertion",
) -> Crossing:
    """Cross ``portal`` as ``traveller``. See the module docs for the order of checks."""
    if portal.status != "Active":
        raise PortalError("portal_suspended", f"portal {portal.id!r} is {portal.status}", status=409)

    position = await _position_fluent(client, conn, traveller=traveller, prop=portal.position_property)

    current, _ = await _read_current(client, conn, fluent=position)
    if current is None or current.kind != "uri" or current.lexical != portal.from_holon:
        held = "nowhere" if current is None else current.lexical
        raise PortalError(
            "not_at_origin",
            f"<{traveller}> is at {held}, not <{portal.from_holon}>, so it cannot use portal {portal.id!r}",
            status=409,
            extra={"position": position, "at": None if current is None else current.lexical},
        )

    if portal.boundary_shapes:
        if not (payload_turtle or "").strip():
            raise PortalError(
                "boundary_needs_payload",
                f"portal {portal.id!r} checks a payload against <{portal.boundary_shapes}>; "
                "send the traveller's description as Turtle in 'payload'",
            )
        try:
            report = await shacl_mod.validate_full(
                client, conn, turtle=payload_turtle, shapes_graph=portal.boundary_shapes
            )
        except ValueError as exc:  # empty/absent shapes, or shapes the gate cannot enforce
            raise PortalError("boundary_shapes_unusable", str(exc), status=409) from exc
        except FusekiError as exc:  # an unparseable payload is a 400 from the store
            raise PortalError("boundary_check_failed", f"could not check the payload: {exc}") from exc
        if report.blocking:
            raise PortalError(
                "boundary_refused",
                f"the payload does not satisfy <{portal.boundary_shapes}> "
                f"({len(report.blocking)} violation(s))",
                status=403,
                extra={"violations": [_violation(v) for v in report.blocking][:20]},
            )

    if portal.lock:
        rows = await _rows(client, conn, portal, portal.lock, traveller, label="lock")
        if not rows:
            raise PortalError("locked", f"portal {portal.id!r} is locked for <{traveller}>", status=403)
        # A lock that answers with ?open (or ?allowed) false is shut too, so a lock
        # can be written as a single row of reasons rather than as presence/absence.
        first = rows[0]
        for flag in ("open", "allowed"):
            if flag in first and first[flag].get("value", "").lower() in ("false", "0"):
                reason = first.get("reason", {}).get("value", "")
                raise PortalError(
                    "locked",
                    f"portal {portal.id!r} is locked for <{traveller}>" + (f": {reason}" if reason else ""),
                    status=403,
                    extra={"reason": reason} if reason else {},
                )

    ops = [
        FluentOp(
            fluent=position,
            operation=Operation.SET,
            value=portal.to_holon,
            is_iri=True,
            description=f"crossed portal {portal.id}",
            expect=TypedValue(kind="uri", lexical=portal.from_holon),
        )
    ]
    effects = 0
    if portal.effect:
        rows = await _rows(client, conn, portal, portal.effect, traveller, label="effect")
        try:
            parsed = parse_rows(rows)
        except FluentActionInvalid as exc:
            raise PortalError("effect_invalid", str(exc), status=409) from exc
        for row in parsed:
            ops.append(
                FluentOp(
                    fluent=row.fluent,
                    operation=row.operation,
                    value=row.value,
                    is_iri=row.is_iri,
                    description=row.description or f"portal {portal.id}",
                )
            )
        effects = len(parsed)

    try:
        transaction = await update_fluents(
            client,
            conn,
            ops=ops,
            asserted_datetime=asserted_datetime,
            asserted_by=asserted_by,
            description=description or f"crossed portal {portal.id}",
            via_portal=portal.iri,
            traveller=traveller,
            gate_scope=gate_scope,
        )
    except FluentError as exc:
        text = str(exc)
        code = "not_at_origin" if "not the expected" in text else "crossing_refused"
        raise PortalError(
            code, text, status=409 if code == "not_at_origin" else 400,
            extra=(
                {"violations": [_violation(v) for v in exc.report.blocking][:20]}
                if exc.report is not None
                else {}
            ),
        ) from exc

    return Crossing(portal=portal, traveller=traveller, transaction=transaction, effects_applied=effects)


def _violation(result: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in result.items() if k in ("focusNode", "resultMessage", "resultPath", "value")}
