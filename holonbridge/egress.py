"""Outbound endpoint policy: where the bridge is allowed to send things.

A projection hook with ``delivery=webhook`` makes the bridge POST a slice of
the graph to a URL somebody typed in. Left unchecked that is two problems at
once: an exfiltration route (the slice can be any CONSTRUCT over the dataset)
and a server-side request forgery primitive (the bridge sits inside the
network, next to Fuseki, the cloud metadata service and whatever else listens
on a private address, and will happily POST to any of them).

The policy here is deliberately small and operator-controlled:

* **Scheme.** ``http`` and ``https`` only. No credentials in the URL (they end
  up in logs, hook listings and delivery errors).
* **No allowlist configured (the default).** The endpoint must be ``https`` and
  every address its host resolves to must be a public one -- loopback, private,
  link-local (which includes ``169.254.169.254``), CGNAT, multicast and
  reserved ranges are refused, as are hosts that do not resolve at all.
* **Allowlist configured** (``WEBHOOK_ALLOWLIST``, comma-separated). The
  endpoint's host must match an entry, and nothing else may be reached. An
  entry is an explicit operator decision, so a listed host is exempt from the
  ``https`` and public-address rules -- that is how a deployment points a hook
  at a service on its own network on purpose. Entries are ``host``,
  ``host:port`` or ``*.example.com`` (any subdomain, not the bare domain); an
  entry without a port allows any port.

The allowlist lives in the process environment, not in the graph: anyone who
can write a hook can write to the graph, and the thing being limited is what
those people can make the bridge talk to.

Known limit, stated rather than hidden: the check resolves the host and the
HTTP client resolves it again when it connects, so a hostile DNS server could
answer differently the second time (DNS rebinding). Redirects are not
followed, which closes the more common route to the same place, but pinning the
connection to the address that was checked would be the complete fix and is
not done here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = [
    "EgressPolicy",
    "EndpointRefused",
    "check_endpoint",
    "parse_allowlist",
    "parse_endpoint",
]

Resolver = Callable[[str, int], Awaitable[list[str]]]


class EndpointRefused(ValueError):
    """An endpoint the bridge will not send to. The message says why and is
    safe to show the caller."""


@dataclass(frozen=True)
class EgressPolicy:
    allowlist: tuple[str, ...] = ()
    #: The operator setting this policy's allowlist comes from, and what to call
    #: it in messages. Webhooks and agent endpoints share the mechanism but not
    #: the list, and a refusal must point at the setting that would change it.
    setting: str = "WEBHOOK_ALLOWLIST"
    label: str = "webhook allowlist"


def parse_allowlist(raw: str | None) -> tuple[str, ...]:
    """``"hooks.example.com, *.corp.example:8443"`` -> normalised entries."""
    if not raw:
        return ()
    entries = (item.strip().lower() for item in raw.replace("\n", ",").split(","))
    return tuple(entry for entry in entries if entry)


def _split_host_port(entry: str) -> tuple[str, int | None]:
    if entry.startswith("["):  # [::1]:9000
        host, _, rest = entry[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else None
    if entry.count(":") == 1:
        host, _, port = entry.partition(":")
        return host, int(port) if port.isdigit() else None
    return entry, None  # bare host, or an unbracketed IPv6 literal


def _entry_matches(entry: str, host: str, port: int) -> bool:
    entry_host, entry_port = _split_host_port(entry)
    if entry_port is not None and entry_port != port:
        return False
    if entry_host.startswith("*."):
        return host.endswith(entry_host[1:]) and host != entry_host[2:]
    return host == entry_host


def parse_endpoint(url: str) -> tuple[str, str, int]:
    """Syntax checks that need no network. Returns ``(scheme, host, port)``."""
    text = (url or "").strip()
    if not text:
        raise EndpointRefused("no endpoint given")
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise EndpointRefused("endpoint contains whitespace or control characters")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise EndpointRefused(f"endpoint is not a valid URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise EndpointRefused(f"endpoint scheme must be http or https, not {scheme or 'missing'!r}")
    if parts.username or parts.password:
        raise EndpointRefused("endpoint must not carry credentials in the URL")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise EndpointRefused("endpoint has no host")
    return scheme, host, port or (443 if scheme == "https" else 80)


def _non_public(address: str) -> str | None:
    """Why ``address`` is not a public one, or ``None`` if it is."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return f"{address!r} is not an IP address"
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_global:
        return None
    if ip.is_loopback:
        return f"{ip} is a loopback address"
    if ip.is_link_local:
        return f"{ip} is a link-local address"
    if ip.is_private:
        return f"{ip} is a private address"
    return f"{ip} is not a public address"


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EndpointRefused(f"{host} does not resolve") from exc
    return sorted({str(info[4][0]) for info in infos})


async def check_endpoint(
    url: str, policy: EgressPolicy, *, resolver: Resolver | None = None
) -> None:
    """Raise :class:`EndpointRefused` unless the bridge may send to ``url``."""
    scheme, host, port = parse_endpoint(url)

    if policy.allowlist:
        if any(_entry_matches(entry, host, port) for entry in policy.allowlist):
            return
        raise EndpointRefused(f"{host}:{port} is not on the {policy.label}")

    if scheme != "https":
        raise EndpointRefused(
            f"endpoint must use https (list the host in {policy.setting} to allow plain http)"
        )

    literal = host
    try:
        ipaddress.ip_address(literal.split("%", 1)[0])
        addresses = [literal]
    except ValueError:
        addresses = await (resolver or _resolve)(host, port)

    if not addresses:
        raise EndpointRefused(f"{host} does not resolve")
    for address in addresses:
        reason = _non_public(address)
        if reason:
            raise EndpointRefused(
                f"{host} resolves to a non-public address ({reason}); "
                f"list the host in {policy.setting} to allow it deliberately"
            )
