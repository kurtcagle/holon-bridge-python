"""Tests for the media fetch (``holonbridge/media.py`` and ``routes/media.py``).

The point of the endpoint is to let a sandboxed client show an image the
bridge fetched for it -- which also makes it an outbound request the *caller*
chooses, so most of what is tested here is what it refuses: private and
loopback addresses (including IPv6 forms that merely carry an IPv4 address),
redirects that hop somewhere internal, credentials in the URL, odd ports, and
anything that is not actually an image whatever it claims to be.

Nothing here touches the network. DNS is an injected resolver; the HTTP
exchange is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from holonbridge.deps import require_animus
from holonbridge.media import (
    MediaFetchConfig,
    MediaFetchError,
    fetch_image,
    is_public_address,
    sniff_image_type,
)
from holonbridge.routes import media as media_routes

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'

PUBLIC = "93.184.216.34"  # example.com's long-standing address, a global unicast one


def resolver_for(mapping: dict[str, list[str]]):
    async def resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise MediaFetchError(502, f"could not resolve {host}")
        return mapping[host]

    return resolve


def transport_for(handler):
    return httpx.MockTransport(handler)


def ok(body: bytes = PNG, content_type: str = "image/png", **headers) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": content_type, **headers})


CFG = MediaFetchConfig()


async def fetch(url, handler, *, resolver=None, config=CFG, **kw):
    return await fetch_image(
        url,
        config=config,
        resolver=resolver or resolver_for({"img.example.org": [PUBLIC]}),
        transport=transport_for(handler),
        **kw,
    )


# --- address policy ---------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "2002:7f00:0001::",  # 6to4 wrapping 127.0.0.1
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "not-an-address",
        "fe80::1%eth0",  # scoped
    ],
)
def test_non_public_addresses_are_refused(address):
    assert is_public_address(address) is False


@pytest.mark.parametrize(
    "address",
    ["93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"],
)
def test_public_addresses_are_allowed(address):
    assert is_public_address(address) is True


# --- the happy path ---------------------------------------------------------


async def test_fetches_and_returns_base64():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["host"] = request.headers["host"]
        return ok(PNG)

    result = await fetch("https://img.example.org/a/b.png", handler)
    assert result.content_type == "image/png"
    assert result.size_bytes == len(PNG)
    assert base64.b64decode(result.as_dict()["data_base64"]) == PNG
    assert result.final_url == "https://img.example.org/a/b.png"


async def test_connection_is_pinned_to_the_validated_address():
    """The bytes must come from the address that was checked, not from a
    second resolution of the name -- and the origin still sees its own name."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["connect_host"] = request.url.host
        seen["host_header"] = request.headers["host"]
        seen["path"] = request.url.path
        return ok()

    await fetch("https://img.example.org/p/x.png", handler)
    assert seen == {
        "connect_host": PUBLIC,
        "host_header": "img.example.org",
        "path": "/p/x.png",
    }


async def test_non_default_port_is_kept_in_the_host_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host_header"] = request.headers["host"]
        return ok()

    await fetch("https://img.example.org:8443/x.png", handler)
    assert seen["host_header"] == "img.example.org:8443"


async def test_content_type_is_taken_from_the_bytes_not_the_header():
    result = await fetch("https://img.example.org/x", lambda r: ok(JPEG, "image/png"))
    assert result.content_type == "image/jpeg"


@pytest.mark.parametrize(
    ("body", "declared", "expected"),
    [
        (PNG, "image/png", "image/png"),
        (JPEG, "image/jpeg", "image/jpeg"),
        (GIF, "image/gif", "image/gif"),
        (WEBP, "image/webp", "image/webp"),
        (SVG, "image/svg+xml", "image/svg+xml"),
    ],
)
async def test_each_supported_format_round_trips(body, declared, expected):
    result = await fetch("https://img.example.org/x", lambda r: ok(body, declared))
    assert result.content_type == expected


async def test_an_ip_literal_url_is_fetched_without_a_second_resolution():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        return ok()

    async def explode(host, port):  # a literal must never be resolved
        raise AssertionError("resolver was called for an IP literal")

    await fetch_image(
        f"https://{PUBLIC}/x.png", config=CFG, resolver=explode, transport=transport_for(handler)
    )
    assert seen["host"] == PUBLIC


