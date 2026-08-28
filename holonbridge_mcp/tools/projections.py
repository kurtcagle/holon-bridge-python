"""Projection hook and delivery tools.

The graph stays authoritative. A hook computes a scoped slice, diffs it
against what was last delivered, and hands the difference to a target that
does its own transformation. The bridge knows nothing about SQL or XSLT.
"""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def list_projection_hooks(hook_status: str | None = None) -> dict:
    """List projection hooks, with any configuration problems flagged."""
    params = {"hook_status": hook_status} if hook_status else None
    return await _call("GET", "/projection/hooks", params=params)


@mcp.tool()
async def get_projection_hook(hook_id: str) -> dict:
    """One hook, its scope, and how many triples its watermark holds."""
    return await _call("GET", f"/projection/hook/{hook_id}")


@mcp.tool()
async def register_projection_hook(
    hook_id: str,
    target: str,
    scope: str | None = None,
    named_query: str | None = None,
    change_mode: str = "upsert",
    delivery: str = "pull",
    endpoint: str | None = None,
    key_predicate: str | None = None,
) -> dict:
    """Register a projection hook.

    ``scope`` is a CONSTRUCT defining what this target sees, or use
    ``named_query`` to point at a registered one. ``change_mode`` is how the
    target wants change expressed: ``append`` (additions only), ``upsert``
    (keyed by ``key_predicate``), ``soft-delete`` (retractions become
    tombstones), or ``replace`` (whole slice each time). ``delivery`` is
    ``webhook`` (the bridge POSTs to ``endpoint``) or ``pull`` (the target
    collects and acknowledges).
    """
    return await _call(
        "POST",
        "/projection/hook",
        json_body={
            "id": hook_id,
            "target": target,
            "scope": scope,
            "named_query": named_query,
            "change_mode": change_mode,
            "delivery": delivery,
            "endpoint": endpoint,
            "key_predicate": key_predicate,
        },
    )


@mcp.tool()
async def run_projection_hook(
    hook_id: str, force: bool = False, include_payload: bool = True
) -> dict:
    """Compute what changed since the last delivery and hand it over.

    For a ``pull`` hook this returns the envelope and holds it pending; call
    ``acknowledge_projection_delivery`` once the target has applied it. The
    watermark only advances on acknowledgement, so an unacknowledged delivery
    is simply re-derived next run.
    """
    return await _call(
        "POST",
        f"/projection/hook/{hook_id}/run",
        json_body={"force": force, "include_payload": include_payload},
    )


@mcp.tool()
async def reset_projection_hook(hook_id: str) -> dict:
    """Forget what has been delivered, so the next run sends the whole slice."""
    return await _call("POST", f"/projection/hook/{hook_id}/reset")


@mcp.tool()
async def sweep_projection_deliveries(max_age_seconds: float = 86400) -> dict:
    """Reclaim pull deliveries no target ever acknowledged.

    Safe to run often: sweeping leaves the watermark alone, so a swept
    delivery's difference is re-derived on the next run.
    """
    return await _call(
        "POST", "/projection/sweep", json_body={"max_age_seconds": max_age_seconds}
    )


@mcp.tool()
async def list_projection_deliveries(
    hook: str | None = None, delivery_status: str | None = None, limit: int = 50
) -> dict:
    """Delivery records. Filter by hook or by pending/delivered/acknowledged/failed."""
    params = {"limit": limit}
    if hook:
        params["hook"] = hook
    if delivery_status:
        params["delivery_status"] = delivery_status
    return await _call("GET", "/projection/deliveries", params=params)


@mcp.tool()
async def get_projection_delivery(
    delivery_id: str, include_payload: bool = True
) -> dict:
    """One delivery; a pending one comes back with its envelope."""
    return await _call(
        "GET",
        f"/projection/delivery/{delivery_id}",
        params={"include_payload": include_payload},
    )


@mcp.tool()
async def acknowledge_projection_delivery(delivery_id: str) -> dict:
    """Confirm the target applied this envelope. The watermark advances."""
    return await _call("POST", f"/projection/delivery/{delivery_id}/ack")


@mcp.tool()
async def reject_projection_delivery(
    delivery_id: str, reason: str = "rejected by target"
) -> dict:
    """Report that the target could not apply it. The watermark stays put, so
    the same difference is offered again on the next run."""
    return await _call(
        "POST", f"/projection/delivery/{delivery_id}/reject", json_body={"reason": reason}
    )
