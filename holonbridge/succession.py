"""Succession: close a holon and open a successor that carries its final state forward.

``succeed(A, B)`` makes A inactive and creates B, so that:

* B is linked to A (``A hb:succeededBy B``, ``B hb:predecessor A``);
* B has A's outgoing structural triples in the holons graph: its type, label,
  containment, the entities it references. B is in the same places A was;
* B has its own copy of each of A's fluents, seeded with A's **final** value, so the
  state carries over without the two holons sharing a fluent;
* A's lifecycle state is Inactive (or Tombstoned, if asked), so it stays readable and
  queryable and its fluents no longer change.

Order of work, and why
----------------------

1. **Plan** (A may still be Active): read A's fluents and values, check each can be
   carried over exactly and that the whole seed fits one transaction.
2. **Freeze A.** Make it Inactive first. From then on A's fluents cannot change
   (``lifecycle.write_block``), so the values read next are final: there is no window in
   which a concurrent writer can give B a stale value.
3. **Read** A's final values again (now stable).
4. **Create B's structure** in the holons graph, in one SPARQL update.
5. **Seed B's fluents** in one ``update_fluents`` transaction: all of them or none.
6. **Record provenance** (``prov:wasDerivedFrom``, best effort).

If step 4 or 5 fails, B's structure is removed, the link is removed and A goes back to
the state it was in; nothing is left in the ledger or the scene, because step 5 is
atomic and writes nothing when it fails. A crash between steps leaves A inactive with a
half-made B, which a re-run or a lifecycle call can repair.

Limits of this version
----------------------

* **One to one.** One predecessor, one successor. Split and merge are not built.
* **At most 20 values** (:data:`fluent_transaction.MAX_OPS`) are carried over in one go,
  counting each list member as one. A holon with more is refused rather than carried
  over partly.
* **Only values that can be reproduced exactly** are carried: IRIs, strings, booleans,
  integers, decimals, dates and date-times. Anything else is refused by name.
* **Triples with a blank-node object are not copied** from A to B (they cannot be
  copied by identity); their number is reported.
* **Incoming links are not moved.** What points at A (children, other holons) still
  points at A. Containment *upward* (A's own ``isPartOf`` and friends) is copied.
* **Boundaries** are registered per dataset, not per holon, so there is nothing to copy;
  B is under the same ones.
* **Avatars** do not exist in the bridge.
* One dataset only. B is not SHACL-checked at creation beyond the fluent gate on its
  fluents: its structure is A's, which was already in the dataset.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .conn import Conn
from .fluent import (
    FluentError,
    Operation,
    OperationMode,
    TypedValue,
    _lexical_form,
    _xsd_datatype,
)
from .fluent_transaction import MAX_OPS, FluentOp, update_fluents
from .fuseki import FusekiClient, FusekiError
from .lifecycle import (
    _PREFIXES,
    HB,
    LIFECYCLE_PROPERTY,
    LifecycleError,
    holon_exists,
    read_state,
    read_succession,
    set_state,
)
from .sequence import SequenceError
from .turtle import escape_literal

log = logging.getLogger(__name__)

PROV = "http://www.w3.org/ns/prov#"
SUCCEEDED_BY = f"{HB}succeededBy"
PREDECESSOR = f"{HB}predecessor"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

__all__ = ["Seeded", "Succession", "resolve_successor", "succeed"]

_XSD = "http://www.w3.org/2001/XMLSchema#"
_INTEGERS = {
    f"{_XSD}{n}"
    for n in (
        "integer", "int", "long", "short", "byte", "nonNegativeInteger", "positiveInteger",
        "negativeInteger", "nonPositiveInteger", "unsignedInt", "unsignedLong", "unsignedShort",
        "unsignedByte",
    )
}  # fmt: skip


@dataclass(frozen=True)
class Seeded:
    """One of A's fluents, as carried to B."""

    link: str  # the predicate from the holder to the fluent
    source: str  # A's fluent
    target: str  # B's fluent
    prop: str
    mode: str | None
    values: tuple[TypedValue, ...]
    assertion: str | None  # A's current assertion, for provenance (scalar fluents)


