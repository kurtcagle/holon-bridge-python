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
- Fuseki's admin API cannot make a dataset with a SHACL endpoint, so the bridge
  adds one to the configuration file Fuseki wrote (effective after a restart),
  says so, and never edits anything but an appended statement; the bundled shapes
  are seeded by default;
- the bridge cannot drop a dataset: no client method, no route;
- a successful creation is always audited, and a refusal is audited as denied.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from rdflib import RDF, Literal, URIRef

from holonbridge.audit import audit_graph
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiClient, FusekiError
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app
from holonbridge.service_config import ServiceConfigError, enable_shacl, with_shacl
from holonbridge.turtle import parse

from .test_identity_admin import GraphStoreStub

HOLON = "https://w3id.org/holon/"
HAS_ROLE = URIRef(f"{HOLON}hasRole")
FUSEKI = "http://jena.apache.org/fuseki#"

#: Roughly what Fuseki writes for a dataset made through its admin API.
FUSEKI_TEMPLATE = """@prefix :        <#> .
@prefix fuseki:  <http://jena.apache.org/fuseki#> .
@prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix tdb2:    <http://jena.apache.org/2016/tdb#> .

:service rdf:type fuseki:Service ;
    fuseki:name "{name}" ;
    fuseki:endpoint [ fuseki:operation fuseki:query ; fuseki:name "query" ] ;
    fuseki:endpoint [ fuseki:operation fuseki:update ; fuseki:name "update" ] ;
    fuseki:endpoint [ fuseki:operation fuseki:gsp-rw ; fuseki:name "data" ] ;
    fuseki:dataset :ds .

:ds rdf:type tdb2:DatasetTDB2 ;
    tdb2:location "databases/{name}" .
"""

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
        self.config_dir: Path | None = None  # where this "Fuseki" writes its config files
        self.create_error: FusekiError | None = None
        self.list_error: FusekiError | None = None
        self.services: dict[str, list[str]] = {}
        self.forget_creations = False  # model a server that accepts but does not list

    async def list_datasets(self, conn):
        if self.list_error is not None:
            raise self.list_error
        return [
            {"name": n, "state": True, "services": self.services.get(n, [])} for n in self.hosted
        ]

    async def create_dataset(self, conn, name, *, persistent):
        if self.create_error is not None:
            raise self.create_error
        self.created.append((name, persistent))
        # What the admin API really does: the fixed template, no shacl endpoint.
        self.services[name] = ["gsp-rw", "query", "update"]
        if self.config_dir is not None:
            (self.config_dir / f"{name}.ttl").write_text(FUSEKI_TEMPLATE.format(name=name))
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
    assert not any("bankScopedDatasets" in n for n in body.get("notes", []))  # nothing to warn about


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


# --- SHACL: only a restart can add it ------------------------------------------------------------


@pytest.fixture
def cfg(stub, tmp_path) -> Path:
    d = tmp_path / "configuration"
    d.mkdir()
    stub.config_dir = d
    return d