# --- what it refuses: the URL itself ---------------------------------------


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("file:///etc/passwd", 400),
        ("ftp://img.example.org/a.png", 400),
        ("gopher://img.example.org/", 400),
        ("data:image/png;base64,AAAA", 400),
        ("javascript:alert(1)", 400),
        ("//img.example.org/a.png", 400),
        ("https://user:pw@img.example.org/a.png", 400),
        ("https://user@img.example.org/a.png", 400),
        ("https:///a.png", 400),
        ("https://img.example.org:99999/a.png", 400),
        ("https://img.example.org:22/a.png", 403),
        ("https://img.example.org:3030/a.png", 403),
        ("http://img.example.org:6379/a.png", 403),
        ("https://" + "a" * 3000 + ".example.org/", 400),
    ],
)
async def test_bad_urls_are_refused_before_any_request(url, status):
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("a request was made for a refused URL")

    with pytest.raises(MediaFetchError) as err:
        await fetch(url, handler)
    assert err.value.status == status


# --- what it refuses: where the name points --------------------------------


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "192.168.0.10", "169.254.169.254", "::1", "::ffff:127.0.0.1"],
)
async def test_a_name_resolving_to_a_private_address_is_refused(address):
    def handler(request):  # pragma: no cover
        raise AssertionError("connected to a non-public address")

    with pytest.raises(MediaFetchError) as err:
        await fetch(
            "https://internal.example.org/a.png",
            handler,
            resolver=resolver_for({"internal.example.org": [address]}),
        )
    assert err.value.status == 403
    assert "non-public" in err.value.message


async def test_one_bad_address_among_good_ones_refuses_the_whole_name():
    """A name that resolves to a mix of public and private addresses is the
    shape of a rebinding attempt; refuse it rather than pick the good one."""

    def handler(request):  # pragma: no cover
        raise AssertionError("fetched despite a private address in the answer")

    with pytest.raises(MediaFetchError) as err:
        await fetch(
            "https://mixed.example.org/a.png",
            handler,
            resolver=resolver_for({"mixed.example.org": [PUBLIC, "10.0.0.1"]}),
        )
    assert err.value.status == 403


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/a.png",
        "http://[::ffff:7f00:1]/a.png",
        "http://10.0.0.1:8080/a.png",
    ],
)
async def test_private_ip_literals_are_refused(url):
    def handler(request):  # pragma: no cover
        raise AssertionError("connected to a non-public address")

    with pytest.raises(MediaFetchError) as err:
        await fetch(url, handler)
    assert err.value.status == 403


async def test_allow_private_permits_an_internal_host_when_the_operator_says_so():
    cfg = MediaFetchConfig(allow_private=True)
    result = await fetch(
        "https://media.corp.example/a.png",
        lambda r: ok(),
        resolver=resolver_for({"media.corp.example": ["10.0.0.7"]}),
        config=cfg,
    )
    assert result.content_type == "image/png"


async def test_allow_list_restricts_hosts_and_admits_subdomains():
    cfg = MediaFetchConfig(allowed_hosts=frozenset({"cdn.example.org"}))
    resolver = resolver_for(
        {"cdn.example.org": [PUBLIC], "a.cdn.example.org": [PUBLIC], "evil.example.org": [PUBLIC]}
    )
    assert await fetch("https://cdn.example.org/x", lambda r: ok(), resolver=resolver, config=cfg)
    assert await fetch("https://a.cdn.example.org/x", lambda r: ok(), resolver=resolver, config=cfg)
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://evil.example.org/x", lambda r: ok(), resolver=resolver, config=cfg)
    assert err.value.status == 403


async def test_allow_list_does_not_match_a_lookalike_suffix():
    cfg = MediaFetchConfig(allowed_hosts=frozenset({"cdn.example.org"}))
    resolver = resolver_for({"notcdn.example.org": [PUBLIC]})
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://notcdn.example.org/x", lambda r: ok(), resolver=resolver, config=cfg)
    assert err.value.status == 403


async def test_unresolvable_host_is_a_502():
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://nowhere.example.org/x", lambda r: ok())
    assert err.value.status == 502


# --- redirects --------------------------------------------------------------


