"""Portal tools.

A portal is a door between two holons: a registered link with a lock. A
traveller's position is a fluent; once a portal is registered for that position
property, the only way the position changes is by crossing a portal, which checks
the traveller is at the origin, runs the lock (and optionally a SHACL boundary
check on a payload), and moves the traveller and applies any side effects as one
all-or-nothing transaction. See ``holonbridge/portal.py`` on the bridge.
"""

from __future__ import annotations

from typing import Any

from ..session import mcp, _call


@mcp.tool()
async def list_portals(portal_status: str | None = None) -> dict:
    """List registered portals (``Active`` or ``Suspended``): each with its origin
    (``from``), destination (``to``), the position property it governs, and its lock,
    effect and boundary shapes if it has them."""
    return await _call("GET", "/portals", params={"portal_status": portal_status} if portal_status else None)


@mcp.tool()
async def register_portal(
    id: str,
    from_holon: str,
    to_holon: str,
    position_property: str,
    lock: str | None = None,
    effect: str | None = None,
    boundary_shapes: str | None = None,
    status: str = "Active",
    label: str | None = None,
    description: str | None = None,
) -> dict:
    """Register (or overwrite) a portal.

    ``from_holon`` and ``to_holon`` are the IRIs of the two holons it joins.
    ``position_property`` is the IRI of the ``holon:FluentProperty`` whose fluent says
    where a traveller is (the traveller links to that fluent in the holons graph).
    **Registering a portal makes that property portal-governed**: ``update_fluent``,
    ``update_fluents`` and trigger fluent actions then refuse to change a fluent of
    that property, and only ``traverse_portal`` may move it. Delete the portal to
    release the property.

    ``lock`` is the id of a named SELECT. It is bound to the traveller as ``focus``
    (declare a ``focus`` parameter of type anyURI); the door is open when it returns a
    row, and shut if it returns none or its first row has ``?open`` or ``?allowed``
    false. A ``?reason`` column is passed back to the caller when it is shut.

    ``effect`` is the id of a named SELECT, also bound to the traveller, whose rows
    (``?fluent`` and ``?value``, optionally ``?operation``, ``?isIri``, ``?assertedAt``,
    ``?description``) are applied in the same transaction as the move: a toll taken, a
    key used, an arrival counted.

    ``boundary_shapes`` is the IRI of a graph of SHACL shapes. If given, the traveller
    must send a Turtle ``payload`` describing itself that satisfies them. This is how a
    portal consults a boundary; the boundary itself says nothing about portals.

    Needs write access to the portals graph, and read access to what the lock and
    effect queries read.
    """
    return await _call(
        "POST",
        "/portal",
        json_body={
            "id": id,
            "from": from_holon,
            "to": to_holon,
            "position_property": position_property,
            "lock": lock,
            "effect": effect,
            "boundary_shapes": boundary_shapes,
            "status": status,
            "label": label,
            "description": description,
        },
    )


@mcp.tool()
async def set_portal_status(portal_id: str, status: str) -> dict:
    """Open or close a portal: ``Active`` or ``Suspended``. A suspended portal refuses
    every crossing but still governs its position property."""
    return await _call("POST", f"/portal/{portal_id}/status", json_body={"status": status})


@mcp.tool()
async def delete_portal(portal_id: str) -> dict:
    """Delete a portal. Its position property is no longer governed by it. Past
    crossings stay in the ledger. Needs replace access to the portals graph."""
    return await _call("DELETE", f"/portal/{portal_id}")


@mcp.tool()
async def traverse_portal(
    portal_id: str,
    traveller: str,
    payload: str | None = None,
    description: str | None = None,
    asserted_datetime: str | None = None,
) -> dict:
    """Cross a portal as ``traveller`` (a holon IRI).

    In order: the portal must be Active; the traveller's position must be the portal's
    origin; if the portal has boundary shapes, ``payload`` (Turtle describing the
    traveller) must satisfy them; the lock must be open. Then the position moves to the
    destination and any effects are applied, all as one transaction (every change or
    none). A refusal comes back with a machine-readable ``error``: ``portal_suspended``,
    ``not_at_origin``, ``boundary_needs_payload``, ``boundary_refused`` (with
    ``violations``), ``locked`` (with ``reason`` if the lock gave one), ``effect_invalid``,
    ``crossing_refused`` (e.g. a shapes-graph rejection of the combined result).

    Needs write access to the events, scene and sequences graphs, as ``update_fluent``
    does. The crossing is attributed to you. The lock is evaluated just before the
    transaction, not inside it; the position change itself is conditional on the
    traveller still being at the origin.
    """
    return await _call(
        "POST",
        f"/portal/{portal_id}/traverse",
        json_body={
            "traveller": traveller,
            "payload": payload,
            "description": description,
            "asserted_datetime": asserted_datetime,
        },
    )