@dataclass
class Succession:
    """What a succession did."""

    holon: str
    successor: str
    previous_state: str
    state: str
    copied_triples: int
    skipped_triples: int
    fluents: list[dict] = field(default_factory=list)
    provenance_recorded: bool = True

    def as_dict(self) -> dict:
        return {
            "ok": True,
            "holon": self.holon,
            "successor": self.successor,
            "previousState": self.previous_state,
            "state": self.state,
            "copiedTriples": self.copied_triples,
            "skippedBlankNodeTriples": self.skipped_triples,
            "fluents": self.fluents,
            "provenanceRecorded": self.provenance_recorded,
        }


def _bad(code: str, message: str, status: int = 400) -> LifecycleError:
    return LifecycleError(code, message, status=status)


def _fluent_iri(conn: Conn, successor: str, source: str) -> str:
    digest = hashlib.sha256(f"{successor}|{source}".encode()).hexdigest()[:20]
    return f"{conn.graph('meta')}:succession:{digest}"


# --- reading A -------------------------------------------------------------------------


async def _fluents_of(client: FusekiClient, conn: Conn, holon: str) -> list[dict]:
    query = f"""{_PREFIXES}
SELECT ?link ?fluent ?prop ?mode WHERE {{
  GRAPH <{conn.holons_graph}> {{
    <{holon}> ?link ?fluent .
    ?fluent holon:fluentProperty ?prop .
  }}
  OPTIONAL {{ GRAPH <{conn.ontology_graph}> {{ ?prop holon:fluentOperationMode ?mode . }} }}
  FILTER (?prop != hb:lifecycleStatus)
}} ORDER BY ?fluent"""
    rows = (await client.select(conn, query)).get("results", {}).get("bindings", [])
    return [
        {
            "link": r["link"]["value"],
            "fluent": r["fluent"]["value"],
            "prop": r["prop"]["value"],
            "mode": r["mode"]["value"].rsplit("/", 1)[-1] if "mode" in r else None,
        }
        for r in rows
    ]


async def _values_of(client: FusekiClient, conn: Conn, fluent: str):
    """``(scalar values, list members, current assertion)`` of one fluent, from the scene."""
    query = f"""{_PREFIXES}
SELECT ?value ?member ?assertion WHERE {{
  GRAPH <{conn.scene_graph}> {{
    {{ <{fluent}> holon:currentValue ?value . OPTIONAL {{ <{fluent}> holon:currentAssertion ?assertion . }} }}
    UNION {{ <{fluent}> holon:hasListItem ?member . }}
  }}
}}"""
    rows = (await client.select(conn, query)).get("results", {}).get("bindings", [])
    scalars = [TypedValue.from_binding(r["value"]) for r in rows if "value" in r]
    members = [TypedValue.from_binding(r["member"]) for r in rows if "member" in r]
    assertion = next((r["assertion"]["value"] for r in rows if "assertion" in r), None)
    return scalars, members, assertion


async def _plan(client: FusekiClient, conn: Conn, holon: str, successor: str) -> list[Seeded]:
    seeded: list[Seeded] = []
    for fl in await _fluents_of(client, conn, holon):
        scalars, members, assertion = await _values_of(client, conn, fl["fluent"])
        if len(scalars) > 1:
            raise _bad(
                "cannot_carry",
                f"<{fl['fluent']}> has {len(scalars)} current values; a scalar fluent has one",
                409,
            )
        seeded.append(
            Seeded(
                link=fl["link"],
                source=fl["fluent"],
                target=_fluent_iri(conn, successor, fl["fluent"]),
                prop=fl["prop"],
                mode=fl["mode"],
                values=tuple(scalars) or tuple(members),
                assertion=assertion,
            )
        )
    return seeded


