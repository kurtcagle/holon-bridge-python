"""Fetching image bytes on behalf of a client that cannot fetch them itself.

Why this exists (context for anyone opening this cold): the Holon Navigator
is a published claude.ai artifact, and the sandbox it runs in refuses every
remote ``<img>``, ``<video>``, ``<audio>`` and ``fetch()`` -- only ``data:``
and ``blob:`` sources load (tested 2026-10-02; the violations were
``img-src``, ``media-src``, ``frame-src`` and ``connect-src``). Its one way
out to the network is a tool call to this bridge, so an ``http(s)`` image URL
stored on a holon can only be shown if the bridge fetches it and hands the
bytes back. This module is that fetch: one GET, an image back as base64, and
nothing else. It is deliberately not a general-purpose proxy.

It is also an *outbound request chosen by the caller*, which makes it the
textbook server-side request forgery (SSRF) surface, so most of this file is
about what it will refuse:

- ``http``/``https`` only, no credentials in the URL, and a short list of
  ports (80, 443, 8080, 8443) -- not an arbitrary port scanner.
- The hostname is resolved here, every address it resolves to must be
  globally routable (no loopback, private, link-local -- which is where cloud
  metadata services live -- CGNAT, multicast or reserved ranges, and
  IPv4-mapped/6to4/Teredo/NAT64 forms are unwrapped and checked as the IPv4
  address they carry), and the connection is then made to *that address*
  with the original name sent as ``Host``/SNI. Validating a name and then
  letting the HTTP client resolve it again would leave a DNS-rebinding gap
  between the check and the connect.
- Redirects are followed by hand, up to five, and every hop is validated
  from scratch. A public URL that redirects to ``http://169.254.169.254/``
  is refused at the hop, not followed.
- The response must declare ``image/*`` *and* look like an image (magic
  bytes, or ``<svg`` for SVG) -- the declared type alone is never trusted --
  and is cut off at a byte cap while streaming, never buffered unbounded.

Operators can adjust it with four environment variables:

``MEDIA_FETCH_MAX_BYTES``      per-image cap, default 6 MiB, ceiling 16 MiB.
``MEDIA_FETCH_TIMEOUT``        seconds per network step, default 15.
``MEDIA_FETCH_ALLOWED_HOSTS``  comma-separated; when set, only these hosts
                               and their subdomains may be fetched.
``MEDIA_FETCH_ALLOW_PRIVATE``  true/1/yes/on to permit private and loopback
                               addresses -- for a deployment whose media
                               genuinely lives on an internal host. Off by
                               default; pair it with ALLOWED_HOSTS.

They are read here rather than added to ``config.Settings`` to keep this
change self-contained; moving them there later is mechanical.

SVG is returned like any other image. The Navigator shows it through
``<img>``, where a browser runs no script, so it is inert -- but a client
that inlines the markup into a page instead should sanitise it first.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 6 * 1024 * 1024
HARD_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
USER_AGENT = "HolonBridge-media-fetch/1.0 (+https://github.com/kurtcagle/holon-bridge-python)"

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

#: Resolves ``(host, port)`` to a list of address strings. Injectable so the
#: tests never touch real DNS.
Resolver = Callable[[str, int], Awaitable[list[str]]]


class MediaFetchError(Exception):
    """A fetch was refused or failed. ``status`` is the HTTP status the route
    should answer with: 400 malformed request, 403 refused by policy, 413 too
    large, 415 not an image, 502 the remote end failed, 504 timed out."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class MediaFetchConfig:
    max_bytes: int = DEFAULT_MAX_BYTES
    timeout: float = DEFAULT_TIMEOUT
    allow_private: bool = False
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls) -> "MediaFetchConfig":
        def _number(key: str, default: float, cast):
            raw = os.getenv(key)
            if raw is None or not raw.strip():
                return default
            try:
                value = cast(raw)
            except ValueError:
                log.warning("media fetch: ignoring non-numeric %s=%r", key, raw)
                return default
            return value if value > 0 else default

        hosts = os.getenv("MEDIA_FETCH_ALLOWED_HOSTS", "")
        return cls(
            max_bytes=min(
                int(_number("MEDIA_FETCH_MAX_BYTES", DEFAULT_MAX_BYTES, int)), HARD_MAX_BYTES
            ),
            timeout=float(_number("MEDIA_FETCH_TIMEOUT", DEFAULT_TIMEOUT, float)),
            allow_private=os.getenv("MEDIA_FETCH_ALLOW_PRIVATE", "").strip().lower() in _TRUTHY,
            allowed_hosts=frozenset(
                h.strip().lower().strip(".") for h in hosts.split(",") if h.strip()
            ),
        )


