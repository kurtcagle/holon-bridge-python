"""Holon lifecycle: Active, Inactive, Tombstoned.

A holon is either active or inactive. An inactive holon can still be read and
queried, but nothing can change its fluents, and it can be made active again.
A tombstoned holon is inactive and also left out of discovery (the
neighbourhood lists in ``get_holon``); it can still be read directly by IRI.
All transitions are reversible.

Why a fluent
------------

The state is a fluent on the holon, not a flag, so the ledger gives its history
and ``get_holon(observed_at=...)`` reads it as of a past time with no new
machinery::

    <holon>     hb:lifecycleStatus <fluent> .                  (holons graph)
    <fluent>    a holon:Fluent ;
                holon:fluentProperty hb:lifecycleStatus .      (holons graph)
    hb:lifecycleStatus a holon:FluentProperty ;
                holon:fluentOperationMode holon:SetValueMode . (ontology graph)

The link predicate is the fluent property itself, which is the convention the
other fluents follow. A holon with no lifecycle fluent is Active, so nothing
needs migrating: the fluent is created on a holon's first change of state.

The terms are in the ``hb:`` namespace because the HCG has not ratified a
lifecycle vocabulary. If it does, the values can be aliased.

What it governs
---------------

* **Writes.** A fluent whose holder is Inactive or Tombstoned refuses every
  change that goes through ``POST /fluent/update``, ``POST /fluent/update-many``,
  a trigger's fluent action, or a portal crossing (including the crossing's
  effect rows). :func:`write_block` is the one check all four call.
* **The lifecycle fluent itself** is governed in the way a portal governs a
  position property: only ``POST /holon/lifecycle`` may change it. Otherwise
  the guard above could be undone with an ordinary fluent update.
* **Reads.** ``get_holon`` reports the state. Neighbour lists leave out
  Tombstoned holons unless asked (:func:`tombstoned_filter`).

What it does not govern
-----------------------

This is a fluent-level freeze, not a seal on the holon's graph. ``/ingest``,
``POST /holon``, named-rule writes and raw SPARQL Update can still change an
inactive holon's structural triples (and, through SPARQL Update, even its scene
triples). Freezing those paths is a separate piece of work, and SPARQL Update
in particular cannot be guaranteed: keep it an access-control matter.

Nor are arbitrary SPARQL queries filtered: a query that lists holons will list
tombstoned ones unless it uses :func:`tombstoned_filter`'s pattern.

Not here yet: succession (closing one holon and seeding a successor from its
final state), and a trigger action that changes a holon's state. A holon that
has a successor will have to refuse reactivation; there is no successor link
to check yet.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .conn import Conn
from .fluent import (
    HEVT_NS,
    HOLON_NS,
    FluentError,
    Operation,
    update_fluent,
)
from .fuseki import FusekiClient

HB = "https://w3id.org/holonbridge/"

LIFECYCLE_PROPERTY = f"{HB}lifecycleStatus"
ACTIVE = f"{HB}Active"
INACTIVE = f"{HB}Inactive"
TOMBSTONED = f"{HB}Tombstoned"

#: Short names, in the order a holon is usually taken through them.
STATES: dict[str, str] = {"Active": ACTIVE, "Inactive": INACTIVE, "Tombstoned": TOMBSTONED}

#: States in which a holon's fluents refuse change.
BLOCKING_STATES = (INACTIVE, TOMBSTONED)

_PREFIXES = f"""PREFIX holon: <{HOLON_NS}>
PREFIX hevt:  <{HEVT_NS}>
PREFIX hb:    <{HB}>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
"""


class LifecycleError(RuntimeError):
    """A lifecycle change was refused. ``code`` is machine-readable; ``status``
    is the HTTP status a route should answer with."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def state_name(iri: str | None) -> str:
    """``Active`` for no value; otherwise the short name, or the IRI itself if it is
    not one of the three (so an unexpected value shows up rather than hiding)."""
    if iri is None:
        return "Active"
    for name, value in STATES.items():
        if iri == value:
            return name
    return iri


def lifecycle_fluent_iri(conn: Conn, holon: str) -> str:
    """The IRI the lifecycle fluent of ``holon`` gets: stable, so creating it twice is
    harmless, and independent of how the holon's own IRI is shaped."""
    digest = hashlib.sha256(holon.encode("utf-8")).hexdigest()[:20]
    return f"{conn.graph('meta')}:lifecycle:{digest}"


# --- reading ----------------------------------------------------------------------


