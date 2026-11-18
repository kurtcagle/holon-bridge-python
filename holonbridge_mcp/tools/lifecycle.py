"""Holon lifecycle tools: make a holon Active, Inactive or Tombstoned, read its state, and
close one holon in favour of a successor that carries its final state.

An inactive holon can still be read and queried, but its fluents cannot change; it
can be made Active again. A tombstoned holon is inactive and is also left out of the
neighbour lists ``get_holon`` returns (it can still be read by IRI). See
``holonbridge/lifecycle.py`` on the bridge.
"""

from __future__ import annotations

from ..session import _call, mcp


@mcp.tool()
async def set_holon_lifecycle(
    holon: str,
    state: str,
    reason: str | None = None,
    effective_at: str | None = None,
) -> dict:
    """Make a holon ``Active``, ``Inactive`` or ``Tombstoned``.

    - ``Active`` accepts change (the default for every holon).
    - ``Inactive`` can be read and queried, but ``update_fluent``, ``update_fluents``,
      trigger fluent actions and portal crossings refuse to change its fluents (409,
      ``holon_inactive``).
    - ``Tombstoned`` is inactive and also left out of discovery: ``get_holon`` omits it
      from neighbour lists unless asked. It can still be read by IRI.

    Any state may follow any other, so every change is reversible. Asking for the state
    the holon is already in changes nothing. The change is an ordinary fluent transition:
    it is recorded on the ledger, attributed to you, and readable as of a past time with
    ``get_holon``'s ``observed_at``. ``reason`` is recorded with it. ``effective_at`` (ISO
    8601, no timezone means UTC) is when the holon entered the state; it defaults to now.

    This freezes the holon's fluents and, for ordinary writes, its structure too:
    ``push_turtle``, ``create_holon`` and ``sparql_update`` that name an inactive holon as a
    subject are refused (409). It is not a seal: updates that reach a holon only through a
    WHERE clause, whole-graph operations (CLEAR, DROP, LOAD, COPY, MOVE, ADD), named rules
    and ``/graph-op`` are not checked.

    A holon that has a successor (see ``succeed_holon``) cannot be made Active again.

    Needs write access to the events, scene, sequences and holons graphs (an administrator
    has it implicitly), and the ontology graph the first time a dataset uses lifecycle.
    """
    body: dict = {"holon": holon, "state": state}
    if reason:
        body["reason"] = reason
    if effective_at:
        body["effective_at"] = effective_at
    return await _call("POST", "/holon/lifecycle", json_body=body)


@mcp.tool()
async def get_holon_lifecycle(holon: str) -> dict:
    """A holon's current lifecycle state (``Active``, ``Inactive`` or ``Tombstoned``), the
    ledger entry that set it, and when (valid time). A holon that has never changed state
    is Active. ``get_holon`` also reports it in its frontmatter."""
    return await _call("GET", "/holon/lifecycle", params={"iri": holon})


@mcp.tool()
async def succeed_holon(
    holon: str,
    successor: str,
    reason: str | None = None,
    label: str | None = None,
    tombstone: bool = False,
    effective_at: str | None = None,
) -> dict:
    """Close a holon and open a new one that carries its final state.

    The predecessor is made Inactive (or Tombstoned with ``tombstone=true``) first, so its
    fluents cannot change while they are read. The successor ``successor`` (an IRI that
    must not exist yet) then gets the predecessor's outgoing triples (same parents,
    labels and so on, minus its fluents and lifecycle), a fluent of its own for each of the
    predecessor's fluents, and each fluent's final value (every member, for a list). Those
    values are written as new assertions on the successor's own ledger with
    ``prov:wasDerivedFrom`` pointing at the predecessor's. ``hb:succeededBy`` and
    ``hb:predecessor`` link the two.

    A holon can be succeeded once, and once it has a successor it cannot be reactivated.
    If anything fails the successor is removed and the predecessor is put back as it was.

    Limits: one-to-one only; at most 20 values; triples whose object is a blank node are
    not copied (counted in ``skippedBlankNodeTriples``); incoming links from other holons
    are not moved; a value whose type cannot be carried over exactly is refused (409,
    ``cannot_carry``) before anything changes. Values go across in rounds (a list fluent's
    members one per round), so a very busy holon could be seen part-seeded for an instant.

    ``label`` replaces the copied rdfs:label on the successor. ``effective_at`` is the valid
    time of the predecessor's state change. Needs the same grants as ``set_holon_lifecycle``,
    plus write access to the holons graph.
    """
    body: dict = {"holon": holon, "successor": successor, "tombstone": tombstone}
    if reason:
        body["reason"] = reason
    if label:
        body["label"] = label
    if effective_at:
        body["effective_at"] = effective_at
    return await _call("POST", "/holon/succeed", json_body=body)
