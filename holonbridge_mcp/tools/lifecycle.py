"""Holon lifecycle tools: make a holon Active, Inactive or Tombstoned, and read its state.

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

    This freezes fluents only. Structural writes (``push_turtle``, ``create_holon``, named
    rules, raw ``sparql_update``) are not stopped by it.

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
