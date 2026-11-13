"""The post-write hook: evaluate state triggers after data has changed.

Before this, only a fluent transition evaluated state triggers. A write by any
other route -- ``push_turtle``, ``/ingest``, ``create_holon``, a SPARQL update,
a named update, a rule run, a scheduler payload, a snapshot restore -- changed
the data a trigger's condition looks at and fired nothing until the next fluent
transition happened to come along. Every route that writes domain data now calls
:func:`after_write` once the write has committed.

Properties worth stating:

* **It never affects the write.** It runs after the commit, and any failure is
  logged and swallowed, exactly as the fluent hook always did. A write that
  succeeded does not turn into an error because a trigger misbehaved.
* **It is not recursive.** A firing writes, and those writes reach this hook;
  ``triggers._evaluating`` makes the nested call a no-op.
* **System graphs do not fire it.** A write that touches only the bridge's own
  bookkeeping (registries, the trigger ledger, candidates, projection state,
  sequences, SHACL shapes) cannot change what a condition over domain data
  sees, and evaluating on every such write would make the ledger's own writes
  trigger evaluation. If the graphs touched are unknown (``None``) it fires.
* **``touched`` narrows, never decides.** See ``touched.py``: unknown means
  "evaluate everything".

Paths that deliberately do *not* call it: the registries and their cache
(named queries/rules/projections/triggers, pipelines), the audit log,
identity and delegation administration, the message store, projection and
scheduler bookkeeping, the SHACL reduction scratch graphs, and snapshot restore
(which only fills an empty dataset, ledger included, and should not fire
anything while it does). None of those is
domain data a condition is written over.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from .conn import Conn
from .fuseki import FusekiClient
log = logging.getLogger("holonbridge.triggers")

#: Graph roles that are the bridge's own bookkeeping rather than domain data.
SYSTEM_ROLES = (
    "named-queries",
    "named-projections",
    "named-rules",
    "named-triggers",
    "portals",
    "trigger-log",
    "candidates",
    "agents",
    "agent-calls",
    "pipelines",
    "projections",
    "projection-log",
    "sequences",
    "meta",
    "shacl",
    "shacl-context",
)


def system_graphs(conn: Conn) -> frozenset[str]:
    return frozenset(conn.graph(role) for role in SYSTEM_ROLES)


def only_system_graphs(conn: Conn, graphs: Iterable[str] | None) -> bool:
    """True when ``graphs`` is known, non-empty, and entirely bookkeeping."""
    if graphs is None:
        return False
    graphs = set(graphs)
    return bool(graphs) and graphs <= system_graphs(conn)


async def after_write(
    client: FusekiClient,
    conn: Conn,
    *,
    graphs: Iterable[str] | None = None,
    touched: frozenset[str] | Callable[[], frozenset[str] | None] | None = None,
    source: str = "",
) -> None:
    """Evaluate the state triggers a committed write may have set off.

    ``graphs`` are the graphs written, when known. ``touched`` is the set of
    predicates written, or a callable that works it out (only called if some
    trigger declares watched predicates), or ``None`` if unknown.
    """
    if only_system_graphs(conn, graphs):
        return
    # Imported here: triggers imports ingest, and ingest calls this hook.
    from .triggers import TRIGGER_STATE, evaluate_triggers  # noqa: PLC0415

    try:
        await evaluate_triggers(client, conn, kind=TRIGGER_STATE, touched_predicates=touched)
    except Exception:  # noqa: BLE001 - a trigger failure must not break the write
        log.exception("StateTrigger evaluation failed after a write (%s); the write itself committed", source or "unknown source")


async def after_write_update(
    client: FusekiClient, conn: Conn, update: str, *, source: str = ""
) -> None:
    """:func:`after_write` for a SPARQL update, working out what it touched.

    The graphs are the update's own graph references; if it reaches a graph it
    does not name (``GRAPH ?g``) or writes the default graph, they are unknown.
    The predicates are whatever its INSERT/DELETE templates name, or unknown.
    """
    from .acl import analyse_graph_refs  # noqa: PLC0415
    from .touched import update_predicates  # noqa: PLC0415

    analysis = analyse_graph_refs(update)
    graphs = None
    if analysis is not None and not analysis.dynamic and not analysis.default_write:
        graphs = analysis.all_refs
    await after_write(
        client, conn, graphs=graphs, touched=lambda: update_predicates(update), source=source
    )
