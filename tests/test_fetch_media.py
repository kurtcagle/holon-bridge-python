"""``fetch_media`` / ``POST /media/fetch``: images, audio and video through the ingest egress policy.

Fetching a URL the caller chose is an outbound request, so what matters here is what it will NOT do:
reach a private address (on the URL or after a redirect), return something that is not media, or
read without bound -- and that a refusal is decided before the body is read. Transport and name
resolution are faked, so nothing touches a network.
"""

from __future__ import annotations

import base64

import httpx
import pytest
from fastapi.testclient import TestClient
from rdflib import URIRef

from holonbridge import config
from holonbridge.config import Settings
from holonbridge.egress import EgressPolicy
from holonbridge.fetch import SourceFetchError, fetch_media
from holonbridge.persona_state import PersonaStore
from holonbridge.routes import media as media_route
from holonbridge.server import create_app

from .test_identity_admin import FIXTURE_TURTLE, HOLONS_GRAPH, TOKEN, GraphStoreStub

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PUBLIC = "93.184.216.34"
DEFAULT = EgressPolicy(setting="FETCH_ALLOWLIST", label="fetch allowlist")


async def public(host: str, port: int) -> list[str]:
    return [PUBLIC]


def resolving(table: dict[str, str]):
    async def resolve(host: str, port: int) -> list[str]:
        return [table.get(host, PUBLIC)]

    return resolve


class Server:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[str] = []
        self.bodies_read = 0

    def transport(self) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            return self.handler(request)

        return httpx.MockTransport(respond)


def serving(body: bytes = PNG, content_type: str = "image/png", status: int = 200) -> Server:
    return Server(lambda r: httpx.Response(status, content=body, headers={"content-type": content_type}))


async def get(url: str, server: Server, **kwargs):
    kwargs.setdefault("policy", DEFAULT)
    kwargs.setdefault("resolver", public)
    return await fetch_media(url, transport=server.transport(), **kwargs)


# --- the ordinary case -----------------------------------------------------------------------


async def test_returns_the_bytes_and_a_clean_media_type():
    server = serving(content_type="Image/PNG; charset=binary")
    data, media_type = await get("https://example.org/a.png", server)
    assert data == PNG
    assert media_type == "image/png"  # lower-cased, parameters dropped
    assert server.calls == ["https://example.org/a.png"]


@pytest.mark.parametrize("ctype", ["image/svg+xml", "image/webp", "audio/mpeg", "video/mp4"])
async def test_image_audio_and_video_are_all_media(ctype):
    data, media_type = await get("https://example.org/f", serving(content_type=ctype))
    assert (data, media_type) == (PNG, ctype)


# --- only media comes back -------------------------------------------------------------------


@pytest.mark.parametrize(
    "ctype", ["text/html", "application/json", "application/pdf", "text/turtle", "application/zip", ""]
)
async def test_anything_that_is_not_media_is_refused(ctype):
    with pytest.raises(SourceFetchError, match="not an image, audio or video") as refused:
        await get("https://example.org/x", serving(content_type=ctype))
    assert refused.value.kind == "not_media"


async def test_a_wrong_type_is_refused_before_the_body_is_read():
    class Unread(httpx.AsyncByteStream):
        read = False

        async def __aiter__(self):
            Unread.read = True
            yield b"x" * 1024

    server = Server(
        lambda r: httpx.Response(200, headers={"content-type": "text/html"}, stream=Unread())
    )
    with pytest.raises(SourceFetchError, match="not an image"):
        await get("https://example.org/page", server)
    assert Unread.read is False


async def test_a_bare_type_with_no_subtype_is_not_media():
    with pytest.raises(SourceFetchError, match="not an image"):
        await get("https://example.org/x", serving(content_type="image"))


# --- the size limit --------------------------------------------------------------------------


async def test_a_declared_size_over_the_limit_is_refused():
    server = Server(
        lambda r: httpx.Response(
            200, content=PNG, headers={"content-type": "image/png", "content-length": "5000"}
        )
    )
    with pytest.raises(SourceFetchError, match="larger than the 100-byte limit") as big:
        await get("https://example.org/a.png", server, max_bytes=100)
    assert big.value.kind == "too_large"


async def test_a_body_that_grows_past_the_limit_is_abandoned():
    with pytest.raises(SourceFetchError, match="larger than") as big:
        await get("https://example.org/a.png", serving(body=b"x" * 5000), max_bytes=1000)
    assert big.value.kind == "too_large"


async def test_a_caller_can_lower_the_operators_cap_but_never_raise_it(monkeypatch):
    monkeypatch.setattr(config, "_settings", None, raising=False)
    monkeypatch.setattr(
        "holonbridge.fetch.get_settings", lambda: Settings(fetch_media_max_bytes=200)
    )
    big = serving(body=b"x" * 500)
    with pytest.raises(SourceFetchError, match="200-byte limit"):
        await get("https://example.org/a.png", big, max_bytes=10_000_000)  # asks for more: capped
    assert (await get("https://example.org/a.png", serving(body=b"x" * 50), max_bytes=100))[0]