@dataclass(frozen=True)
class MediaResult:
    url: str
    final_url: str
    content_type: str
    size_bytes: int
    data: bytes

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "data_base64": base64.b64encode(self.data).decode("ascii"),
        }


# --------------------------------------------------------------------------
# Address policy
# --------------------------------------------------------------------------


def is_public_address(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is somewhere on the public internet.

    ``ipaddress``'s ``is_global`` already excludes loopback, private,
    link-local, unspecified, reserved and carrier-grade-NAT space. The extra
    work is IPv6 forms that merely *carry* an IPv4 address (mapped, 6to4,
    Teredo, NAT64): ``::ffff:127.0.0.1`` must not pass because it is
    syntactically IPv6.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0] if isinstance(address, str) else address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_address(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_address(ip.sixtofour)
        if ip.teredo is not None:
            return all(is_public_address(part) for part in ip.teredo)
        if ip in _NAT64:
            return is_public_address(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return ip.is_global and not ip.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise MediaFetchError(502, f"could not resolve {host}") from exc
    return list(dict.fromkeys(info[4][0] for info in infos))


def _host_allowed(host: str, config: MediaFetchConfig) -> bool:
    if not config.allowed_hosts:
        return True
    return any(host == h or host.endswith("." + h) for h in config.allowed_hosts)


@dataclass(frozen=True)
class _Target:
    url: httpx.URL  # what the caller asked for (this hop)
    host: str  # ASCII hostname, lower-case
    port: int
    pinned: str | None  # the validated address to connect to; None for an IP literal


async def _validate(url: str, config: MediaFetchConfig, resolver: Resolver) -> _Target:
    if len(url) > MAX_URL_LENGTH:
        raise MediaFetchError(400, f"url is longer than {MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise MediaFetchError(400, "url is not valid") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise MediaFetchError(400, "only http and https urls can be fetched")
    if parts.username is not None or parts.password is not None:
        raise MediaFetchError(400, "urls carrying credentials are not accepted")
    if not parts.hostname:
        raise MediaFetchError(400, "url has no host")

    try:
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise MediaFetchError(400, "url host is not a valid name") from exc
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    if port not in ALLOWED_PORTS:
        raise MediaFetchError(
            403, f"port {port} is not permitted (allowed: {sorted(ALLOWED_PORTS)})"
        )
    if not _host_allowed(host, config):
        raise MediaFetchError(403, f"host {host} is not in this bridge's media allow-list")

    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False

    addresses = [host] if literal else await resolver(host, port)
    if not addresses:
        raise MediaFetchError(502, f"could not resolve {host}")
    if not config.allow_private:
        bad = [a for a in addresses if not is_public_address(a)]
        if bad:
            raise MediaFetchError(403, f"host {host} resolves to a non-public address; refused")

    return _Target(
        url=httpx.URL(url), host=host, port=port, pinned=None if literal else addresses[0]
    )


# --------------------------------------------------------------------------
# Content sniffing
# --------------------------------------------------------------------------


def sniff_image_type(data: bytes, declared: str = "") -> str | None:
    """The image type the bytes actually are, or ``None``. The declared type
    only disambiguates the two formats whose signatures are weak (BMP, SVG)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:2] == b"BM" and len(data) > 14 and declared in ("image/bmp", "image/x-ms-bmp"):
        return "image/bmp"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<") and b"<svg" in head and declared == "image/svg+xml":
        return "image/svg+xml"
    return None


# --------------------------------------------------------------------------
# The fetch
# --------------------------------------------------------------------------


async def _fetch(
    url: str,
    *,
    max_bytes: int,
    config: MediaFetchConfig,
    resolver: Resolver,
    transport: httpx.AsyncBaseTransport | None,
) -> MediaResult:
    timeout = httpx.Timeout(config.timeout)
    current = url
    async with httpx.AsyncClient(
        timeout=timeout, transport=transport, follow_redirects=False
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            target = await _validate(current, config, resolver)

            request_url = target.url
            headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.1"}
            extensions: dict = {}
            if target.pinned is not None:
                # Connect to the address we validated, not whatever the name
                # resolves to a moment later. Host and SNI keep the origin
                # server (and its certificate check) seeing the real name.
                request_url = request_url.copy_with(host=target.pinned)
                default_port = 443 if target.url.scheme == "https" else 80
                headers["Host"] = (
                    target.host if target.port == default_port else f"{target.host}:{target.port}"
                )
                if target.url.scheme == "https":
                    extensions["sni_hostname"] = target.host

            try:
                async with client.stream(
                    "GET", request_url, headers=headers, extensions=extensions
                ) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise MediaFetchError(
                                502, "the remote end redirected without a location"
                            )
                        current = urljoin(str(target.url), location)
                        continue
                    if response.status_code >= 400:
                        raise MediaFetchError(
                            502, f"the remote end answered HTTP {response.status_code}"
                        )

                    declared = (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    )
                    if not declared.startswith("image/"):
                        raise MediaFetchError(
                            415,
                            f"not an image (the remote end sent {declared or 'no content type'})",
                        )
                    length = response.headers.get("content-length", "")
                    if length.isdigit() and int(length) > max_bytes:
                        raise MediaFetchError(
                            413, f"image is larger than the {max_bytes}-byte limit"
                        )

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            raise MediaFetchError(
                                413, f"image is larger than the {max_bytes}-byte limit"
                            )
            except httpx.TimeoutException as exc:
                raise MediaFetchError(504, f"timed out after {config.timeout:g}s") from exc
            except httpx.HTTPError as exc:
                raise MediaFetchError(
                    502, f"could not fetch the image: {exc.__class__.__name__}"
                ) from exc

            data = bytes(body)
            sniffed = sniff_image_type(data, declared)
            if sniffed is None:
                raise MediaFetchError(415, "the content is not a recognised image format")
            return MediaResult(
                url=url,
                final_url=str(target.url),
                content_type=sniffed,
                size_bytes=len(data),
                data=data,
            )

    raise MediaFetchError(502, f"too many redirects (more than {MAX_REDIRECTS})")


async def fetch_image(
    url: str,
    *,
    max_bytes: int | None = None,
    config: MediaFetchConfig | None = None,
    resolver: Resolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MediaResult:
    """GET an image and return its bytes. Raises :class:`MediaFetchError`.

    ``max_bytes`` may lower the configured cap for one call, never raise it.
    ``resolver`` and ``transport`` exist for tests.
    """
    config = config or MediaFetchConfig.from_env()
    cap = config.max_bytes if max_bytes is None else max(1, min(int(max_bytes), config.max_bytes))
    try:
        # The per-step timeout bounds each read; this bounds the whole
        # exchange, so a server dribbling one byte at a time cannot hold a
        # worker for hours.
        return await asyncio.wait_for(
            _fetch(
                url,
                max_bytes=cap,
                config=config,
                resolver=resolver or system_resolver,
                transport=transport,
            ),
            timeout=config.timeout * (MAX_REDIRECTS + 1),
        )
    except asyncio.TimeoutError as exc:
        raise MediaFetchError(504, "timed out") from exc