@dataclass(frozen=True)
class LifecycleState:
    holon: str
    state: str  # Active | Inactive | Tombstoned
    fluent: str | None  # None until the holon's first change of state
    assertion: str | None
    since: str | None  # valid time of the current assertion, if there is one

    def as_dict(self) -> dict:
        return {
            "holon": self.holon,
            "state": self.state,
            "fluent": self.fluent,
            "assertion": self.assertion,
            "since": self.since,
        }


async def read_state(client: FusekiClient, conn: Conn, holon: str) -> LifecycleState:
    query = f"""{_PREFIXES}
SELECT ?fluent ?value ?assertion ?at WHERE {{
  GRAPH <{conn.holons_graph}> {{ <{holon}> hb:lifecycleStatus ?fluent . }}
  OPTIONAL {{
    GRAPH <{conn.scene_graph}> {{
      ?fluent holon:currentValue ?value .
      OPTIONAL {{ ?fluent holon:currentAssertion ?assertion . }}
    }}
    OPTIONAL {{ GRAPH <{conn.graph("events")}> {{ ?assertion hevt:assertedDateTime ?at . }} }}
  }}
}} LIMIT 1"""
    rows = (await client.select(conn, query)).get("results", {}).get("bindings", [])
    if not rows:
        return LifecycleState(holon, "Active", None, None, None)
    row = rows[0]
    value = row.get("value", {}).get("value")
    return LifecycleState(
        holon=holon,
        state=state_name(value),
        fluent=row["fluent"]["value"],
        assertion=row.get("assertion", {}).get("value"),
        since=row.get("at", {}).get("value"),
    )


async def holon_exists(client: FusekiClient, conn: Conn, holon: str) -> bool:
    query = f"ASK {{ GRAPH <{conn.holons_graph}> {{ <{holon}> ?p ?o . }} }}"
    return bool((await client.select(conn, query)).get("boolean"))


async def vocabulary_declared(client: FusekiClient, conn: Conn) -> bool:
    query = f"""{_PREFIXES}
ASK {{ GRAPH <{conn.ontology_graph}> {{ hb:lifecycleStatus holon:fluentOperationMode ?mode . }} }}"""
    return bool((await client.select(conn, query)).get("boolean"))


# --- changing ---------------------------------------------------------------------


def _declare_vocabulary_update(conn: Conn) -> str:
    return f"""{_PREFIXES}
INSERT DATA {{ GRAPH <{conn.ontology_graph}> {{
  hb:lifecycleStatus a holon:FluentProperty ;
    holon:fluentOperationMode holon:SetValueMode ;
    rdfs:label "Lifecycle status"@en ;
    rdfs:comment "Whether a holon is Active, Inactive or Tombstoned. Absent means Active."@en .
}} }}"""


def _create_fluent_update(conn: Conn, holon: str, fluent: str) -> str:
    return f"""{_PREFIXES}
INSERT DATA {{ GRAPH <{conn.holons_graph}> {{
  <{holon}> hb:lifecycleStatus <{fluent}> .
  <{fluent}> a holon:Fluent ;
    holon:fluentProperty hb:lifecycleStatus .
}} }}"""


@dataclass(frozen=True)
class LifecycleChange:
    holon: str
    fluent: str | None
    previous: str  # short name
    state: str  # short name
    changed: bool
    assertion: str | None

    def as_dict(self) -> dict:
        return {
            "ok": True,
            "holon": self.holon,
            "fluent": self.fluent,
            "previousState": self.previous,
            "state": self.state,
            "changed": self.changed,
            "assertion": self.assertion,
        }


async def set_state(
    client: FusekiClient,
    conn: Conn,
    *,
    holon: str,
    state: str,
    asserted_by: str | None,
    reason: str | None = None,
    asserted_datetime: datetime | None = None,
    gate_scope: str = "assertion",
) -> LifecycleChange:
    """Move ``holon`` to ``state`` (``Active``, ``Inactive`` or ``Tombstoned``).

    Any state may follow any other. Asking for the state a holon is already in
    changes nothing and writes no ledger entry (``changed`` is False). The change
    itself is an ordinary fluent transition, so it is gated, ledgered and
    attributed like any other, and state triggers see it.

    The caller is responsible for authorisation: this writes the ledger, the
    scene, the sequences and the holons graph, and the ontology graph the first
    time a dataset uses lifecycle (:func:`vocabulary_declared`).
    """
    target = STATES.get(state)
    if target is None:
        raise LifecycleError(
            "bad_state", f"state must be one of {', '.join(STATES)}, not {state!r}"
        )
    if not await holon_exists(client, conn, holon):
        raise LifecycleError(
            "unknown_holon", f"<{holon}> is not a holon in this dataset", status=404
        )

    current = await read_state(client, conn, holon)
    if current.state == state:
        return LifecycleChange(holon, current.fluent, state, state, False, current.assertion)

    if not await vocabulary_declared(client, conn):
        await client.update(conn, _declare_vocabulary_update(conn))

    fluent = current.fluent or lifecycle_fluent_iri(conn, holon)
    if current.fluent is None:
        await client.update(conn, _create_fluent_update(conn, holon, fluent))

    note = f"lifecycle: {state}" + (f" ({reason})" if reason else "")
    try:
        result = await update_fluent(
            client,
            conn,
            fluent=fluent,
            operation=Operation.SET,
            value=target,
            is_iri=True,
            asserted_datetime=asserted_datetime,
            asserted_by=asserted_by,
            description=note,
            gate_scope=gate_scope,
        )
    except FluentError as exc:
        raise LifecycleError("fluent_error", str(exc)) from exc

    return LifecycleChange(holon, fluent, current.state, state, True, result.assertion_iri)