def _python_value(value: TypedValue, fluent: str) -> tuple[Any, bool]:
    """``(python value, is_iri)`` that :func:`update_fluents` will turn back into exactly
    ``value``, or a refusal naming the fluent."""
    if value.kind == "uri":
        return value.lexical, True
    text, datatype = value.lexical, value.datatype
    try:
        if datatype in _INTEGERS:
            carried: Any = int(text)
        elif datatype == f"{_XSD}decimal":
            carried = Decimal(text)
        elif datatype == f"{_XSD}boolean":
            carried = {"true": True, "1": True, "false": False, "0": False}[text.strip().lower()]
        elif datatype == f"{_XSD}dateTime":
            carried = datetime.fromisoformat(text.replace("Z", "+00:00"))
        elif datatype == f"{_XSD}date":
            carried = date.fromisoformat(text)
        elif datatype in (None, f"{_XSD}string"):
            carried = text
        else:
            raise KeyError(datatype)
    except (ValueError, KeyError, InvalidOperation) as exc:
        raise _bad(
            "cannot_carry",
            f"<{fluent}> holds {text!r} with datatype {datatype or 'none'}, which cannot be "
            "carried over exactly",
            409,
        ) from exc
    # What the write will store must be what A holds, lexical form and datatype both.
    again = TypedValue("literal", _lexical_form(carried), _xsd_datatype(carried))
    if again.lexical != text or (again.datatype or None) != (
        None if datatype == f"{_XSD}string" else datatype
    ):
        raise _bad(
            "cannot_carry",
            f"<{fluent}> holds {text!r} ({datatype or 'plain'}); carrying it over would store "
            f"{again.lexical!r} ({again.datatype or 'plain'}), so it is refused",
            409,
        )
    return carried, False


def _ops(seeded: list[Seeded], holon: str) -> list[FluentOp]:
    ops: list[FluentOp] = []
    note = f"succession from <{holon}>"
    for item in seeded:
        if not item.values:
            continue
        listy = item.mode == OperationMode.LIST.value
        for value in item.values:
            python, is_iri = _python_value(value, item.source)
            ops.append(
                FluentOp(
                    fluent=item.target,
                    operation=Operation.LIST_INSERT if listy else Operation.SET,
                    value=python,
                    is_iri=is_iri,
                    description=note,
                )
            )
    return ops


def _rounds(ops: list[FluentOp]) -> list[list[FluentOp]]:
    """Split ``ops`` into batches that touch each fluent at most once, in order."""
    rounds: list[list[FluentOp]] = []
    taken: dict[str, int] = {}
    for op in ops:
        index = taken.get(op.fluent, 0)
        taken[op.fluent] = index + 1
        if index == len(rounds):
            rounds.append([])
        rounds[index].append(op)
    return rounds


# --- writing B -------------------------------------------------------------------------


def _structure_update(
    conn: Conn, holon: str, successor: str, seeded: list[Seeded], label: str | None
) -> str:
    """One update that creates B: A's outgoing triples (minus fluents and lifecycle), B's
    own fluent nodes and links, and the succession links."""
    g = conn.holons_graph
    fluent_sources = ", ".join(f"<{s.source}>" for s in seeded) or "<urn:none>"
    skip = ", ".join(f"<{p}>" for p in _skip_predicates(label))

    parts = [
        f"""INSERT {{ GRAPH <{g}> {{ <{successor}> ?p ?o . }} }}
WHERE {{ GRAPH <{g}> {{ <{holon}> ?p ?o .
  FILTER (!isBlank(?o) && ?p NOT IN ({skip}) && ?o NOT IN ({fluent_sources})) }} }}"""
    ]
    for item in seeded:
        parts.append(
            f"""INSERT {{ GRAPH <{g}> {{ <{successor}> <{item.link}> <{item.target}> .
    <{item.target}> ?p ?o . }} }}
WHERE {{ GRAPH <{g}> {{ <{item.source}> ?p ?o . FILTER (!isBlank(?o)) }} }}"""
        )
    tail = [
        f"<{holon}> <{SUCCEEDED_BY}> <{successor}> .",
        f"<{successor}> <{PREDECESSOR}> <{holon}> .",
    ]
    if label is not None:
        tail.append(f'<{successor}> <{RDFS_LABEL}> "{escape_literal(label)}" .')
    parts.append("INSERT DATA { GRAPH <" + g + "> { " + " ".join(tail) + " } }")
    return ";\n".join(parts)


