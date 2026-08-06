"""Projection hook tests.

The watermark is the thing under test. Additions and retractions are both
derived from it, and it only moves when a delivery settles — which is what
makes a failed delivery re-derive the same difference next time instead of
losing it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.projection import ProjectionError, ProjectionHook, ProjectionRunner
from holonbridge.projection.vocab import scope_graph
from holonbridge.server import create_app

TOKEN = "test-token"
PROJ = "https://w3id.org/holon/projection#"

SCOPE = "CONSTRUCT { ?s ?p ?o } WHERE { GRAPH <urn:ds:holons> { ?s ?p ?o } }"


def _row(**cells: str) -> dict:
    return {key: {"value": value} for key, value in cells.items()}


def hook_rows(**overrides: str) -> list[dict]:
    iri = f"{PROJ}hook-pg"
    props = {
        "id": "pg",
        "target": "postgres://analytics",
        "construct": SCOPE,
        "changeMode": "upsert",
        "delivery": "pull",
        "keyPredicate": "urn:id",
        "hookStatus": f"{PROJ}Active",
        "sequence": "3",
    }
    props.update(overrides)
    return [_row(h=iri, p=f"{PROJ}{k}", o=v) for k, v in props.items() if v]


class ProjStub:
    """Answers delta counts from a scripted plan and records every write."""

    def __init__(self) -> None:
        self.hooks = hook_rows()
        self.additions = 5
        self.retractions = 2
        self.slice_size = 12
        self.updates: list[str] = []
        self.constructs: list[str] = []
        self.pushed: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        self.delivery_rows: list[dict] = []

    async def select(self, conn, query, *, default_graph=None):
        if "COUNT(*)" in query:
            if "NOT EXISTS" not in query:
                return {"results": {"bindings": [{"n": {"value": str(self.slice_size)}}]}}
            head = query.split("FILTER NOT EXISTS")[0]
            value = self.additions if "projection-scratch" in head else self.retractions
            return {"results": {"bindings": [{"n": {"value": str(value)}}]}}
        if "deliveryId" in query and "ORDER BY" in query:
            return {"results": {"bindings": self.delivery_rows}}
        if "urn:ds:delivery:" in query:
            return {"results": {"bindings": self.delivery_rows}}
        if "ProjectionHook" in query:
            return {"results": {"bindings": self.hooks}}
        return {"results": {"bindings": []}}

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        self.constructs.append(query)
        if "NOT EXISTS" in query:
            head = query.split("FILTER NOT EXISTS")[0]
            return (
                "<urn:a> <urn:b> <urn:new> .\n"
                if "projection-scratch" in head
                else "<urn:a> <urn:b> <urn:gone> .\n"
            )
        return "<urn:a> <urn:b> <urn:c> .\n"

    async def update(self, conn, update):
        self.updates.append(" ".join(update.split()))

    async def get_graph(self, conn, graph_iri):
        return ""

    async def post_graph(self, conn, graph_iri, turtle):
        self.pushed.append((graph_iri, turtle))

    async def put_graph(self, conn, graph_iri, turtle):
        return None

    async def drop_graph(self, conn, graph_iri):
        self.dropped.append(graph_iri)

    async def shacl_validate(self, conn, *, target_graph, shapes_turtle):
        return ""

    async def ping(self, conn):
        return True

    async def aclose(self):
        return None

    def updates_matching(self, keyword: str) -> list[str]:
        return [u for u in self.updates if u.upper().startswith(keyword.upper())]


class RecordingSender:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list = []

    async def send(self, hook, envelope):
        if self.fail:
            raise ProjectionError("target refused")
        self.sent.append(envelope)
        return "200"


def conn() -> Conn:
    return Conn(base_url="http://x", dataset="ds", overridden=False, bank_name="local")


def hook(**kwargs) -> ProjectionHook:
    base = dict(
        id="pg",
        iri=f"{PROJ}hook-pg",
        target="postgres://analytics",
        construct=SCOPE,
        change_mode="upsert",
        delivery="pull",
        key_predicate="urn:id",
    )
    base.update(kwargs)
    return ProjectionHook(**base)


@pytest.fixture
def stub() -> ProjStub:
    return ProjStub()


@pytest.fixture
def client(stub: ProjStub):
    app = create_app(Settings(bearer_token=TOKEN))
    with TestClient(app) as test_client:
        app.state.fuseki = stub
        yield test_client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# --- the delta ----------------------------------------------------------------


async def test_additions_and_retractions_are_both_derived(stub):
    delivery, envelope = await ProjectionRunner(stub).run(conn(), hook())
    assert envelope.addition_count == 5
    assert envelope.retraction_count == 2
    assert "urn:new" in envelope.additions
    assert "urn:gone" in envelope.retractions


async def test_the_delta_is_computed_server_side(stub):
    await ProjectionRunner(stub).run(conn(), hook())
    deltas = [c for c in stub.constructs if "NOT EXISTS" in c]
    assert len(deltas) == 2
    assert all("GRAPH" in c for c in deltas)


async def test_append_mode_never_sends_retractions(stub):
    _, envelope = await ProjectionRunner(stub).run(conn(), hook(change_mode="append"))
    assert envelope.addition_count == 5
    assert envelope.retraction_count == 0
    assert envelope.retractions == ""


async def test_replace_mode_sends_the_whole_slice(stub):
    _, envelope = await ProjectionRunner(stub).run(conn(), hook(change_mode="replace"))
    assert envelope.full_slice is True
    assert envelope.addition_count == 12
    assert envelope.retraction_count == 0


# --- the watermark ------------------------------------------------------------


async def test_a_successful_webhook_advances_the_watermark(stub):
    sender = RecordingSender()
    runner = ProjectionRunner(stub, sender=sender)
    delivery, _ = await runner.run(
        conn(), hook(delivery="webhook", endpoint="https://target.example/hook")
    )
    assert delivery.status == "delivered"
    copies = stub.updates_matching("COPY SILENT")
    assert copies and copies[0].endswith(f"TO <{scope_graph('ds', 'pg')}>")
    assert sender.sent[0].target == "postgres://analytics"


async def test_a_failed_webhook_leaves_the_watermark_alone(stub):
    runner = ProjectionRunner(stub, sender=RecordingSender(fail=True))
    delivery, _ = await runner.run(
        conn(), hook(delivery="webhook", endpoint="https://target.example/hook")
    )
    assert delivery.status == "failed"
    assert not stub.updates_matching("COPY SILENT")
    assert any("projection-scratch" in g for g in stub.dropped)


async def test_a_pending_pull_delivery_holds_its_scratch_graph(stub):
    delivery, _ = await ProjectionRunner(stub).run(conn(), hook())
    assert delivery.status == "pending"
    # nothing advanced, and the scratch survives for the acknowledgement
    assert not stub.updates_matching("COPY SILENT")
    assert not any("projection-scratch" in g for g in stub.dropped)


async def test_acknowledgement_advances_and_cleans_up(stub):
    runner = ProjectionRunner(stub)
    delivery, _ = await runner.run(conn(), hook())
    await runner.acknowledge(conn(), delivery)

    assert delivery.status == "acknowledged"
    copies = stub.updates_matching("COPY SILENT")
    assert copies and delivery.id in copies[0]
    assert any(delivery.id in g for g in stub.dropped)


async def test_rejection_keeps_the_watermark_so_the_delta_survives(stub):
    runner = ProjectionRunner(stub)
    delivery, _ = await runner.run(conn(), hook())
    await runner.reject(conn(), delivery, "column missing")

    assert delivery.status == "failed"
    assert delivery.error == "column missing"
    assert not stub.updates_matching("COPY SILENT")


async def test_a_settled_delivery_cannot_be_settled_twice(stub):
    runner = ProjectionRunner(stub)
    delivery, _ = await runner.run(conn(), hook())
    await runner.acknowledge(conn(), delivery)
    with pytest.raises(ProjectionError):
        await runner.acknowledge(conn(), delivery)


async def test_an_empty_delta_settles_without_bothering_the_target(stub):
    stub.additions = 0
    stub.retractions = 0
    sender = RecordingSender()
    delivery, envelope = await ProjectionRunner(stub, sender=sender).run(
        conn(), hook(delivery="webhook", endpoint="https://target.example/hook")
    )
    assert envelope.empty
    assert delivery.status == "delivered"
    assert sender.sent == []


async def test_force_sends_even_when_nothing_changed(stub):
    stub.additions = 0
    stub.retractions = 0
    sender = RecordingSender()
    await ProjectionRunner(stub, sender=sender).run(
        conn(),
        hook(delivery="webhook", endpoint="https://target.example/hook"),
        force=True,
    )
    assert len(sender.sent) == 1


# --- configuration ------------------------------------------------------------


def test_a_hook_must_declare_exactly_one_scope():
    assert "declare exactly one" in " ".join(
        hook(construct=SCOPE, named_query="q").problems()
    )
    assert "declare exactly one" in " ".join(hook(construct="", named_query="").problems())


def test_a_webhook_hook_needs_an_endpoint():
    assert "needs an endpoint" in " ".join(hook(delivery="webhook").problems())


def test_upsert_without_a_key_is_a_warning_not_an_error(stub):
    problems = hook(key_predicate="").problems()
    assert any("keyPredicate" in p for p in problems)


async def test_upsert_without_a_key_still_runs(stub):
    delivery, _ = await ProjectionRunner(stub).run(conn(), hook(key_predicate=""))
    assert delivery.status == "pending"


async def test_a_select_scope_is_refused(stub):
    with pytest.raises(ProjectionError) as exc:
        await ProjectionRunner(stub).run(
            conn(), hook(construct="SELECT * WHERE { ?s ?p ?o }")
        )
    assert "must CONSTRUCT" in str(exc.value)


async def test_a_suspended_hook_will_not_run(stub):
    with pytest.raises(ProjectionError):
        await ProjectionRunner(stub).run(conn(), hook(status="Suspended"))


async def test_the_sequence_advances_on_each_run(stub):
    h = hook(sequence=3)
    _, first = await ProjectionRunner(stub).run(conn(), h)
    _, second = await ProjectionRunner(stub).run(conn(), h)
    assert (first.sequence, second.sequence) == (4, 5)


# --- routes -------------------------------------------------------------------


def test_hooks_are_listed_with_their_problems(client):
    body = client.get("/projection/hooks", headers=auth()).json()
    assert body["count"] == 1
    assert body["hooks"][0]["target"] == "postgres://analytics"
    assert body["changeModes"] == ["append", "upsert", "soft-delete", "replace"]


def test_register_rejects_a_hook_with_no_scope(client):
    response = client.post(
        "/projection/hook", json={"id": "bad", "target": "x"}, headers=auth()
    )
    assert response.status_code == 400
    assert "declare exactly one" in " ".join(response.json()["detail"]["problems"])


def test_register_stores_the_hook(client, stub):
    body = client.post(
        "/projection/hook",
        json={"id": "xslt", "target": "xslt:invoice", "scope": SCOPE, "delivery": "pull"},
        headers=auth(),
    ).json()
    assert body["ok"] is True
    assert any("ProjectionHook" in u for u in stub.updates)


def test_run_returns_the_envelope(client):
    body = client.post("/projection/hook/pg/run", json={}, headers=auth()).json()
    assert body["status"] == "pending"
    assert body["envelope"]["counts"] == {"additions": 5, "retractions": 2}
    assert "urn:new" in body["envelope"]["additions"]


def test_run_can_omit_the_payload(client):
    body = client.post(
        "/projection/hook/pg/run", json={"include_payload": False}, headers=auth()
    ).json()
    assert "additions" not in body["envelope"]
    assert body["envelope"]["counts"]["additions"] == 5


def test_unknown_hook_lists_what_is_available(client):
    response = client.get("/projection/hook/nope", headers=auth())
    assert response.status_code == 404
    assert "pg" in response.json()["detail"]["available"]


def test_reset_clears_the_watermark(client, stub):
    body = client.post("/projection/hook/pg/reset", headers=auth()).json()
    assert body["watermark"] == "cleared"
    assert scope_graph("ds", "pg") in stub.dropped


def test_unknown_delivery_is_404(client):
    assert client.get("/projection/delivery/nope", headers=auth()).status_code == 404


# --- sweeping -----------------------------------------------------------------


async def test_sweeping_settles_abandoned_deliveries_and_drops_their_graphs(stub):
    stub.delivery_rows = [
        _row(
            id="abandoned1",
            hook="pg",
            createdAt="2026-07-01T00:00:00+00:00",
            additions="4",
            retractions="1",
        )
    ]
    result = await ProjectionRunner(stub).sweep(conn(), max_age_seconds=3600)

    assert result["abandonedCount"] == 1
    assert "abandoned1" in result["abandoned"]
    assert any("abandoned1" in g for g in stub.dropped)
    assert any('"failed"' in u for u in stub.updates)


async def test_sweeping_never_advances_a_watermark(stub):
    stub.delivery_rows = [
        _row(
            id="abandoned1",
            hook="pg",
            createdAt="2026-07-01T00:00:00+00:00",
            additions="4",
            retractions="1",
        )
    ]
    await ProjectionRunner(stub).sweep(conn(), max_age_seconds=3600)
    # the difference must survive the sweep so the next run offers it again
    assert not stub.updates_matching("COPY SILENT")


async def test_the_sweep_cutoff_carries_a_timezone(stub):
    result = await ProjectionRunner(stub).sweep(conn())
    assert result["cutoff"].endswith("+00:00")


def test_sweep_route(client, stub):
    body = client.post(
        "/projection/sweep", json={"max_age_seconds": 60}, headers=auth()
    ).json()
    assert body["ok"] is True
    assert "cutoff" in body
