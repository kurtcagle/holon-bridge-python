"""``fetch_media``: have the bridge fetch an image, audio or video file.

A thin wrapper over ``POST /media/fetch``; the rules (egress policy, media types, size cap) are
server-side in ``holonbridge/fetch.py``.
"""

from __future__ import annotations

from ..session import _call, mcp


@mcp.tool()
async def fetch_media(url: str, max_bytes: int = 262144) -> dict:
    """Fetch one image, audio or video file by URL and return it base64-encoded.

    Built for a page that cannot load remote media itself (its content-security policy blocks
    remote hosts): the page calls this and shows ``data:<content_type>;base64,<data_base64>``.
    Returns ``{url, content_type, size_bytes, data_base64}``. The default ``max_bytes`` is small
    on purpose: the whole file comes back inline, so do not use this to read media in a chat.

    Only https, and only public addresses, unless the operator listed the host in
    ``FETCH_ALLOWLIST`` (every redirect is re-checked). Only ``image/*``, ``audio/*`` and
    ``video/*`` come back; anything else is refused with 415. Larger than ``max_bytes`` (or the
    operator's ``FETCH_MEDIA_MAX_BYTES``, whichever is smaller) is refused with 413; an address
    the policy refuses is 403.
    """
    return await _call("POST", "/media/fetch", json_body={"url": url, "max_bytes": max_bytes})