async def test_a_redirect_is_followed_and_revalidated():
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(request.headers["host"])
        if request.headers["host"] == "img.example.org":
            return httpx.Response(302, headers={"location": "https://cdn.example.org/real.png"})
        return ok()

    result = await fetch(
        "https://img.example.org/short",
        handler,
        resolver=resolver_for({"img.example.org": [PUBLIC], "cdn.example.org": [PUBLIC]}),
    )
    assert hops == ["img.example.org", "cdn.example.org"]
    assert result.url == "https://img.example.org/short"
    assert result.final_url == "https://cdn.example.org/real.png"


async def test_a_relative_redirect_resolves_against_the_current_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a/start":
            return httpx.Response(301, headers={"location": "../b/final.png"})
        return ok()

    result = await fetch("https://img.example.org/a/start", handler)
    assert result.final_url == "https://img.example.org/b/final.png"


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/admin",
        "http://[::1]/",
        "file:///etc/passwd",
        "https://user:pw@img.example.org/x.png",
    ],
)
async def test_a_redirect_into_somewhere_forbidden_is_refused_at_the_hop(target):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/redirector", handler)
    assert err.value.status in (400, 403)
    assert len(calls) == 1, "the forbidden hop must never be requested"


async def test_a_redirect_to_a_name_that_resolves_privately_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://sneaky.example.org/x.png"})

    with pytest.raises(MediaFetchError) as err:
        await fetch(
            "https://img.example.org/start",
            handler,
            resolver=resolver_for(
                {"img.example.org": [PUBLIC], "sneaky.example.org": ["10.0.0.9"]}
            ),
        )
    assert err.value.status == 403


async def test_a_redirect_loop_is_cut_off():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://img.example.org/again"})

    with pytest.raises(MediaFetchError, match="too many redirects") as err:
        await fetch("https://img.example.org/start", handler)
    assert err.value.status == 502


async def test_a_redirect_with_no_location_is_a_502():
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", lambda r: httpx.Response(302))
    assert err.value.status == 502


# --- what it refuses: the response -----------------------------------------


async def test_a_non_image_content_type_is_a_415():
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", lambda r: ok(b"<html></html>", "text/html"))
    assert err.value.status == 415


async def test_an_image_content_type_with_non_image_bytes_is_a_415():
    """The declared type is never trusted on its own."""
    with pytest.raises(MediaFetchError) as err:
        await fetch(
            "https://img.example.org/x",
            lambda r: ok(b"<html><script>x</script></html>", "image/png"),
        )
    assert err.value.status == 415
    assert "not a recognised image" in err.value.message


async def test_svg_needs_both_the_declared_type_and_svg_content():
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", lambda r: ok(SVG, "image/png"))
    assert err.value.status == 415
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", lambda r: ok(b"<p>hi</p>", "image/svg+xml"))
    assert err.value.status == 415


async def test_a_missing_content_type_is_a_415():
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", lambda r: httpx.Response(200, content=PNG))
    assert err.value.status == 415


@pytest.mark.parametrize("code", [403, 404, 500, 503])
async def test_an_upstream_error_status_is_a_502(code):
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", lambda r: httpx.Response(code))
    assert err.value.status == 502
    assert str(code) in err.value.message


# --- size -------------------------------------------------------------------


async def test_a_declared_content_length_over_the_cap_is_refused_early():
    cfg = MediaFetchConfig(max_bytes=100)
    with pytest.raises(MediaFetchError) as err:
        await fetch(
            "https://img.example.org/x",
            lambda r: ok(PNG, **{"content-length": "999999"}),
            config=cfg,
        )
    assert err.value.status == 413


async def test_a_body_that_outgrows_its_declared_length_is_cut_off_while_streaming():
    cfg = MediaFetchConfig(max_bytes=100)
    big = PNG + b"\x00" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, content=big, headers={"content-type": "image/png"})
        response.headers.pop("content-length", None)  # a server may simply not say
        return response

    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", handler, config=cfg)
    assert err.value.status == 413