def _undo_update(conn: Conn, holon: str, successor: str, seeded: list[Seeded]) -> str:
    g = conn.holons_graph
    nodes = [successor] + [s.target for s in seeded]
    deletes = [f"DELETE WHERE {{ GRAPH <{g}> {{ <{n}> ?p ?o . }} }}" for n in nodes]
    # Values a first round may already have written to B's new fluents. Their ledger
    # entries stay: they are the record of an attempt that was rolled back.
    deletes += [
        f"DELETE WHERE {{ GRAPH <{conn.scene_graph}> {{ <{s.target}> ?p ?o . }} }}" for s in seeded
    ]
    deletes.append(
        f"DELETE DATA {{ GRAPH <{g}> {{ <{holon}> <{SUCCEEDED_BY}> <{successor}> . }} }}"
    )
    return ";\n".join(deletes)


def _skip_predicates(label: str | None) -> list[str]:
    skip = [LIFECYCLE_PROPERTY, SUCCEEDED_BY, PREDECESSOR]
    if label is not None:
        skip.append(RDFS_LABEL)
    return skip


async def _count_triples(
    client: FusekiClient, conn: Conn, holon: str, seeded: list[Seeded], label: str | None
) -> tuple[int, int]:
    """``(copied, skipped for a blank-node object)`` for A's own triples."""
    sources = ", ".join(f"<{s.source}>" for s in seeded) or "<urn:none>"
    skip = ", ".join(f"<{p}>" for p in _skip_predicates(label))
    query = f"""SELECT (SUM(IF(isBlank(?o), 0, 1)) AS ?copied) (SUM(IF(isBlank(?o), 1, 0)) AS ?blank) WHERE {{
  GRAPH <{conn.holons_graph}> {{ <{holon}> ?p ?o . FILTER (?p NOT IN ({skip}) && ?o NOT IN ({sources})) }}
}}"""
    rows = (await client.select(conn, query)).get("results", {}).get("bindings", [])
    if not rows:
        return 0, 0
    row = rows[0]
    return int(row.get("copied", {}).get("value", 0) or 0), int(
        row.get("blank", {}).get("value", 0) or 0
    )


async def _record_provenance(
    client: FusekiClient, conn: Conn, seeded: list[Seeded], results
) -> bool:
    """``prov:wasDerivedFrom`` from each seeding assertion to A's current assertion (or, for
    a list member, A's fluent). Best effort: B is already complete without it."""
    by_fluent: dict[str, Seeded] = {s.target: s for s in seeded}
    triples = []
    for res in results:
        item = by_fluent.get(res.fluent)
        if item is None:
            continue
        origin = item.assertion or item.source
        triples.append(f"<{res.assertion_iri}> <{PROV}wasDerivedFrom> <{origin}> .")
    if not triples:
        return True
    update = f"INSERT DATA {{ GRAPH <{conn.graph('events')}> {{ {' '.join(triples)} }} }}"
    try:
        await client.update(conn, update)
    except FusekiError as exc:
        log.warning("succession provenance not recorded: %s", exc)
        return False
    return True


# --- the operation ---------------------------------------------------------------------