# --- the guard --------------------------------------------------------------------


@dataclass(frozen=True)
class WriteBlock:
    """Why a fluent cannot be changed. ``kind`` is ``governed`` (it is a lifecycle
    fluent) or ``inactive`` (its holder is Inactive or Tombstoned)."""

    kind: str
    fluent: str
    holders: tuple[str, ...] = ()
    state: str | None = None

    @property
    def error(self) -> str:
        return "lifecycle_governed" if self.kind == "governed" else "holon_inactive"

    @property
    def message(self) -> str:
        if self.kind == "governed":
            return (
                f"<{self.fluent}> is a holon's lifecycle state; it changes only through "
                "POST /holon/lifecycle"
            )
        who = ", ".join(f"<{h}>" for h in self.holders)
        return (
            f"<{self.fluent}> belongs to {who}, which is {self.state}: an inactive holon can "
            "be read but not changed. Make it Active first (POST /holon/lifecycle)"
        )

    def detail(self) -> dict:
        out: dict = {"error": self.error, "message": self.message, "fluent": self.fluent}
        if self.holders:
            out["holders"] = list(self.holders)
        if self.state:
            out["state"] = self.state
        return out


async def write_block(client: FusekiClient, conn: Conn, fluent: str) -> WriteBlock | None:
    """``None`` if ``fluent`` may be changed, otherwise why not.

    Not caught: failing open here would make an inactive holon writable exactly
    when the graphs cannot be read, the same stance as ``governing_portals``.
    """
    query = f"""{_PREFIXES}
SELECT ?kind ?holder ?state WHERE {{
  {{
    GRAPH <{conn.holons_graph}> {{ <{fluent}> holon:fluentProperty hb:lifecycleStatus . }}
    BIND("governed" AS ?kind)
  }} UNION {{
    GRAPH <{conn.holons_graph}> {{
      ?holder ?link <{fluent}> .
      ?holder hb:lifecycleStatus ?lifecycle .
    }}
    GRAPH <{conn.scene_graph}> {{ ?lifecycle holon:currentValue ?state . }}
    FILTER (?state IN (hb:Inactive, hb:Tombstoned))
    BIND("inactive" AS ?kind)
  }}
}}"""
    rows = (await client.select(conn, query)).get("results", {}).get("bindings", [])
    if not rows:
        return None
    if any(r["kind"]["value"] == "governed" for r in rows):
        return WriteBlock("governed", fluent)
    holders = tuple(sorted({r["holder"]["value"] for r in rows if "holder" in r}))
    # If one fluent has several holders in different blocking states, report the
    # stricter one.
    states = {state_name(r["state"]["value"]) for r in rows if "state" in r}
    state = "Tombstoned" if "Tombstoned" in states else "Inactive"
    return WriteBlock("inactive", fluent, holders, state)


def tombstoned_filter(conn: Conn, var: str = "?neighbour") -> str:
    """A ``FILTER NOT EXISTS`` that drops ``var`` if it is a Tombstoned holon.

    Reads the holons and scene graphs directly, so it follows ground truth, not
    a persona's private overlay. Add it to any query that discovers holons.
    """
    return f"""FILTER NOT EXISTS {{
    GRAPH <{conn.holons_graph}> {{ {var} <{LIFECYCLE_PROPERTY}> ?lifecycleFluent . }}
    GRAPH <{conn.scene_graph}> {{ ?lifecycleFluent <{HOLON_NS}currentValue> <{TOMBSTONED}> . }}
  }}"""
