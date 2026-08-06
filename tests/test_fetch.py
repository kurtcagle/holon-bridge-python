from __future__ import annotations

import httpx
import pytest

from holonbridge.fetch import SourceFetchError, fetch_source


@pytest.mark.asyncio
async def test_fetch_source_returns_text_and_content_type(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(
            200,
            text="<urn:a> <urn:b> <urn:c> .",
            headers={"content-type": "text/turtle"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    text, content_type = await fetch_source("https://example.org/data.ttl")
    assert text == "<urn:a> <urn:b> <urn:c> ."
    assert content_type == "text/turtle"


@pytest.mark.asyncio
async def test_fetch_source_raises_on_http_error(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(SourceFetchError, match="HTTP 404"):
        await fetch_source("https://example.org/gone.ttl")


@pytest.mark.asyncio
async def test_fetch_source_wraps_timeout(monkeypatch):
    async def fake_get(self, url, **kwargs):
        raise httpx.TimeoutException("boom")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(SourceFetchError, match="timed out"):
        await fetch_source("https://example.org/slow.ttl", timeout=1.0)


@pytest.mark.asyncio
async def test_fetch_source_wraps_generic_httpx_errors(monkeypatch):
    async def fake_get(self, url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    with pytest.raises(SourceFetchError, match="refused"):
        await fetch_source("https://example.org/down.ttl")
