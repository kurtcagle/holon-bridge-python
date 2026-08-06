"""Fetching source content from outside the bridge.

Ingestion has two shapes that reach past the store: a payload the caller
already has in hand (inline turtle or an inline DataBook), and one they only
point at by URL. This module is the second case, and only the minimal case
at that — one GET, no retries, no auth beyond what the URL itself carries.
Document-set analysis, authenticated federated fetch, and anything needing
more than a single request are out of scope here.
"""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 30.0


class SourceFetchError(RuntimeError):
    """A remote source could not be retrieved."""

    def __init__(self, url: str, message: str) -> None:
        super().__init__(f"{url}: {message}")
        self.url = url
        self.message = message


async def fetch_source(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[str, str]:
    """GET a URL, returning ``(text, content_type)``.

    ``content_type`` is the response header, verbatim — sniffing what the
    body actually is happens one layer up, where the caller also has the
    DataBook frontmatter pattern to check and can make a better call than a
    content-type header alone would let this function make.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.TimeoutException as exc:
        raise SourceFetchError(url, f"timed out after {timeout:g}s") from exc
    except httpx.HTTPError as exc:
        raise SourceFetchError(url, str(exc)) from exc

    if response.status_code >= 400:
        raise SourceFetchError(url, f"HTTP {response.status_code}")

    return response.text, response.headers.get("content-type", "")
