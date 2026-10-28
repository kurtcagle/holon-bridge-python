"""Plain-language messages on MCP error results (holonbridge_mcp/friendly.py).

Each case pins one real wording from the bridge (deps.py, acl.py, the routes) to
the sentence a person sees, and checks ``_call`` adds ``message`` without
changing ``status`` or ``detail``.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from holonbridge_mcp import session
from holonbridge_mcp.friendly import plain_message


@pytest.mark.parametrize(
    "status, path, detail, phrase",
    [
        (401, "/sparql/select", "invalid bearer token", "connect it again"),
        (401, "/sparql/select", "missing bearer token", "connect it again"),
        (401, "/sparql/select", "no Person found for GitHubIdentity identifier 'x'", "no account for you"),
        (403, "/sparql/select", "this token is bound to a different identity than the x-holon-animus-id header names", "another user"),
        (403, "/sparql/select", "query reaches graphs it does not name (GRAPH ?var, SERVICE ?var, ALL/NAMED); name each graph explicitly, or ask an admin", "Name each graph"),
        (403, "/sparql/select", "no ReadGrant covering urn:x (persona p): it is another", "permission to read"),
        (403, "/graph/push", "writes are never role-based; no grantsWrite on the scratch area", "scratch area"),
        (403, "/graph/replace", "replace is stricter than write; no explicit grantsReplace found (holding grantsWrite does not imply it)", "not replace"),
        (403, "/sparql/select", "the audit graph is readable by admins only", "administrator"),
        (403, "/anything", "something new", "permission"),
        (400, "/sparql/update", "this is a read query; use /sparql/select or /sparql/construct", "wrong kind of query"),
        (400, "/validate", "delta mode requires target_graph", "target_graph"),
        (400, "/validate", "shapes graph urn:ggsc:shacl does not exist", "shapes graph"),
        (400, "/graph/push", "bad turtle", "not understood"),
        (404, "/graph", "nope", "not found"),
        (405, "/validate", "Method Not Allowed", "does not accept"),
        (409, "/graph/push", "conflict", "same time"),
        (422, "/validate", [{"loc": ["body"], "msg": "field required"}], "could not read"),
        (422, "/graph/push", [{"msg": "field required"}], "required field"),
        (500, "/validate", "Internal Server Error", "RDF 1.2"),
        (500, "/sparql/select", "boom", "internal error"),
        (503, "/sparql/select", "down", "graph store"),
    ],
)
def test_known_failures_get_a_sentence(status, path, detail, phrase):
    assert phrase.lower() in plain_message(status, path, detail).lower()


def test_unknown_success_class_status_gets_no_message():
    assert plain_message(302, "/x", "") is None


def test_detail_shapes_do_not_raise():
    for detail in (None, "", {"k": "v"}, ["a"], 3):
        plain_message(403, "/x", detail)


def _patched_client(monkeypatch, response):
    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            return response

    monkeypatch.setattr(session.httpx, "AsyncClient", Client)


def test_call_adds_message_and_keeps_status_and_detail(monkeypatch):
    _patched_client(monkeypatch, httpx.Response(403, json={"k": "no ReadGrant covering urn:x"}))
    out = asyncio.run(session._call("GET", "/graph"))
    assert out["error"] is True and out["status"] == 403
    assert out["detail"] == {"k": "no ReadGrant covering urn:x"}
    assert "permission to read" in out["message"]


def test_call_success_is_untouched(monkeypatch):
    _patched_client(monkeypatch, httpx.Response(200, json={"ok": True}))
    assert asyncio.run(session._call("GET", "/graph")) == {"ok": True}
