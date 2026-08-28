"""Dataset listing and override tools.

Both tools here read or write ``session._dataset_override`` /
``session._dataset_override_source``, so this module imports ``session``
itself (qualified access), not just ``mcp``/``_call`` -- see
``session.py``'s own docstring for why a plain name import would go stale.
"""

from __future__ import annotations

from .. import session
from ..session import mcp, _call


@mcp.tool()
async def list_datasets() -> dict:
    """List the datasets the backend actually hosts.

    Different question from ``list_banks``, which reports the bridge's
    configured banks. A bank names a dataset it expects; this says what is
    really there. ``active`` marks the one calls are currently going to.
    """
    result = await _call("GET", "/datasets")
    if isinstance(result, dict) and not result.get("error"):
        result["active"] = session._dataset_override or result.get("active")
        result["datasetOverrideSource"] = session._dataset_override_source
    return result


@mcp.tool()
async def switch_dataset(name: str) -> dict:
    """Point every subsequent call at a different dataset.

    Validates first: switching to a name the server does not host would
    otherwise produce empty reads that look exactly like empty data, which is
    a slow way to find a typo. Sets ``X-Dataset-Override`` on every following
    request, so it needs no restart and no second bridge process.

    Persisted to disk, not just held in memory — a process restart restores
    this choice instead of silently falling back to whatever
    ``HOLONBRIDGE_DATASET`` (or nothing) says, which is exactly the failure
    that motivated this. An explicit ``HOLONBRIDGE_DATASET`` env var still
    wins over the persisted value on the *next* restart, the same as every
    other setting in this codebase — persistence is a convenience for the
    ordinary case, not a way to override a deliberate pin.

    Pass an empty string to clear the override and fall back to the active
    bank's own dataset. Clearing also removes the persisted file, so a
    restart after clearing starts genuinely fresh rather than restoring the
    just-cleared value.
    """
    if not name.strip():
        session._dataset_override = ""
        session._dataset_override_source = "none"
        session._persist_dataset("")
        endpoint = await _call("GET", "/endpoint")
        return {
            "ok": True,
            "cleared": True,
            "dataset": endpoint.get("dataset") if isinstance(endpoint, dict) else None,
            "note": "override cleared; using the active bank's dataset",
        }

    check = await _call("GET", f"/datasets/{name.strip()}")
    if isinstance(check, dict) and check.get("error"):
        return {
            "ok": False,
            "requested": name,
            "detail": check.get("detail"),
            "note": "dataset not switched",
        }

    session._dataset_override = name.strip()
    session._dataset_override_source = "explicit"
    session._persist_dataset(session._dataset_override)
    return {
        "ok": True,
        "dataset": session._dataset_override,
        "graphs": check.get("graphs") if isinstance(check, dict) else None,
    }
