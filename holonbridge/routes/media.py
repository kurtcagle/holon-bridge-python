"""``POST /media/fetch``: fetch one image, audio or video file on a caller's behalf.

Why it exists: a page served from a locked-down origin (a published artifact, say) cannot load
``<img src="https://...">`` -- its content-security policy refuses remote hosts. It can call the
bridge, so it asks the bridge to fetch the file and gets it back base64-encoded, ready to show as
a ``data:`` URI. The ``fetch_media`` MCP tool is a thin wrapper over this route.

The fetch is an outbound request the caller chooses, so it is held to the same policy as
``/ingest``'s ``source_url`` (:mod:`holonbridge.fetch`, :mod:`holonbridge.egress`): https only and
public addresses only unless the operator lists hosts in ``FETCH_ALLOWLIST``, every redirect hop
re-checked, the body read as a bounded stream. On top of that, only ``image/*``, ``audio/*`` and
``video/*`` are returned (decided from the response headers, before the body is read), and
``FETCH_MEDIA_MAX_BYTES`` caps the size -- the caller's ``max_bytes`` can only lower it.

Gate: a resolved identity, as for reading a graph. No grant on any dataset is needed, because
nothing in the store is read or written. The audit entry records the host, never the full URL
(a query string can carry a signature).
"""

from __future__ import annotations

import base64
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from .. import audit
from ..deps import AnimusDep
from ..fetch import SourceFetchError, fetch_media

router = APIRouter(prefix="/media", tags=["media"])

#: What each kind of refusal is called on the wire.
_STATUS = {
    "refused": status.HTTP_403_FORBIDDEN,
    "not_media": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "too_large": 413,
}


class MediaFetchRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096, description="An http(s) URL of the file.")
    max_bytes: int | None = Field(
        default=None,
        ge=1,
        description="Refuse a file larger than this. Can only lower the operator's "
        "FETCH_MEDIA_MAX_BYTES, never raise it.",
    )


@router.post("/fetch")
async def fetch_media_route(body: MediaFetchRequest, animus: AnimusDep) -> dict:
    """Fetch an image, audio or video file and return it base64-encoded.

    Returns ``{url, content_type, size_bytes, data_base64}``. Refusals: 403 when the egress
    policy refuses the URL (or a redirect), 415 when the response is not image/audio/video, 413
    when it is over the size limit, 502 for anything else that goes wrong reaching it.
    """
    host = urlsplit(body.url).hostname or ""
    audit.annotate(security_read=True, reason=f"media fetch from {host}"[:200])
    try:
        data, content_type = await fetch_media(body.url, max_bytes=body.max_bytes)
    except SourceFetchError as exc:
        raise HTTPException(_STATUS.get(exc.kind, status.HTTP_502_BAD_GATEWAY), detail=str(exc)) from exc
    return {
        "url": body.url.split("#", 1)[0],
        "content_type": content_type,
        "size_bytes": len(data),
        "data_base64": base64.b64encode(data).decode("ascii"),
    }
