"""Fetching source content from outside the bridge.

Ingestion has two shapes that reach past the store: a payload the caller
already has in hand (inline turtle or an inline DataBook), and one they only
point at by URL. This module is the second case, and only the minimal case
at that — one GET, no retries, no auth beyond what the URL itself carries.
Document-set analysis, authenticated federated fetch, and anything needing
more than a single request are out of scope here.

CHANGED 2026-11-01: the GET used to go anywhere. The bridge sits next to
Fuseki, the cloud metadata service and whatever else listens on a private
address, and ``/ingest`` is reachable by anyone who can write a graph (and by
an LLM driving the MCP tool, which a document it is reading can steer). So the
URL, and every redirect hop, now passes the same outbound policy as a webhook
(see ``egress.py``): https and public addresses only, unless the operator lists
a host in ``FETCH_ALLOWLIST``, in which case only listed hosts can be reached.
Redirects are followed a few hops, each one re-checked. The body is read as a
stream and abandoned past ``FETCH_MAX_BYTES``, counted after decompression.
Error text names the kind of failure, not the library's detail, so a refusal
or a failed connection does not describe the network behind the bridge.

Known limit, as for webhooks: the policy resolves the host and the client
resolves it again to connect (DNS rebinding). See ``egress.py``.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .config import get_settings
from .egress import EgressPolicy, EndpointRefused, Resolver, check_endpoint

DEFAULT_TIMEOUT = 30.0
#: Redirect hops followed (each re-checked against the policy).
MAX_REDIRECTS = 3
_REDIRECTS = {301, 302, 303, 307, 308}


class SourceFetchError(RuntimeError):
    """A remote source could not be retrieved."""

    def __init__(self, url: str, message: str) -> None:
        super().__init__(f"{url}: {message}")
        self.url = url
        self.message = message


def _shown(url: str) -> str:
    """``url`` without any userinfo, for messages and logs."""
    try:
        parts = urlsplit(url)
        if parts.username or parts.password:
            host = parts.hostname or ""
            netloc = f"{host}:{parts.port}" if parts.port else host
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass
    return url


def policy_from_settings() -> EgressPolicy:
    return EgressPolicy(
        allowlist=get_settings().fetch_allowlist,
        setting="FETCH_ALLOWLIST",
        label="fetch allowlist",
    )


async def fetch_source(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    policy: EgressPolicy | None = None,
    max_bytes: int | None = None,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str]:
    """GET a URL, returning ``(text, content_type)``.

    ``content_type`` is the response header, verbatim — sniffing what the
    body actually is happens one layer up, where the caller also has the
    DataBook frontmatter pattern to check and can make a better call than a
    content-type header alone would let this function make.

    ``policy`` and ``max_bytes`` default to the operator's settings;
    ``resolver`` and ``transport`` are seams for tests.
    """
    policy = policy or policy_from_settings()
    limit = max_bytes if max_bytes is not None else get_settings().fetch_max_bytes
    shown = _shown(url)
    current = url

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=transport
        ) as client:
            for hop in range(MAX_REDIRECTS + 1):
                try:
                    await check_endpoint(current, policy, resolver=resolver)
                except EndpointRefused as exc:
                    where = "" if hop == 0 else " (after a redirect)"
                    raise SourceFetchError(shown, f"refused{where}: {exc}") from exc

                async with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECTS:
                        target = response.headers.get("location", "")
                        if not target:
                            raise SourceFetchError(shown, "redirect without a Location")
                        current = urljoin(current, target)
                        continue
                    if response.status_code >= 400:
                        raise SourceFetchError(shown, f"HTTP {response.status_code}")
                    text = await _read_text(response, limit, shown)
                    return text, response.headers.get("content-type", "")

            raise SourceFetchError(shown, f"more than {MAX_REDIRECTS} redirects")
    except SourceFetchError:
        raise
    except httpx.TimeoutException as exc:
        raise SourceFetchError(shown, f"timed out after {timeout:g}s") from exc
    except httpx.HTTPError as exc:
        # The kind of failure, not the library's account of it.
        raise SourceFetchError(shown, f"request failed ({type(exc).__name__})") from exc


async def _read_text(response: httpx.Response, limit: int, shown: str) -> str:
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise SourceFetchError(shown, f"response is larger than the {limit}-byte limit")

    body = bytearray()
    # Decoded bytes: a small compressed body that expands past the limit stops here.
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            raise SourceFetchError(shown, f"response is larger than the {limit}-byte limit")

    try:
        return body.decode(response.encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
