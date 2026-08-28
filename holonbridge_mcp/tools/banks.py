"""Bank listing and override tools.

Same qualified-``session``-access reasoning as ``tools/datasets.py`` --
see that module's and ``session.py``'s docstrings.
"""

from __future__ import annotations

from .. import session
from ..session import mcp, _call


@mcp.tool()
async def list_banks() -> dict:
    """List the banks the bridge has configured, and which one this process uses.

    A *bank* is a named backend connection — a server URL, a default dataset,
    and optional credentials. Formerly called a "profile".

    ``active`` in the bridge's own response is the bank the *bridge* treats as
    current. ``bankOverride`` is this MCP process's own per-call override,
    which takes precedence for every call made from here without changing
    anything for other clients.
    """
    result = await _call("GET", "/endpoints")
    if isinstance(result, dict) and not result.get("error"):
        result["bankOverride"] = session._bank_override
        result["bankOverrideSource"] = session._bank_override_source
    return result


@mcp.tool()
async def switch_bank(name: str) -> dict:
    """Point every subsequent call from this process at a different bank.

    Validates first: switching to a bank the bridge has not configured would
    otherwise produce 404s, or worse, silently fall through to the wrong
    store. Applied as ``?bank=`` on every following request, so it needs no
    restart and no second bridge process.

    Different from ``set_endpoint``, deliberately. ``set_endpoint`` changes
    the bridge's *own* active bank, which every client then sees; this changes
    only what this MCP process asks for, leaving other clients alone. Prefer
    this one unless the intent really is to move the whole bridge.

    Persisted to disk on the same reasoning as ``switch_dataset``: a process
    restart restores this choice rather than silently reverting, because a
    call that reaches the wrong store still succeeds and still returns data.
    An explicit ``HOLONBRIDGE_BANK`` env var still wins on the next restart.

    Pass an empty string to clear the override and fall back to whatever the
    bridge considers active. Clearing removes the persisted file too.
    """
    if not name.strip():
        session._bank_override = ""
        session._bank_override_source = "none"
        session._persist_bank("")
        endpoint = await _call("GET", "/endpoint")
        return {
            "ok": True,
            "cleared": True,
            "bank": endpoint.get("bank") if isinstance(endpoint, dict) else None,
            "note": "override cleared; using the bridge's active bank",
        }

    wanted = name.strip()
    listing = await _call("GET", "/endpoints")
    if isinstance(listing, dict) and listing.get("error"):
        return {
            "ok": False,
            "requested": wanted,
            "detail": listing.get("detail"),
            "note": "bank not switched; could not read the bank list",
        }

    known = [
        b.get("name")
        for b in (listing.get("banks") or [])
        if isinstance(b, dict)
    ]
    if wanted not in known:
        return {
            "ok": False,
            "requested": wanted,
            "known": known,
            "note": "bank not switched; no bank by that name is configured",
        }

    session._bank_override = wanted
    session._bank_override_source = "explicit"
    session._persist_bank(session._bank_override)
    return {"ok": True, "bank": session._bank_override, "known": known}
