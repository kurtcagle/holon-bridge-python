"""Media-fetch tool."""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def fetch_media(url: str, max_bytes: int | None = None) -> dict:
    """Fetch an image over HTTP(S) through the bridge, returned as base64.

    For clients that cannot load remote media themselves -- the Holon
    Navigator, running inside a claude.ai artifact sandbox, is the case this
    was built for. Returns ``content_type``, ``size_bytes``, ``data_base64``
    and the ``final_url`` after any redirects; build a ``data:`` URI from the
    first and the third to display it.

    Images only (PNG, JPEG, GIF, WebP, AVIF, BMP, ICO, SVG), checked against
    the bytes and not just the declared type. The bridge refuses URLs that
    resolve to private, loopback or link-local addresses, non-web ports, and
    URLs carrying credentials, and follows at most five redirects, each
    checked afresh. ``max_bytes`` can lower the size cap for one call; it
    cannot raise the bridge's own.
    """
    params: dict = {"url": url}
    if max_bytes is not None:
        params["max_bytes"] = max_bytes
    return await _call("GET", "/media/fetch", params=params)