# --- where it may go -------------------------------------------------------------------------


async def test_a_private_address_is_refused_without_a_request():
    server = serving()
    with pytest.raises(SourceFetchError, match="non-public") as refused:
        await get("https://internal.example/a.png", server, resolver=resolving({"internal.example": "10.0.0.5"}))
    assert refused.value.kind == "refused"
    assert server.calls == []


@pytest.mark.parametrize("url", ["http://example.org/a.png", "https://169.254.169.254/a.png", "https://localhost/a.png"])
async def test_plain_http_metadata_and_loopback_are_refused(url):
    server = serving()
    with pytest.raises(SourceFetchError) as refused:
        await get(url, server, resolver=resolving({"localhost": "127.0.0.1"}))
    assert refused.value.kind == "refused"
    assert server.calls == []


async def test_a_redirect_to_a_private_address_is_refused():
    def handler(request):
        if request.url.host == "example.org":
            return httpx.Response(302, headers={"location": "https://evil.example/a.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    server = Server(handler)
    with pytest.raises(SourceFetchError, match=r"refused \(after a redirect\)") as refused:
        await get("https://example.org/a.png", server, resolver=resolving({"evil.example": "10.0.0.5"}))
    assert refused.value.kind == "refused"
    assert server.calls == ["https://example.org/a.png"]  # the second hop was never sent


async def test_a_listed_host_may_be_plain_http_on_a_private_address():
    policy = EgressPolicy(allowlist=("files.internal:8080",), setting="FETCH_ALLOWLIST", label="fetch allowlist")
    server = serving()
    data, _ = await get("http://files.internal:8080/a.png", server, policy=policy, resolver=resolving({"files.internal": "10.0.0.9"}))
    assert data == PNG


async def test_an_http_error_is_a_plain_failure():
    with pytest.raises(SourceFetchError, match="HTTP 404") as missing:
        await get("https://example.org/gone.png", serving(status=404))
    assert missing.value.kind == "failed"


# --- the route ---------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    stub = GraphStoreStub()
    stub.ds.get_context(URIRef(HOLONS_GRAPH)).parse(data=FIXTURE_TURTLE, format="turtle")
    app = create_app(Settings(bearer_token=TOKEN, fuseki_dataset="example"))
    with TestClient(app) as c:
        app.state.fuseki = stub
        app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
        yield c


def who(identifier: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Holon-Animus-Id": identifier,
        "X-Holon-Animus-Type": "GitHubIdentity",
    }


JANE = who("jane-doe")  # a Person with no role and no grant on anything


def stub_fetch(monkeypatch, result=None, error=None):
    seen = {}

    async def fake(url, max_bytes=None, **_):
        seen.update(url=url, max_bytes=max_bytes)
        if error:
            raise error
        return result

    monkeypatch.setattr(media_route, "fetch_media", fake)
    return seen


def test_the_route_returns_base64_in_the_shape_the_page_reads(client, monkeypatch):
    seen = stub_fetch(monkeypatch, (PNG, "image/png"))
    r = client.post("/media/fetch", json={"url": "https://example.org/a.png#frag", "max_bytes": 1000}, headers=JANE)
    assert r.status_code == 200
    body = r.json()
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == len(PNG)
    assert base64.b64decode(body["data_base64"]) == PNG
    assert body["url"] == "https://example.org/a.png"  # fragment dropped
    assert seen == {"url": "https://example.org/a.png#frag", "max_bytes": 1000}


@pytest.mark.parametrize(
    "kind, code", [("refused", 403), ("not_media", 415), ("too_large", 413), ("failed", 502)]
)
def test_each_kind_of_refusal_has_its_own_status(client, monkeypatch, kind, code):
    stub_fetch(monkeypatch, error=SourceFetchError("https://example.org/a.png", "nope", kind))
    r = client.post("/media/fetch", json={"url": "https://example.org/a.png"}, headers=JANE)
    assert r.status_code == code
    assert "nope" in r.json()["detail"]


def test_a_missing_or_empty_url_is_a_validation_error(client):
    assert client.post("/media/fetch", json={}, headers=JANE).status_code == 422
    assert client.post("/media/fetch", json={"url": ""}, headers=JANE).status_code == 422
    assert client.post("/media/fetch", json={"url": "https://x.org/a.png", "max_bytes": 0}, headers=JANE).status_code == 422


def test_the_route_needs_a_known_person(client, monkeypatch):
    seen = stub_fetch(monkeypatch, (PNG, "image/png"))
    body = {"url": "https://example.org/a.png"}
    assert client.post("/media/fetch", json=body).status_code in (401, 403)  # no credential
    assert client.post("/media/fetch", json=body, headers=who("nobody")).status_code == 401  # no such Person
    assert seen == {}  # neither reached the fetch
