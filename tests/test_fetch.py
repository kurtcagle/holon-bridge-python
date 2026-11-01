"""``source_url`` ingestion: where the bridge may fetch from, and how much.

``/ingest`` is reachable by anyone who can write a graph, and by an LLM that a
document can steer, so the GET it makes is an outbound request the caller
chooses. These pin down that it passes the same policy as a webhook -- on the
URL and on every redirect -- that it cannot be made to read without bound, and
that what it says when it refuses describes the request, not the network.

The transport is faked, and so is name resolution, so nothing here touches a
network. ``calls`` records every request that would have been sent: for a
refusal the interesting assertion is that there were none.
"""

from __future__ import annotations

import gzip

import httpx
import pytest

from holonbridge import config
from holonbridge.egress import EgressPolicy
from holonbridge.fetch import MAX_REDIRECTS, SourceFetchError, fetch_source

TURTLE = "<urn:a> <urn:b> <urn:c> ."
PUBLIC = "93.184.216.34"
#: What the operator gets by setting nothing.
DEFAULT = EgressPolicy(setting="FETCH_ALLOWLIST", label="fetch allowlist")


async def public(host: str, port: int) -> list[str]:
    return [PUBLIC]


def resolving(table: dict[str, str]):
    async def resolve(host: str, port: int) -> list[str]:
        return [table.get(host, PUBLIC)]

    return resolve


