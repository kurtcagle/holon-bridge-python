"""Focus tools: list_avatars, get_focus, set_focus.

A focus is an avatar (the point of view you read as) and an optional time. The place is not
chosen: it is read from where that avatar is. Held per person and dataset on the bridge, like
``switch_persona``, so this MCP process keeps no state of its own.
"""

from __future__ import annotations

from ..session import _call, mcp


@mcp.tool()
async def list_avatars(include_tombstoned: bool = False) -> dict:
    """List the avatars in the current dataset: every entity typed ``holon:Avatar``.

    An avatar is a role any entity can play to stand for a point of view -- a person, persona,
    account, company or character. Each entry gives its IRI, label, lifecycle state
    (Active/Inactive/Tombstoned) and whether it is the dataset's default avatar. Tombstoned
    avatars are left out unless ``include_tombstoned`` is true.

    Use this to find a valid ``avatar`` for ``set_focus``.
    """
    return await _call("GET", "/focus/avatars", params={"include_tombstoned": include_tombstoned})


@mcp.tool()
async def get_focus(avatar: str = "", as_of: str = "") -> dict:
    """Where your focus is now: which avatar you read as, and the place it is at.

    The avatar is, in order: the ``avatar`` argument, the one you saved with ``set_focus``,
    the dataset's default avatar. With none of those it refuses rather than guess. The time is
    ``as_of`` (ISO 8601), else the one you saved, else now -- read at each call, never frozen.

    ``paths`` is the place: each root-to-leaf chain of the avatar's focus properties and their
    values (e.g. a train, then a carriage within it). Passing ``avatar`` or ``as_of`` here
    looks through them for this call only; it does not save them. Changing focus does not
    create or change any holon.

    Follows a succeeded avatar to its successor and reports ``redirectedFrom``.
    """
    params = {k: v for k, v in {"avatar": avatar, "as_of": as_of}.items() if v}
    return await _call("GET", "/focus", params=params)


@mcp.tool()
async def set_focus(avatar: str = "", as_of: str = "") -> dict:
    """Save your focus for the current dataset: the avatar to read as, and optionally a time.

    Replaces whatever you had saved. Leave both empty to clear it (back to the default avatar,
    and now). ``avatar`` must be typed ``holon:Avatar`` and not Tombstoned; an Inactive avatar
    is allowed. The focus is resolved before it is saved, so one that cannot work is refused
    rather than stored.

    Per person and dataset: it cannot move another caller's focus. It writes nothing to the
    graph and needs no write grant. Call ``list_avatars`` to see the choices.
    """
    body = {"avatar": avatar, "as_of": as_of}
    return await _call("POST", "/focus", json_body=body)
