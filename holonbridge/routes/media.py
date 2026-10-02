"""Media fetch route.

One route, one job: fetch an ``http(s)`` image on the caller's behalf and
return it as base64, for clients (the Holon Navigator, running in a
claude.ai artifact sandbox) that cannot load remote media themselves. See
``holonbridge/media.py`` for why it exists and for everything it refuses.

A GET, deliberately: it changes nothing, so :func:`audit.is_mutation` does
not treat it as a write and the audit graph does not fill with one entry per
image rendered. A *refusal* is another matter -- a 403 (an address or port
the policy forbids) is exactly what a person probing for internal services
would produce, and ``AuditMiddleware`` records every 401/403 on its own.
:func:`audit.annotate` adds the reason to that entry.

Requires a resolved identity (``AnimusDep``), not just the shared bearer
token: this makes outbound requests from the bridge's network position, and
that should be something an identifiable person did.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..audit import annotate
from ..deps import AnimusDep
from ..media import MediaFetchConfig, MediaFetchError, fetch_image

router = APIRouter(tags=["media"])


@router.get("/media/fetch")
async def media_fetch(
    animus: AnimusDep,
    url: str = Query(..., description="An absolute http(s) URL of an image."),
    max_bytes: int | None = Query(
        None,
        ge=1,
        description="Lower the size cap for this call; it cannot raise the configured cap.",
    ),
) -> dict:
    """Fetch an image and return ``{url, final_url, content_type, size_bytes,
    data_base64}``.

    Errors: 400 malformed url, 403 refused by policy (non-public address,
    disallowed port or host), 413 larger than the cap, 415 not an image,
    502 the remote end failed, 504 timed out.
    """
    try:
        result = await fetch_image(url, max_bytes=max_bytes, config=MediaFetchConfig.from_env())
    except MediaFetchError as exc:
        if exc.status == 403:
            annotate(reason=f"media fetch refused: {exc.message}")
        raise HTTPException(exc.status, detail=exc.message) from exc
    return result.as_dict()