async def test_the_caller_can_lower_the_cap_but_not_raise_it():
    cfg = MediaFetchConfig(max_bytes=50)
    body = PNG + b"\x00" * 100  # 140 bytes
    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", lambda r: ok(body), config=cfg, max_bytes=10_000)
    assert err.value.status == 413
    with pytest.raises(MediaFetchError) as err:
        await fetch(
            "https://img.example.org/x",
            lambda r: ok(PNG),
            config=MediaFetchConfig(max_bytes=10_000),
            max_bytes=10,
        )
    assert err.value.status == 413


# --- transport failures -----------------------------------------------------


async def test_a_timeout_is_a_504():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", handler)
    assert err.value.status == 504


async def test_a_connection_failure_is_a_502_that_does_not_leak_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "[Errno 111] connect to 93.184.216.34:443 refused", request=request
        )

    with pytest.raises(MediaFetchError) as err:
        await fetch("https://img.example.org/x", handler)
    assert err.value.status == 502
    assert "93.184" not in err.value.message  # no resolved addresses in the reply


# --- sniffing ---------------------------------------------------------------


def test_sniffing_recognises_each_format_and_rejects_the_rest():
    assert sniff_image_type(PNG) == "image/png"
    assert sniff_image_type(JPEG) == "image/jpeg"
    assert sniff_image_type(GIF) == "image/gif"
    assert sniff_image_type(WEBP) == "image/webp"
    assert sniff_image_type(b"\x00\x00\x00\x1cftypavif" + b"\x00" * 8) == "image/avif"
    assert sniff_image_type(b"\x00\x00\x01\x00" + b"\x00" * 8) == "image/x-icon"
    assert sniff_image_type(b"BM" + b"\x00" * 20, "image/bmp") == "image/bmp"
    assert sniff_image_type(b"BM" + b"\x00" * 20, "image/png") is None  # weak signature
    assert sniff_image_type(SVG, "image/svg+xml") == "image/svg+xml"
    assert sniff_image_type(b"\xef\xbb\xbf" + SVG, "image/svg+xml") == "image/svg+xml"
    assert sniff_image_type(b"%PDF-1.7") is None
    assert sniff_image_type(b"") is None


# --- configuration ----------------------------------------------------------