async def succeed(
    client: FusekiClient,
    conn: Conn,
    *,
    holon: str,
    successor: str,
    asserted_by: str | None,
    reason: str | None = None,
    label: str | None = None,
    tombstone: bool = False,
    asserted_datetime: datetime | None = None,
    gate_scope: str = "assertion",
) -> Succession:
    """Close ``holon`` and open ``successor`` from its final state (see the module docs).

    The caller is responsible for authorisation: this writes the holons, scene, events
    and sequences graphs, and the ontology graph the first time a dataset uses lifecycle.
    """
    if holon == successor:
        raise _bad("same_holon", "a holon cannot succeed itself")
    if not await holon_exists(client, conn, holon):
        raise _bad("unknown_holon", f"<{holon}> is not a holon in this dataset", 404)
    if await holon_exists(client, conn, successor):
        raise _bad("successor_exists", f"<{successor}> already exists in this dataset", 409)
    already, _ = await read_succession(client, conn, holon)
    if already:
        raise _bad("already_succeeded", f"<{holon}> was already succeeded by <{already}>", 409)

    # 1. Plan, while nothing has changed: refuse early what cannot be carried over.
    plan = await _plan(client, conn, holon, successor)
    planned = _ops(plan, holon)
    if len(planned) > MAX_OPS:
        raise _bad(
            "too_large",
            f"<{holon}> has {len(planned)} values to carry over; one succession carries at "
            f"most {MAX_OPS}",
            409,
        )

    # 2. Freeze A: from here its fluents cannot change, so what is read next is final.
    before = await read_state(client, conn, holon)
    target_state = "Tombstoned" if tombstone else "Inactive"
    note = f"succeeded by <{successor}>" + (f" ({reason})" if reason else "")
    if before.state == "Active" or (tombstone and before.state != "Tombstoned"):
        await set_state(
            client,
            conn,
            holon=holon,
            state=target_state,
            asserted_by=asserted_by,
            reason=note,
            asserted_datetime=asserted_datetime,
            gate_scope=gate_scope,
        )
    final_state = (await read_state(client, conn, holon)).state

    async def restore() -> None:
        if before.state != final_state:
            await set_state(
                client, conn, holon=holon, state=before.state, asserted_by=asserted_by,
                reason="succession failed, restored", asserted_datetime=None, gate_scope=gate_scope,
            )  # fmt: skip

    # 3-5. Final values, B's structure, B's fluents.
    created = False
    try:
        plan = await _plan(client, conn, holon, successor)
        ops = _ops(plan, holon)
        copied, blank = await _count_triples(client, conn, holon, plan, label)
        await client.update(conn, _structure_update(conn, holon, successor, plan, label))
        created = True
        results = []
        # A transaction changes each fluent once, so a list fluent's members go in one per
        # round; round one carries every scalar and each list's first member.
        for batch in _rounds(ops):
            transaction = await update_fluents(
                client,
                conn,
                ops=batch,
                asserted_by=asserted_by,
                asserted_datetime=asserted_datetime,
                description=f"succession from <{holon}> to <{successor}>",
                gate_scope=gate_scope,
            )
            results.extend(transaction.results)
    except Exception as exc:
        problems: list[str] = []
        if created:
            try:
                await client.update(conn, _undo_update(conn, holon, successor, plan))
            except Exception as undo:  # noqa: BLE001 - report, do not mask the cause
                problems.append(f"could not remove <{successor}>: {undo}")
        try:
            await restore()
        except Exception as back:  # noqa: BLE001
            problems.append(f"could not restore <{holon}>: {back}")
        suffix = f" ({'; '.join(problems)})" if problems else ""
        if isinstance(exc, LifecycleError):
            raise
        if isinstance(exc, (FluentError, SequenceError)):
            raise _bad("succession_failed", f"succession rolled back: {exc}{suffix}", 409) from exc
        if isinstance(exc, FusekiError):
            raise
        raise _bad("succession_failed", f"succession rolled back: {exc}{suffix}", 500) from exc

    recorded = await _record_provenance(client, conn, plan, results)
    summary = [
        {
            "from": item.source,
            "to": item.target,
            "property": item.prop,
            "values": len(item.values),
        }
        for item in plan
    ]
    return Succession(
        holon=holon,
        successor=successor,
        previous_state=before.state,
        state=final_state,
        copied_triples=copied,
        skipped_triples=blank,
        fluents=summary,
        provenance_recorded=recorded,
    )


async def resolve_successor(
    client: FusekiClient, conn: Conn, holon: str, *, max_hops: int = 10
) -> str:
    """Follow ``hb:succeededBy`` from ``holon`` to the end of its chain, which is the holon
    itself if it has no successor. A cycle or a chain over ``max_hops`` stops where it is."""
    seen = {holon}
    current = holon
    for _ in range(max_hops):
        successor, _ = await read_succession(client, conn, current)
        if not successor or successor in seen:
            break
        seen.add(successor)
        current = successor
    return current
