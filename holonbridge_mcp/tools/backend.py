"""Restarting the Fuseki server: ``restart_fuseki`` and ``get_backend_status``.

Thin wrappers over ``POST /admin/backend/restart`` and ``GET /admin/backend/status``; all
the rules are server-side in ``holonbridge/fuseki_restart.py``.
"""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def restart_fuseki(
    reason: str,
    commit: bool = False,
    snapshot_first: bool = True,
    restore_from: str = "auto",
    acknowledge_loss: list[str] | None = None,
    wait_seconds: int = 90,
) -> dict:
    """Preview or perform a restart of the Fuseki server, and rebuild its in-memory datasets.

    A restart empties every in-memory dataset (data, seeded shapes and owner) and is the only
    way a new dataset gets its /shacl endpoint. The default is a DRY RUN (``commit=false``):
    it returns the plan -- every dataset and whether it is persistent or in-memory, where each
    in-memory one would be rebuilt from, and any blockers -- and changes nothing. Show the
    person the plan and get their go-ahead before calling again with ``commit=true``.

    ``snapshot_first`` (default true) snapshots every in-memory dataset that holds data first.
    ``restore_from`` is ``auto`` (the pinned default backup if there is one, else the newest
    snapshot), ``latest`` or ``default``. ``acknowledge_loss`` lists in-memory datasets the
    person knowingly accepts losing because they have no backup. ``wait_seconds`` is how long
    to wait for Fuseki to come back.

    A restart is refused (409 ``restart_blocked``, with the plan) while an in-memory dataset
    holding data has no way back, an assembler file does not parse, a write or a Fuseki backup
    is in progress, another restart is pending, or the last was under a minute ago. If Fuseki
    does not come back healthy, the helper restores the last known-good configuration and
    restarts again (``status: rolled_back``).

    Only for a caller whose identity is established AND on the operator's
    ``FUSEKI_RESTARTERS``; anyone else gets a 403. Fuseki only: a Fluree bank gets 409
    ``not_applicable``. Take care: this interrupts every dataset on the server.
    """
    return await _call(
        "POST",
        "/admin/backend/restart",
        json_body={
            "reason": reason,
            "commit": commit,
            "snapshot_first": snapshot_first,
            "restore_from": restore_from,
            "acknowledge_loss": acknowledge_loss or [],
            "wait_seconds": wait_seconds,
        },
    )


@mcp.tool()
async def get_backend_status() -> dict:
    """Read-only: the Fuseki server's datasets and their kinds (persistent / in-memory),
    running Fuseki tasks (backups), assembler files that do not parse, the last restart and
    whether one is pending. Same gate as ``restart_fuseki`` (``FUSEKI_RESTARTERS``)."""
    return await _call("GET", "/admin/backend/status")
