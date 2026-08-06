"""Dataset listing tests.

Fuseki's admin API is stubbed at the client boundary. The shape it returns
is its own -- path-style names like "/ds", a services array -- and normalising
that at the edge rather than in each caller is part of what's under test.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from holonbridge.config import Settings
from holonbridge.fuseki import FusekiError
from holonbridge.server import create_app

TOKEN = "test-token"

FUSEKI_RAW = {
    "datasets": [
        {
            "ds.name": "/ds",
            "ds.state": True,
            "ds.services": [
                {"srv.type": "query"},
                {"srv.type": "update"},
                {"srv.type": "gsp-rw"},
            ],
        },
        {"ds.name": "/bridgerton", "ds.state": True, "ds.services": [{"srv.type": "query"}]},
        {"ds.name": "/worldtest", "ds.state": False, "ds.services": []},
    ]
}


class DatasetStub:
    def __init__(self) -> None:
        self.raw = FUSEKI_RAW
        self.error: Exception | None = None
        self.requested_urls: list[str] = []

    async def list_datasets(self, conn):
        if self.error is not None:
            raise self.error

        self.requested_urls.append(f"{conn.base_url}/$/datasets")
        out = []
        for entry in self.raw.get("datasets", []):
            name = str(entry.get("ds.name", "")).lstrip("/")
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "state": bool(entry.get("ds.state", True)),
                    "services": sorted(
                        s.get("srv.type", "")
                        for s in entry.get("ds.services", [])
                        if s.get("srv.type")
                    ),
                }
            )
        out.sort(key=lambda d: d["name"])
        return out

    async def select(self, conn, query, *, default_graph=None):
        return {"results": {"bindings": []}}

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        return ""

    async def update(self, conn, update):
        return None

    async def get_graph(self, conn, graph_iri):
        return ""

    async def post_graph(self, conn, graph_iri, turtle):
        return None

    async def put_graph(self, conn, graph_iri, turtle):
        return None

    async def drop_graph(self, conn, graph_iri):
        return None

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        return ""

    async def ping(self, conn):
        return True

    async def aclose(self):
        return None


@pytest.fixture
def stub() -> DatasetStub:
    return DatasetStub()


@pytest.fixture
def client(stub: DatasetStub):
    app = create_app(Settings(bearer_token=TOKEN))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        yield test_client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_lists_what_the_server_hosts(client):
    body = client.get("/datasets", headers=auth()).json()
    assert body["count"] == 3
    assert [d["name"] for d in body["datasets"]] == ["bridgerton", "ds", "worldtest"]


def test_the_path_style_name_is_normalised(client):
    """Fuseki says "/ds"; every other part of this codebase says "ds"."""
    body = client.get("/datasets", headers=auth()).json()
    assert all(not d["name"].startswith("/") for d in body["datasets"])


def test_the_active_dataset_is_marked(client):
    body = client.get("/datasets", headers=auth()).json()
    assert body["active"] == "ds"
    assert body["overridden"] is False


def test_an_offline_dataset_is_reported_not_hidden(client):
    body = client.get("/datasets", headers=auth()).json()
    worldtest = next(d for d in body["datasets"] if d["name"] == "worldtest")
    assert worldtest["state"] is False


def test_one_dataset_reports_its_graph_roles(client):
    body = client.get("/datasets/bridgerton", headers=auth()).json()
    assert body["name"] == "bridgerton"
    # the convention resolved against *that* dataset, not the active one
    assert body["graphs"]["holons"] == "urn:bridgerton:holons"
    assert body["graphs"]["shacl"] == "urn:bridgerton:shacl"


def test_an_unknown_dataset_says_what_is_available(client):
    """The typo case: a switch to a nonexistent dataset must fail loudly.

    Silently switching would produce empty reads indistinguishable from
    empty data -- a slow and confusing way to discover a spelling mistake.
    """
    response = client.get("/datasets/bridgerston", headers=auth())
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_dataset"
    assert "bridgerton" in detail["available"]


def test_a_disabled_admin_api_explains_itself(client, stub):
    stub.error = FusekiError(404, "not found", endpoint="/$/datasets")
    response = client.get("/datasets", headers=auth())
    assert response.status_code == 404
    assert "admin API" in response.json()["detail"]["hint"]


def test_datasets_require_auth(client):
    assert client.get("/datasets").status_code == 401
