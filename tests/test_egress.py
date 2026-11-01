"""Outbound endpoint policy.

DNS is never touched: ``check_endpoint`` takes a resolver, so a hostname's
answer is whatever the test says it is.
"""

from __future__ import annotations

import pytest

from holonbridge.egress import (
    EgressPolicy,
    EndpointRefused,
    check_endpoint,
    parse_allowlist,
    parse_endpoint,
)
from holonbridge.projection import ProjectionError, ProjectionHook
from holonbridge.projection.model import Envelope
from holonbridge.projection.runner import HttpSender

OPEN = EgressPolicy()


def resolving(*addresses: str):
    async def resolver(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolver


async def refused(url: str, policy: EgressPolicy = OPEN, resolver=None) -> str:
    with pytest.raises(EndpointRefused) as exc:
        await check_endpoint(url, policy, resolver=resolver or resolving("93.184.216.34"))
    return str(exc.value)


# --- syntax -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["", "   ", "ftp://example.com/x", "file:///etc/passwd", "gopher://example.com",
     "example.com/hook", "https://", "https://user:pw@example.com/",
     "https://exa mple.com/", "https://example.com/\nX: y"],
)
async def test_malformed_or_unsafe_urls_are_refused(url):
    await refused(url)


def test_parse_endpoint_defaults_the_port_from_the_scheme():
    assert parse_endpoint("https://Example.com./x") == ("https", "example.com", 443)
    assert parse_endpoint("http://example.com/x") == ("http", "example.com", 80)
    assert parse_endpoint("https://example.com:8443/x") == ("https", "example.com", 8443)


# --- default policy: https, public addresses only -----------------------------


async def test_a_public_https_endpoint_is_allowed():
    await check_endpoint("https://hooks.example.com/in", OPEN, resolver=resolving("93.184.216.34"))


async def test_plain_http_is_refused_by_default():
    assert "https" in await refused("http://hooks.example.com/in")


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254",
     "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1"],
)
async def test_non_public_resolutions_are_refused(address):
    message = await refused("https://hooks.example.com/in", resolver=resolving(address))
    assert "non-public" in message


async def test_one_private_answer_among_public_ones_is_enough_to_refuse():
    await refused("https://hooks.example.com/in", resolver=resolving("93.184.216.34", "10.0.0.5"))


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/latest/meta-data/",
     "https://10.1.2.3:8443/"],
)
async def test_ip_literals_are_checked_without_resolving(url):
    async def never(host, port):
        raise AssertionError("an IP literal must not be resolved")

    with pytest.raises(EndpointRefused):
        await check_endpoint(url, OPEN, resolver=never)


async def test_an_unresolvable_host_is_refused():
    await refused("https://nope.invalid/", resolver=resolving())


async def test_the_refusal_points_at_the_allowlist():
    assert "WEBHOOK_ALLOWLIST" in await refused(
        "https://internal.corp/", resolver=resolving("10.0.0.5")
    )


# --- allowlist ----------------------------------------------------------------


def test_parse_allowlist_normalises():
    assert parse_allowlist(None) == ()
    assert parse_allowlist(" Hooks.Example.com , *.corp.example:8443\n,") == (
        "hooks.example.com",
        "*.corp.example:8443",
    )


async def test_an_allowlisted_host_is_exempt_from_the_https_and_address_rules():
    policy = EgressPolicy(("n8n.internal:5678",))
    await check_endpoint("http://n8n.internal:5678/webhook/x", policy, resolver=resolving("10.0.0.5"))


async def test_a_host_off_the_list_is_refused_even_if_public():
    policy = EgressPolicy(("hooks.example.com",))
    assert "allowlist" in await refused("https://other.example.com/", policy)


async def test_an_entry_with_a_port_only_allows_that_port():
    policy = EgressPolicy(("hooks.example.com:8443",))
    await check_endpoint("https://hooks.example.com:8443/x", policy)
    await refused("https://hooks.example.com/x", policy)


async def test_a_wildcard_allows_subdomains_but_not_the_bare_domain():
    policy = EgressPolicy(("*.example.com",))
    await check_endpoint("https://a.b.example.com/x", policy)
    await refused("https://example.com/x", policy)
    await refused("https://evilexample.com/x", policy)


async def test_a_bracketed_ipv6_entry_matches():
    await check_endpoint("http://[::1]:9000/x", EgressPolicy(("[::1]:9000",)))


# --- the sender ---------------------------------------------------------------


def hook(endpoint: str) -> ProjectionHook:
    return ProjectionHook(
        id="h", iri="urn:h", target="t", construct="CONSTRUCT {} WHERE {}",
        change_mode="append", delivery="webhook", endpoint=endpoint,
    )


def envelope() -> Envelope:
    return Envelope(
        delivery_id="d", hook_id="h", target="t", dataset="ds", change_mode="append",
        sequence=1, additions="", retractions="", addition_count=0, retraction_count=0,
    )


async def test_the_sender_refuses_a_forbidden_endpoint_before_connecting():
    sender = HttpSender(policy=OPEN)
    with pytest.raises(ProjectionError) as exc:
        await sender.send(hook("https://169.254.169.254/latest/meta-data/"), envelope())
    assert "endpoint refused" in str(exc.value)


async def test_the_sender_reads_the_operators_allowlist_at_send_time(monkeypatch):
    from holonbridge import config

    seen = {}

    async def fake_check(url, policy, **kw):
        seen["allowlist"] = policy.allowlist
        raise EndpointRefused("stop here")

    monkeypatch.setattr("holonbridge.projection.runner.check_endpoint", fake_check)
    monkeypatch.setattr(
        config, "get_settings", lambda: config.Settings(webhook_allowlist=("a.example",))
    )
    with pytest.raises(ProjectionError):
        await HttpSender().send(hook("https://a.example/x"), envelope())
    assert seen["allowlist"] == ("a.example",)