def test_config_defaults(monkeypatch):
    for key in (
        "MEDIA_FETCH_MAX_BYTES",
        "MEDIA_FETCH_TIMEOUT",
        "MEDIA_FETCH_ALLOWED_HOSTS",
        "MEDIA_FETCH_ALLOW_PRIVATE",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = MediaFetchConfig.from_env()
    assert cfg.max_bytes == 6 * 1024 * 1024
    assert cfg.timeout == 15.0
    assert cfg.allow_private is False
    assert cfg.allowed_hosts == frozenset()


def test_config_from_environment(monkeypatch):
    monkeypatch.setenv("MEDIA_FETCH_MAX_BYTES", "1000")
    monkeypatch.setenv("MEDIA_FETCH_TIMEOUT", "3.5")
    monkeypatch.setenv("MEDIA_FETCH_ALLOW_PRIVATE", "Yes")
    monkeypatch.setenv("MEDIA_FETCH_ALLOWED_HOSTS", " CDN.Example.org , .img.example.org ,,")
    cfg = MediaFetchConfig.from_env()
    assert (cfg.max_bytes, cfg.timeout, cfg.allow_private) == (1000, 3.5, True)
    assert cfg.allowed_hosts == frozenset({"cdn.example.org", "img.example.org"})


def test_config_ceiling_and_garbage_fall_back_safely(monkeypatch):
    monkeypatch.setenv("MEDIA_FETCH_MAX_BYTES", str(10**12))
    assert MediaFetchConfig.from_env().max_bytes == 16 * 1024 * 1024
    monkeypatch.setenv("MEDIA_FETCH_MAX_BYTES", "lots")
    monkeypatch.setenv("MEDIA_FETCH_TIMEOUT", "-4")
    cfg = MediaFetchConfig.from_env()
    assert (cfg.max_bytes, cfg.timeout) == (6 * 1024 * 1024, 15.0)


# --- the route --------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(media_routes.router)
    app.dependency_overrides[require_animus] = lambda: object()
    audit_calls: list[dict] = []
    monkeypatch.setattr(media_routes, "annotate", lambda **kw: audit_calls.append(kw))
    c = TestClient(app, raise_server_exceptions=False)
    c.audit_calls = audit_calls
    return c


def test_route_returns_the_image(client, monkeypatch):
    async def fake_fetch(url, *, max_bytes=None, config=None):
        assert url == "https://img.example.org/a.png"
        assert max_bytes == 1234
        from holonbridge.media import MediaResult

        return MediaResult(
            url=url, final_url=url, content_type="image/png", size_bytes=3, data=b"abc"
        )

    monkeypatch.setattr(media_routes, "fetch_image", fake_fetch)
    response = client.get(
        "/media/fetch", params={"url": "https://img.example.org/a.png", "max_bytes": 1234}
    )
    assert response.status_code == 200
    assert response.json() == {
        "url": "https://img.example.org/a.png",
        "final_url": "https://img.example.org/a.png",
        "content_type": "image/png",
        "size_bytes": 3,
        "data_base64": "YWJj",
    }


@pytest.mark.parametrize("status", [400, 403, 413, 415, 502, 504])
def test_route_maps_errors_to_their_status_with_a_plain_string_detail(client, monkeypatch, status):
    async def refuse(url, *, max_bytes=None, config=None):
        raise MediaFetchError(status, "a readable reason")

    monkeypatch.setattr(media_routes, "fetch_image", refuse)
    response = client.get("/media/fetch", params={"url": "https://x.example.org/a.png"})
    assert response.status_code == status
    assert response.json() == {"detail": "a readable reason"}


def test_only_policy_refusals_are_annotated_for_the_audit_log(client, monkeypatch):
    async def refuse_403(url, *, max_bytes=None, config=None):
        raise MediaFetchError(403, "host h resolves to a non-public address; refused")

    monkeypatch.setattr(media_routes, "fetch_image", refuse_403)
    client.get("/media/fetch", params={"url": "http://h.example.org/a.png"})
    assert client.audit_calls == [
        {"reason": "media fetch refused: host h resolves to a non-public address; refused"}
    ]

    client.audit_calls.clear()

    async def refuse_415(url, *, max_bytes=None, config=None):
        raise MediaFetchError(415, "not an image")

    monkeypatch.setattr(media_routes, "fetch_image", refuse_415)
    client.get("/media/fetch", params={"url": "http://h.example.org/a.png"})
    assert client.audit_calls == []


def test_route_requires_the_url_and_a_positive_max_bytes(client):
    assert client.get("/media/fetch").status_code == 422
    assert (
        client.get("/media/fetch", params={"url": "https://x/a.png", "max_bytes": 0}).status_code
        == 422
    )
    assert (
        client.get(
            "/media/fetch", params={"url": "https://x/a.png", "max_bytes": "big"}
        ).status_code
        == 422
    )


def test_route_rejects_an_oversized_url_before_fetching(client):
    response = client.get("/media/fetch", params={"url": "https://x.example.org/" + "a" * 3000})
    assert response.status_code == 400
    assert "longer than" in response.json()["detail"]


def test_route_is_a_get_so_the_audit_policy_does_not_count_it_as_a_mutation():
    """A POST would be audited as a write on every image render. This pins
    the choice: if someone changes the verb, this test says why not to."""
    assert [r.methods for r in media_routes.router.routes if r.path == "/media/fetch"] == [{"GET"}]


# --- the pinned connection, against a real socket ---------------------------


async def test_pinned_connection_really_reaches_the_validated_address(monkeypatch):
    """MockTransport proves what we *ask* httpx to do; this proves httpx
    really does it: a server on 127.0.0.1 is reached by a name that only the
    injected resolver knows, and sees that name -- with the port -- as Host."""
    import asyncio

    from holonbridge import media

    seen: dict = {}

    async def serve(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        for line in head.decode("latin-1").split("\r\n"):
            if line.lower().startswith("host:"):
                seen["host"] = line.split(":", 1)[1].strip()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
            + f"Content-Length: {len(PNG)}\r\nConnection: close\r\n\r\n".encode()
            + PNG
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(media, "ALLOWED_PORTS", frozenset({port}))
    try:
        result = await fetch_image(
            f"http://img.test:{port}/x.png",
            config=MediaFetchConfig(allow_private=True),
            resolver=resolver_for({"img.test": ["127.0.0.1"]}),
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result.content_type == "image/png"
    assert seen["host"] == f"img.test:{port}"