class Server:
    """A scripted transport that remembers what was requested."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            return self.handler(request)

        return httpx.MockTransport(respond)


def serving(text: str = TURTLE, content_type: str = "text/turtle", status: int = 200) -> Server:
    return Server(lambda r: httpx.Response(status, text=text, headers={"content-type": content_type}))


async def get(url: str, server: Server, **kwargs):
    kwargs.setdefault("policy", DEFAULT)
    kwargs.setdefault("resolver", public)
    return await fetch_source(url, transport=server.transport(), **kwargs)


# --- the ordinary case -----------------------------------------------------------------


async def test_returns_text_and_content_type():
    server = serving()
    text, content_type = await get("https://example.org/data.ttl", server)
    assert (text, content_type) == (TURTLE, "text/turtle")
    assert server.calls == ["https://example.org/data.ttl"]


async def test_an_http_error_is_reported_with_its_status():
    with pytest.raises(SourceFetchError, match="HTTP 404"):
        await get("https://example.org/gone.ttl", serving(status=404, text="not found"))


async def test_a_timeout_is_reported_as_one():
    def slow(request):
        raise httpx.ReadTimeout("took too long", request=request)

    with pytest.raises(SourceFetchError, match="timed out after 1s"):
        await get("https://example.org/slow.ttl", Server(slow), timeout=1.0)


async def test_a_connection_failure_names_the_kind_not_the_detail():
    def down(request):
        raise httpx.ConnectError("[Errno 111] Connection refused to 10.1.2.3:5432", request=request)

    with pytest.raises(SourceFetchError) as failed:
        await get("https://example.org/down.ttl", Server(down))
    assert "ConnectError" in str(failed.value)
    assert "10.1.2.3" not in str(failed.value) and "5432" not in str(failed.value)


async def test_the_charset_in_the_header_is_honoured():
    server = Server(
        lambda r: httpx.Response(
            200, content="<urn:a> <urn:b> \"café\" .".encode("latin-1"),
            headers={"content-type": "text/turtle; charset=iso-8859-1"},
        )
    )
    text, _ = await get("https://example.org/x.ttl", server)
    assert "café" in text


# --- where it may go -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, why",
    [
        ("http://example.org/data.ttl", "must use https"),
        ("https://127.0.0.1:3030/ds/sparql", "loopback"),
        ("https://localhost/x", "loopback"),
        ("https://[::1]/x", "loopback"),
        ("https://169.254.169.254/latest/meta-data/", "link-local"),
        ("https://10.0.0.5/x", "private"),
        ("https://192.168.1.10/x", "private"),
        ("https://user:pw@example.org/x", "credentials"),
        ("file:///etc/passwd", "scheme"),
        ("ftp://example.org/x", "scheme"),
    ],
)
async def test_the_default_policy_refuses_internal_and_odd_targets(url, why):
    server = serving()
    resolver = resolving({"localhost": "127.0.0.1"})
    with pytest.raises(SourceFetchError, match="refused") as refused:
        await get(url, server, resolver=resolver)
    assert why in str(refused.value)
    assert server.calls == []  # nothing was sent


async def test_a_name_that_resolves_to_a_private_address_is_refused():
    server = serving()
    with pytest.raises(SourceFetchError, match="non-public"):
        await get(
            "https://internal.attacker.example/x", server,
            resolver=resolving({"internal.attacker.example": "10.0.0.7"}),
        )
    assert server.calls == []


async def test_userinfo_never_appears_in_the_message():
    with pytest.raises(SourceFetchError) as refused:
        await get("https://admin:hunter2@example.org/x", serving())
    assert "hunter2" not in str(refused.value) and "admin" not in str(refused.value)


async def test_with_an_allowlist_only_listed_hosts_can_be_reached():
    policy = EgressPolicy(allowlist=("data.example.org",), setting="FETCH_ALLOWLIST", label="fetch allowlist")
    server = serving()
    assert (await get("https://data.example.org/a.ttl", server, policy=policy))[0] == TURTLE

    other = serving()
    with pytest.raises(SourceFetchError, match="not on the fetch allowlist"):
        await get("https://other.example.org/a.ttl", other, policy=policy)
    assert other.calls == []


async def test_a_listed_internal_host_is_reachable_on_purpose():
    policy = EgressPolicy(allowlist=("files.internal:8080",), setting="FETCH_ALLOWLIST", label="fetch allowlist")
    server = serving()
    text, _ = await get("http://files.internal:8080/a.ttl", server, policy=policy,
                        resolver=resolving({"files.internal": "10.0.0.9"}))
    assert text == TURTLE


async def test_the_refusal_points_at_the_setting_that_would_change_it():
    with pytest.raises(SourceFetchError, match="FETCH_ALLOWLIST"):
        await get("http://example.org/x", serving())


# --- redirects -------------------------------------------------------------------------


def redirecting(*chain: str, final: str = TURTLE) -> Server:
    hops = list(chain)

    def handler(request: httpx.Request) -> httpx.Response:
        if hops:
            return httpx.Response(302, headers={"location": hops.pop(0)})
        return httpx.Response(200, text=final, headers={"content-type": "text/turtle"})

    return Server(handler)


async def test_a_redirect_to_an_internal_address_is_refused_before_it_is_followed():
    server = redirecting("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(SourceFetchError, match=r"refused \(after a redirect\)"):
        await get("https://example.org/start", server)
    assert server.calls == ["https://example.org/start"]  # the second hop was never requested


async def test_a_redirect_to_a_name_that_resolves_internally_is_refused():
    server = redirecting("https://sneaky.example/x")
    with pytest.raises(SourceFetchError, match="non-public"):
        await get("https://example.org/start", server, resolver=resolving({"sneaky.example": "10.0.0.1"}))
    assert len(server.calls) == 1


async def test_redirects_to_acceptable_places_are_followed():
    server = redirecting("https://cdn.example.org/data.ttl")
    text, _ = await get("https://example.org/start", server)
    assert text == TURTLE
    assert server.calls == ["https://example.org/start", "https://cdn.example.org/data.ttl"]


async def test_a_relative_redirect_is_resolved_against_the_current_url():
    server = redirecting("/moved/data.ttl")
    await get("https://example.org/start", server)
    assert server.calls[-1] == "https://example.org/moved/data.ttl"


async def test_an_allowlist_applies_to_each_hop():
    policy = EgressPolicy(allowlist=("a.example.org",), setting="FETCH_ALLOWLIST", label="fetch allowlist")
    server = redirecting("https://b.example.org/x")
    with pytest.raises(SourceFetchError, match="after a redirect"):
        await get("https://a.example.org/start", server, policy=policy)


async def test_a_redirect_loop_stops():
    server = Server(lambda r: httpx.Response(302, headers={"location": "https://example.org/again"}))
    with pytest.raises(SourceFetchError, match="redirects"):
        await get("https://example.org/again", server)
    assert len(server.calls) == MAX_REDIRECTS + 1


async def test_a_redirect_with_nowhere_to_go_fails():
    server = Server(lambda r: httpx.Response(302))
    with pytest.raises(SourceFetchError, match="without a Location"):
        await get("https://example.org/x", server)


# --- how much it reads -----------------------------------------------------------------


class Endless(httpx.AsyncByteStream):
    """A body with no declared length that never ends."""

    def __init__(self) -> None:
        self.sent = 0

    async def __aiter__(self):
        while True:
            self.sent += 1024
            yield b"x" * 1024


async def test_a_declared_length_over_the_limit_is_refused_without_reading():
    server = serving(text="x" * 5000)
    with pytest.raises(SourceFetchError, match="larger than the 1000-byte limit"):
        await get("https://example.org/big", server, max_bytes=1000)


async def test_a_body_with_no_length_is_cut_off_at_the_limit():
    body = Endless()
    server = Server(lambda r: httpx.Response(200, stream=body, headers={"content-type": "text/turtle"}))
    with pytest.raises(SourceFetchError, match="larger than the 4096-byte limit"):
        await get("https://example.org/endless", server, max_bytes=4096)
    assert body.sent <= 4096 + 1024  # it stopped reading; it did not drain the stream


async def test_a_small_download_that_expands_past_the_limit_is_cut_off():
    bomb = gzip.compress(b"a" * 1_000_000)
    assert len(bomb) < 5000
    server = Server(
        lambda r: httpx.Response(
            200, content=bomb, headers={"content-encoding": "gzip", "content-type": "text/turtle"}
        )
    )
    with pytest.raises(SourceFetchError, match="larger than"):
        await get("https://example.org/bomb", server, max_bytes=10_000)


async def test_a_body_exactly_at_the_limit_is_read():
    text, _ = await get("https://example.org/x", serving(text="x" * 100), max_bytes=100)
    assert text == "x" * 100


# --- the operator's settings -----------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    config.get_settings.cache_clear()
    yield monkeypatch
    config.get_settings.cache_clear()


def test_the_settings_are_read_from_the_environment(env):
    env.setenv("FETCH_ALLOWLIST", "data.example.org, *.files.example:8443")
    env.setenv("FETCH_MAX_BYTES", "2048")
    settings = config.get_settings()
    assert settings.fetch_allowlist == ("data.example.org", "*.files.example:8443")
    assert settings.fetch_max_bytes == 2048


def test_the_defaults_are_a_default_policy_and_ten_megabytes(env):
    env.delenv("FETCH_ALLOWLIST", raising=False)
    env.delenv("FETCH_MAX_BYTES", raising=False)
    settings = config.get_settings()
    assert settings.fetch_allowlist == () and settings.fetch_max_bytes == 10 * 1024 * 1024


async def test_fetch_uses_the_operators_allowlist_and_limit_by_default(env):
    env.setenv("FETCH_ALLOWLIST", "data.example.org")
    env.setenv("FETCH_MAX_BYTES", "50")
    config.get_settings.cache_clear()

    server = serving(text="x" * 200)
    with pytest.raises(SourceFetchError, match="FETCH_ALLOWLIST|fetch allowlist"):
        await fetch_source("https://elsewhere.example/x", transport=server.transport(), resolver=public)
    assert server.calls == []

    with pytest.raises(SourceFetchError, match="50-byte limit"):
        await fetch_source("https://data.example.org/x", transport=server.transport(), resolver=public)