def test_without_a_config_directory_the_dataset_is_created_but_reported_as_lacking_shacl(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["shaclEnabled"] is False and body["shaclEndpoint"] is None
    assert body["shaclConfigured"] is False and body["restartRequired"] is False
    assert "FUSEKI_CONFIG_DIR" in body["shaclConfigError"]
    assert any("no shacl endpoint" in n for n in body["notes"])


@pytest.mark.parametrize("kind", ["persistent", "memory"])
def test_a_shacl_endpoint_is_appended_to_the_configuration_fuseki_wrote(make, stub, cfg, kind):
    c = make(identity_tokens={ALICE_TOKEN: "alice"}, fuseki_config_dir=cfg)

    body = alice(c, {"name": "newds", "kind": kind}).json()

    assert body["shaclConfigured"] is True and body["restartRequired"] is True
    assert body["shaclEnabled"] is False  # read back from Fuseki: not live yet
    assert any("restart Fuseki" in n for n in body["notes"])
    text = (cfg / "newds.ttl").read_text()
    # what Fuseki wrote is untouched; the edit is purely an addition
    assert text.startswith(FUSEKI_TEMPLATE.format(name="newds").rstrip("\n"))
    g = parse(text)
    assert (None, URIRef(f"{FUSEKI}operation"), URIRef(f"{FUSEKI}shacl")) in g
    assert (None, URIRef(f"{FUSEKI}name"), Literal("shacl")) in g
    # and the shacl endpoint hangs off the service, beside the originals
    (service,) = g.subjects(RDF.type, URIRef(f"{FUSEKI}Service"))
    operations = {
        o
        for ep in g.objects(service, URIRef(f"{FUSEKI}endpoint"))
        for o in g.objects(ep, URIRef(f"{FUSEKI}operation"))
    }
    assert URIRef(f"{FUSEKI}shacl") in operations and URIRef(f"{FUSEKI}query") in operations


def test_a_missing_configuration_file_is_reported_and_the_dataset_stands(make, stub, cfg):
    stub.config_dir = None  # this "Fuseki" writes no file
    c = make(identity_tokens={ALICE_TOKEN: "alice"}, fuseki_config_dir=cfg)

    r = alice(c, {"name": "newds", "kind": "persistent"})

    assert r.status_code == 201
    body = r.json()
    assert body["shaclConfigured"] is False and "no configuration file" in body["shaclConfigError"]
    assert list(cfg.iterdir()) == []  # nothing invented


def test_the_server_already_reporting_shacl_is_left_alone(make, stub, cfg, monkeypatch):
    original = stub.create_dataset

    async def with_shacl(conn, name, *, persistent):
        await original(conn, name, persistent=persistent)
        stub.services[name] = ["gsp-rw", "query", "shacl", "update"]

    monkeypatch.setattr(stub, "create_dataset", with_shacl)
    c = make(identity_tokens={ALICE_TOKEN: "alice"}, fuseki_config_dir=cfg)

    body = alice(c, {"name": "newds", "kind": "persistent"}).json()

    assert body["shaclEnabled"] is True
    assert body["shaclEndpoint"] == "http://localhost:3030/newds/shacl"
    assert body["restartRequired"] is False
    assert (cfg / "newds.ttl").read_text() == FUSEKI_TEMPLATE.format(name="newds")


# --- the configuration edit itself ---------------------------------------------------------------


def test_with_shacl_is_idempotent_and_only_appends():
    once = with_shacl(FUSEKI_TEMPLATE.format(name="x"), "x")
    assert once.startswith(FUSEKI_TEMPLATE.format(name="x").rstrip("\n"))
    assert with_shacl(once, "x") == once
    assert once.count("fuseki#shacl") == 1


_PFX = "@prefix fuseki: <http://jena.apache.org/fuseki#> .\n"


@pytest.mark.parametrize(
    "text",
    [
        "this is not turtle",
        _PFX + '<#a> a fuseki:Service ; fuseki:name "other" .',
        _PFX + '<#a> a fuseki:Service ; fuseki:name "x" .\n<#b> a fuseki:Service ; fuseki:name "x" .',
        _PFX + '[] a fuseki:Service ; fuseki:name "x" .',
    ],
)
def test_a_configuration_that_is_not_one_clean_named_service_is_refused(text):
    with pytest.raises(ServiceConfigError):
        with_shacl(text, "x")


def test_the_configuration_path_cannot_leave_its_directory(tmp_path):
    inside = tmp_path / "configuration"
    inside.mkdir()
    outside = tmp_path / "escape.ttl"  # a real, valid file one level up
    outside.write_text(FUSEKI_TEMPLATE.format(name="../escape"))  # even if it claims that name

    with pytest.raises(ServiceConfigError):
        enable_shacl(inside, "../escape")

    assert outside.read_text() == FUSEKI_TEMPLATE.format(name="../escape")


def test_enable_shacl_writes_atomically_and_leaves_no_temporary_file(tmp_path):
    (tmp_path / "x.ttl").write_text(FUSEKI_TEMPLATE.format(name="x"))

    result = enable_shacl(tmp_path, "x")

    assert result.changed and result.restart_required
    assert sorted(f.name for f in tmp_path.iterdir()) == ["x.ttl"]
    assert enable_shacl(tmp_path, "x").changed is False


def test_the_bundled_shapes_are_seeded_into_the_shapes_graph_by_default(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    body = alice(c, {"name": "newds", "kind": "persistent"}).json()

    assert body["shapesSeeded"] == "urn:newds:shacl"
    shapes = stub.ds.get_context(URIRef("urn:newds:shacl"))
    node_shapes = set(shapes.subjects(RDF.type, URIRef("http://www.w3.org/ns/shacl#NodeShape")))
    assert node_shapes, "no sh:NodeShape landed in the shapes graph"


def test_shapes_false_leaves_the_shapes_graph_empty(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    body = alice(c, {"name": "newds", "kind": "persistent", "shapes": False}).json()

    assert body["shapesSeeded"] is None
    assert len(stub.ds.get_context(URIRef("urn:newds:shacl"))) == 0


def test_shapes_are_seeded_even_when_the_creator_does_not_claim(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    body = alice(c, {"name": "newds", "kind": "memory", "claim": False}).json()

    assert body["claimed"] is False and body["shapesSeeded"] == "urn:newds:shacl"
    assert roles_in(stub, "urn:newds:holons") == set()


def test_a_shapes_failure_is_reported_and_the_dataset_still_stands(make, stub, monkeypatch):
    async def boom(conn, graph_iri, turtle):
        raise FusekiError(500, "disk full", endpoint="x")

    monkeypatch.setattr(stub, "put_graph", boom)
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = alice(c, {"name": "newds", "kind": "memory"})

    assert r.status_code == 201
    body = r.json()
    assert body["shapesSeeded"] is None
    assert body["shapesError"]["code"] == "shapes_seed_failed"
    assert body["claimed"] is True  # a shapes failure does not stop the claim


def test_the_packaged_shapes_parse_and_cover_every_bundled_file():
    from holonbridge.dataset_create import BUNDLED_SHAPES, SHAPES_DIR, bundled_shapes

    for name in BUNDLED_SHAPES:
        assert (SHAPES_DIR / name).is_file(), name
    assert len(parse(bundled_shapes())) > 0
