"""Creating a dataset: ``POST /datasets``.

As with the claim tests, each one asserts on what was (or was not) DONE -- a
dataset created, a row written -- not only on the status code. The properties
under test:

- only an *established* identity that is also on ``DATASET_CREATORS`` may create;
  the legacy shared token and an open deployment never can, however the header
  is set, and an empty list turns creation off;
- the gate is its own list: being a dataset claimant or admin is not enough;
- names are strict, reserved names are refused, and an existing name (in any
  case) is never overwritten;
- there is a ceiling on how many datasets the server will host;
- ``kind`` is required and means what it says (TDB2 vs in-memory);
- the creator becomes the owner by default, and ``claim=false`` writes nothing;
- the bridge cannot drop a dataset: no client method, no route;
- a successful creation is always audited, and a refusal is audited as denied.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from rdflib import URIRef

from holonbridge.audit import audit_graph
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiClient, FusekiError
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app

from .test_identity_admin import GraphStoreStub

HOLON = "https://w3id.org/holon/"
HAS_ROLE = URIRef(f"{HOLON}hasRole")

ALICE_TOKEN = "alice-identity-token-0123456789abcdef"
MALLORY_TOKEN = "mallory-identity-token-0123456789abcd"
PROXY_TOKEN = "mcp-proxy-token-0123456789abcdef0123"
SHARED_TOKEN = "shared-legacy-token-0123456789abcdef"

CREATORS = ("alice",)


class CreatingStub(GraphStoreStub):
    """A store that hosts datasets and can create them (and say no)."""

    def __init__(self, hosted: tuple[str, ...] = ("example",)) -> None:
        super().__init__()
        self.hosted = list(hosted)
        self.created: list[tuple[str, bool]] = []
        self.create_error: FusekiError | None = None
        self.list_error: FusekiError | None = None
        self.forget_creations = False  # model a server that accepts but does not list

    async def list_datasets(self, conn):
        if self.list_error is not None:
            raise self.list_error
        return [{"name": n, "state": True, "services": []} for n in self.hosted]

    async def create_dataset(self, conn, name, *, persistent):
        if self.create_error is not None:
            raise self.create_error
        self.created.append((name, persistent))
        if not self.forget_creations:
            self.hosted.append(name)


@pytest.fixture
def stub() -> CreatingStub:
    return CreatingStub()


@pytest.fixture
def make(stub, tmp_path):
    opened = []

    def build(**settings) -> TestClient:
        settings.setdefault("dataset_creators", CREATORS)
        app = create_app(Settings(fuseki_dataset="example", **settings))
        cm = TestClient(app)
        c = cm.__enter__()
        app.state.fuseki = stub
        app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
        opened.append(cm)
        return c

    yield build
    for cm in opened:
        cm.__exit__(None, None, None)


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def alice(c, body, **extra):
    return c.post("/datasets", json=body, headers=bearer(ALICE_TOKEN, **extra))


def roles_in(stub: GraphStoreStub, holons: str) -> set[tuple[str, str]]:
    g = stub.ds.get_context(URIRef(holons))
    return {(str(s), str(o)) for s, _, o in g.triples((None, HAS_ROLE, None))}


# --- who may create ----------------------------------------------------------------------


def test_a_listed_identity_creates_a_persistent_dataset_and_becomes_its_owner(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 201, r.text
    body = r.json()
    assert stub.created == [("newds", True)]
    assert body["persistent"] is True and body["volatile"] is False
    assert body["claimed"] is True and body["owner"] == "urn:newds:person:alice"
    assert roles_in(stub, "urn:newds:holons") == {
        ("urn:newds:person:alice", "urn:newds:role:owner")
    }
    assert body["graphs"]["holons"] == "urn:newds:holons"


def test_an_in_memory_dataset_is_created_as_such_and_flagged_volatile(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "scratchpad", "kind": "memory"})

    assert r.status_code == 201, r.text
    assert stub.created == [("scratchpad", False)]
    body = r.json()
    assert body["volatile"] is True and body["persistent"] is False
    assert any("lost when Fuseki restarts" in n for n in body["notes"])


def test_an_identity_not_on_the_list_cannot_create(make, stub):
    c = make(identity_tokens={MALLORY_TOKEN: "mallory"})

    r = c.post("/datasets", json={"name": "newds", "kind": "persistent"}, headers=bearer(MALLORY_TOKEN))

    assert r.status_code == 403
    assert stub.created == []


def test_the_legacy_shared_token_cannot_create_even_naming_a_listed_identity(make, stub):
    """The header is attacker-controlled under a shared token, so it proves nothing."""
    c = make(bearer_token=SHARED_TOKEN)

    r = c.post(
        "/datasets",
        json={"name": "newds", "kind": "persistent"},
        headers=bearer(SHARED_TOKEN, **{"x-holon-animus-id": "alice"}),
    )

    assert r.status_code == 403
    assert stub.created == []


def test_an_open_deployment_cannot_create(make, stub):
    c = make()

    r = c.post(
        "/datasets", json={"name": "newds", "kind": "persistent"}, headers={"x-holon-animus-id": "alice"}
    )

    assert r.status_code == 403
    assert stub.created == []


def test_a_proxy_may_create_for_the_identity_it_asserts_if_listed(make, stub):
    c = make(proxy_tokens=(PROXY_TOKEN,))

    ok = c.post(
        "/datasets",
        json={"name": "newds", "kind": "persistent"},
        headers=bearer(PROXY_TOKEN, **{"x-holon-animus-id": "alice"}),
    )
    no = c.post(
        "/datasets",
        json={"name": "other", "kind": "persistent"},
        headers=bearer(PROXY_TOKEN, **{"x-holon-animus-id": "mallory"}),
    )

    assert ok.status_code == 201, ok.text
    assert no.status_code == 403
    assert stub.created == [("newds", True)]


def test_an_empty_creators_list_turns_creation_off(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"}, dataset_creators=())

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 403
    assert stub.created == []


def test_being_a_claimant_is_not_enough_to_create(make, stub):
    """Claiming and creating are different authorities, each with its own list."""
    c = make(identity_tokens={MALLORY_TOKEN: "mallory"}, dataset_claimants=("mallory",))

    r = c.post("/datasets", json={"name": "newds", "kind": "persistent"}, headers=bearer(MALLORY_TOKEN))

    assert r.status_code == 403
    assert stub.created == []


def test_a_creator_need_not_also_be_a_claimant_to_own_what_they_create(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"}, dataset_claimants=())

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.json()["claimed"] is True


# --- the request -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["", "Bad", "1abc", "a.b", "a b", "x" * 65, "../etc", "a/b", "a:b", "-abc", "ünï"],
)
def test_a_bad_name_is_refused_without_creating_anything(make, stub, name):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": name, "kind": "persistent"})

    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_dataset_name"
    assert stub.created == []


def test_reserved_names_are_refused(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"}, scheduler_dataset="jobs")

    assert alice(c, {"name": "admin", "kind": "persistent"}).status_code == 400
    r = alice(c, {"name": "jobs", "kind": "persistent"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "reserved_dataset_name"
    assert stub.created == []


@pytest.mark.parametrize("body", [{"name": "newds"}, {"name": "newds", "kind": "mem"}, {"kind": "memory"}])
def test_kind_is_required_and_must_be_one_of_the_two(make, stub, body):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    assert alice(c, body).status_code == 422
    assert stub.created == []


def test_an_existing_name_is_never_overwritten_in_any_case(make, stub):
    stub.hosted = ["example", "Chloe"]
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "chloe", "kind": "persistent"})

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "dataset_exists"
    assert stub.created == []


def test_the_server_ceiling_is_enforced(make, stub):
    stub.hosted = ["example", "other"]
    c = make(identity_tokens={ALICE_TOKEN: "alice"}, max_datasets=2)

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "dataset_limit_reached"
    assert stub.created == []


# --- the creator becomes the owner (or not) -----------------------------------------------


def test_claim_false_creates_the_dataset_and_writes_nothing(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "newds", "kind": "persistent", "claim": False})

    assert r.status_code == 201
    assert r.json()["claimed"] is False and r.json()["owner"] is None
    assert stub.created == [("newds", True)]
    assert len(stub.ds.get_context(URIRef("urn:newds:holons"))) == 0


def test_a_failed_claim_is_reported_not_raised_because_the_dataset_exists_regardless(make, stub):
    stub.forget_creations = True  # the claim's own "is it hosted?" check then says no
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 201
    assert r.json()["claimed"] is False
    assert r.json()["claimError"]["code"] == "unknown_dataset"
    assert stub.created == [("newds", True)]


def test_bank_scoping_is_honoured_when_the_dataset_is_declared_first(make, stub, tmp_path):
    """Declare-then-create is the supported order: identity lands in the scoped graph."""
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {"banks": {"local": {"url": "http://localhost:3030", "dataset": "example",
                                 "bankScopedDatasets": ["newds"]}}}
        )
    )
    c = make(identity_tokens={ALICE_TOKEN: "alice"}, config_path=cfg)

    r = alice(c, {"name": "newds", "kind": "persistent"})

    body = r.json()
    assert body["bankScoped"] is True
    assert body["graphs"]["holons"] == "urn:local:newds:holons"
    assert roles_in(stub, "urn:local:newds:holons") == {
        ("urn:local:newds:person:alice", "urn:local:newds:role:owner")
    }
    assert len(stub.ds.get_context(URIRef("urn:newds:holons"))) == 0
    assert "notes" not in body  # nothing to warn about


def test_an_undeclared_dataset_gets_the_warning_about_bank_scoping(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    body = alice(c, {"name": "newds", "kind": "persistent"}).json()

    assert body["bankScoped"] is False
    assert any("bankScopedDatasets" in n for n in body["notes"])


# --- when Fuseki cannot or will not ----------------------------------------------------------


def test_a_race_lost_to_another_creator_is_a_409(make, stub):
    stub.create_error = FusekiError(409, "exists", endpoint="/$/datasets")
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "dataset_exists"


def test_a_fuseki_refusal_is_a_502_naming_the_upstream_status(make, stub):
    stub.create_error = FusekiError(403, "Forbidden", endpoint="/$/datasets")
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "create_failed"
    assert r.json()["detail"]["upstream"]["status"] == 403
    assert len(stub.ds.get_context(URIRef("urn:newds:holons"))) == 0


def test_an_unavailable_admin_api_is_a_502_and_creates_nothing(make, stub):
    stub.list_error = FusekiError(404, "no admin api", endpoint="/$/datasets")
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "admin_api_unavailable"
    assert stub.created == []


# --- the client's own request --------------------------------------------------------------


def _client_with(handler) -> FusekiClient:
    client = FusekiClient()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


CONN = Conn(base_url="http://fuseki:3030", dataset="ds", overridden=False, bank_name="local")


@pytest.mark.parametrize(("persistent", "db_type"), [(True, "tdb2"), (False, "mem")])
async def test_the_client_posts_only_a_name_and_a_type(persistent, db_type):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    await _client_with(handler).create_dataset(CONN, "newds", persistent=persistent)

    (req,) = seen
    assert req.method == "POST" and str(req.url) == "http://fuseki:3030/$/datasets"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(req.content.decode()) == {"dbName": ["newds"], "dbType": [db_type]}


async def test_the_client_surfaces_fusekis_409():
    client = _client_with(lambda request: httpx.Response(409, text="conflict"))

    with pytest.raises(FusekiError) as exc:
        await client.create_dataset(CONN, "newds", persistent=True)

    assert exc.value.status == 409


# --- there is no drop ------------------------------------------------------------------------


def test_the_bridge_has_no_way_to_drop_a_dataset(make):
    assert not [n for n in dir(FusekiClient) if "dataset" in n and any(v in n for v in ("drop", "delete", "remove"))]

    c = make()
    for route in c.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if path.startswith(("/datasets", "/admin/dataset")):
            assert "DELETE" not in methods, f"{path} must not accept DELETE"


# --- audit -----------------------------------------------------------------------------------


def test_a_successful_creation_is_audited_even_when_the_operator_has_turned_audit_down(
    make, stub, monkeypatch
):
    """Like initialising a dataset, creating one is never left to the audit level:
    it allocates storage the bridge cannot take back."""
    monkeypatch.setenv("HOLONBRIDGE_AUDIT_LEVEL", "denials")
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    alice(c, {"name": "newds", "kind": "persistent"})

    text = stub.ds.get_context(URIRef(audit_graph("urn:example:holons"))).serialize(format="turtle")
    assert "POST /datasets" in text
    assert "newds" in text  # the authorised_reason names the dataset


def test_a_refused_creation_is_audited_as_denied(make, stub):
    c = make(identity_tokens={MALLORY_TOKEN: "mallory"})

    c.post("/datasets", json={"name": "newds", "kind": "persistent"}, headers=bearer(MALLORY_TOKEN))

    text = stub.ds.get_context(URIRef(audit_graph("urn:example:holons"))).serialize(format="turtle")
    assert "POST /datasets" in text
    assert '"denied"' in text
