"""Claiming an unprovisioned dataset: ``POST /admin/dataset/initialise``.

The security properties are the point, so each test asserts on what was (or was
not) WRITTEN to the store, not only on the status code:

- only an *established* identity (identity-bound token, or a trusted proxy's
  asserted one) that is also on ``DATASET_CLAIMANTS`` can claim;
- a legacy shared token or an open deployment never can, however the header is set;
- a dataset that already has an owner or admin cannot be claimed;
- the check and the grant are one update, so a second claim cannot also win;
- the claimer is always the caller;
- the typed ``dataset_unclaimed`` error is shown only to a caller who could act on it.

The backing store is the rdflib-backed ``GraphStoreStub`` from the identity-admin
tests, so the claim's SPARQL actually runs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from rdflib import RDF, URIRef

from holonbridge import claim as claim_mod
from holonbridge.audit import audit_graph
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app

from .test_identity_admin import GraphStoreStub

HOLON = "https://w3id.org/holon/"
HOLONS = "urn:example:holons"
OWNER_ROLE = URIRef("urn:example:role:owner")
ADMIN_ROLE = URIRef("urn:example:role:admin")
HAS_ROLE = URIRef(f"{HOLON}hasRole")

ALICE_TOKEN = "alice-identity-token-0123456789abcdef"
MALLORY_TOKEN = "mallory-identity-token-0123456789abcd"
PROXY_TOKEN = "mcp-proxy-token-0123456789abcdef0123"
SHARED_TOKEN = "shared-legacy-token-0123456789abcdef"

CLAIMANTS = ("alice",)


def _client(settings: Settings, stub: GraphStoreStub, tmp_path):
    app = create_app(settings)
    cm = TestClient(app)
    c = cm.__enter__()
    app.state.fuseki = stub
    app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
    return c, cm


@pytest.fixture
def stub() -> GraphStoreStub:
    return GraphStoreStub()


@pytest.fixture
def make(stub, tmp_path):
    opened = []

    def build(**settings) -> TestClient:
        c, cm = _client(Settings(fuseki_dataset="example", dataset_claimants=CLAIMANTS, **settings), stub, tmp_path)
        opened.append(cm)
        return c

    yield build
    for cm in opened:
        cm.__exit__(None, None, None)


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def roles(stub: GraphStoreStub) -> set[tuple[str, str]]:
    g = stub.ds.get_context(URIRef(HOLONS))
    return {(str(s), str(o)) for s, _, o in g.triples((None, HAS_ROLE, None))}


def written(stub: GraphStoreStub) -> int:
    return len(stub.ds.get_context(URIRef(HOLONS)))


# --- who may claim ---------------------------------------------------------------------


def test_an_allowlisted_identity_token_claims_an_unclaimed_dataset(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = c.post("/admin/dataset/initialise", json={}, headers=bearer(ALICE_TOKEN))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["person"] == "urn:example:person:alice"
    assert roles(stub) == {("urn:example:person:alice", str(OWNER_ROLE))}
    home = stub.ds.get_context(URIRef(HOLONS))
    assert (URIRef("urn:example:home"), RDF.type, URIRef(f"{HOLON}Home")) in home


def test_the_new_owner_then_resolves_and_can_administer(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})
    c.post("/admin/dataset/initialise", json={}, headers=bearer(ALICE_TOKEN))

    assert c.get("/admin/persons", headers=bearer(ALICE_TOKEN)).status_code == 200


def test_an_identity_not_on_the_allowlist_cannot_claim(make, stub):
    c = make(identity_tokens={MALLORY_TOKEN: "mallory"})

    r = c.post("/admin/dataset/initialise", json={}, headers=bearer(MALLORY_TOKEN))

    assert r.status_code == 403
    assert written(stub) == 0


def test_a_legacy_shared_token_cannot_claim_even_naming_an_allowlisted_identity(make, stub):
    """The header is attacker-controlled under a shared token, so it proves nothing."""
    c = make(bearer_token=SHARED_TOKEN)

    r = c.post(
        "/admin/dataset/initialise", json={}, headers=bearer(SHARED_TOKEN, **{"x-holon-animus-id": "alice"})
    )

    assert r.status_code == 403
    assert written(stub) == 0


def test_an_open_deployment_cannot_claim(make, stub):
    c = make()  # no tokens configured: loopback development only

    r = c.post("/admin/dataset/initialise", json={}, headers={"x-holon-animus-id": "alice"})

    assert r.status_code == 403
    assert written(stub) == 0


def test_a_proxy_token_may_claim_for_the_identity_it_asserts_if_allowlisted(make, stub):
    c = make(proxy_tokens=(PROXY_TOKEN,))

    ok = c.post("/admin/dataset/initialise", json={}, headers=bearer(PROXY_TOKEN, **{"x-holon-animus-id": "alice"}))

    assert ok.status_code == 200, ok.text
    assert roles(stub) == {("urn:example:person:alice", str(OWNER_ROLE))}


def test_a_proxy_token_asserting_someone_unlisted_cannot_claim(make, stub):
    c = make(proxy_tokens=(PROXY_TOKEN,))

    r = c.post("/admin/dataset/initialise", json={}, headers=bearer(PROXY_TOKEN, **{"x-holon-animus-id": "mallory"}))

    assert r.status_code == 403
    assert written(stub) == 0


def test_an_empty_allowlist_turns_the_claim_off(stub, tmp_path):
    c, cm = _client(
        Settings(fuseki_dataset="example", identity_tokens={ALICE_TOKEN: "alice"}), stub, tmp_path
    )
    try:
        r = c.post("/admin/dataset/initialise", json={}, headers=bearer(ALICE_TOKEN))
    finally:
        cm.__exit__(None, None, None)
    assert r.status_code == 403
    assert written(stub) == 0


# --- when it may be used ---------------------------------------------------------------


@pytest.mark.parametrize("held", [ADMIN_ROLE, OWNER_ROLE])
def test_a_dataset_with_an_admin_or_owner_cannot_be_claimed(make, stub, held):
    stub.ds.get_context(URIRef(HOLONS)).add((URIRef("urn:example:person:someone"), HAS_ROLE, held))
    before = written(stub)
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = c.post("/admin/dataset/initialise", json={}, headers=bearer(ALICE_TOKEN))

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "dataset_already_claimed"
    assert written(stub) == before


def test_the_claim_update_itself_is_guarded_so_a_second_claim_writes_nothing(stub):
    """The route's pre-check is a convenience; this is the property that holds under a race.
    Both updates are built while the dataset still looks unclaimed, then run in turn."""
    conn = Conn(base_url="http://x", dataset="example", overridden=False, bank_name="local")
    a = claim_mod.Claimant("alice", "GitHubIdentity")
    b = claim_mod.Claimant("bob", "GitHubIdentity")
    first = claim_mod.build_claim_update(
        conn, a, person="urn:example:person:alice", identity="urn:example:person:alice:identity:github", full_name="A"
    )
    second = claim_mod.build_claim_update(
        conn, b, person="urn:example:person:bob", identity="urn:example:person:bob:identity:github", full_name="B"
    )

    stub.ds.update(first)
    stub.ds.update(second)

    assert roles(stub) == {("urn:example:person:alice", str(OWNER_ROLE))}


def test_an_existing_person_for_the_identity_is_promoted_not_duplicated(make, stub):
    stub.ds.get_context(URIRef(HOLONS)).parse(
        format="turtle",
        data=f"""@prefix holon: <{HOLON}> .
<urn:example:person:al> a holon:Person ; holon:hasExternalIdentity <urn:example:person:al:identity:github> .
<urn:example:person:al:identity:github> a holon:GitHubIdentity ; holon:identifier "alice" .""",
    )
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = c.post("/admin/dataset/initialise", json={}, headers=bearer(ALICE_TOKEN))

    assert r.status_code == 200, r.text
    assert r.json()["person"] == "urn:example:person:al"
    assert r.json()["existing_person"] is True
    assert roles(stub) == {("urn:example:person:al", str(OWNER_ROLE))}
    persons = list(stub.ds.get_context(URIRef(HOLONS)).subjects(RDF.type, URIRef(f"{HOLON}Person")))
    assert len(persons) == 1


def test_an_existing_home_is_left_alone(make, stub):
    stub.ds.get_context(URIRef(HOLONS)).parse(
        format="turtle", data=f"@prefix holon: <{HOLON}> . <urn:example:my-home> a holon:Home ."
    )
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    c.post("/admin/dataset/initialise", json={}, headers=bearer(ALICE_TOKEN))

    homes = list(stub.ds.get_context(URIRef(HOLONS)).subjects(RDF.type, URIRef(f"{HOLON}Home")))
    assert homes == [URIRef("urn:example:my-home")]


def test_the_claimer_is_always_the_caller(make, stub):
    """There is no field that names anyone else; an attempt to add one is ignored."""
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = c.post(
        "/admin/dataset/initialise",
        json={"slug": "boss", "external_id": "mallory", "identity": "mallory"},
        headers=bearer(ALICE_TOKEN),
    )

    assert r.status_code == 200
    ids = {
        str(o)
        for o in stub.ds.get_context(URIRef(HOLONS)).objects(None, URIRef(f"{HOLON}identifier"))
    }
    assert ids == {"alice"}


def test_an_unsafe_slug_is_refused_without_writing(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = c.post("/admin/dataset/initialise", json={"slug": "a b>"}, headers=bearer(ALICE_TOKEN))

    assert r.status_code == 400
    assert written(stub) == 0


def test_a_successful_claim_is_audited(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    c.post("/admin/dataset/initialise", json={}, headers=bearer(ALICE_TOKEN))

    entries = stub.ds.get_context(URIRef(audit_graph(HOLONS)))
    assert len(entries) > 0
    text = entries.serialize(format="turtle")
    assert "/admin/dataset/initialise" in text
    assert "identity" in text  # the token kind is recorded


# --- the typed error -------------------------------------------------------------------


def test_an_eligible_caller_on_an_unclaimed_dataset_is_told_so(make, stub):
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = c.get("/admin/persons", headers=bearer(ALICE_TOKEN))

    assert r.status_code == 401
    detail = r.json()["detail"]
    assert detail["code"] == "dataset_unclaimed"
    assert "initialise" in detail["remedy"]
    assert "ask_the_person" in detail


def test_an_ineligible_caller_gets_the_plain_401_and_learns_nothing(make, stub):
    c = make(identity_tokens={MALLORY_TOKEN: "mallory"})

    r = c.get("/admin/persons", headers=bearer(MALLORY_TOKEN))

    assert r.status_code == 401
    assert isinstance(r.json()["detail"], str)
    assert "no Person found" in r.json()["detail"]


def test_a_legacy_caller_naming_a_listed_identity_gets_the_plain_401(make, stub):
    c = make(bearer_token=SHARED_TOKEN)

    r = c.get("/admin/persons", headers=bearer(SHARED_TOKEN, **{"x-holon-animus-id": "alice"}))

    assert r.status_code == 401
    assert isinstance(r.json()["detail"], str)


def test_on_a_claimed_dataset_an_unknown_eligible_caller_gets_the_plain_401(make, stub):
    stub.ds.get_context(URIRef(HOLONS)).add((URIRef("urn:example:person:someone"), HAS_ROLE, OWNER_ROLE))
    c = make(identity_tokens={ALICE_TOKEN: "alice"})

    r = c.get("/admin/persons", headers=bearer(ALICE_TOKEN))

    assert r.status_code == 401
    assert isinstance(r.json()["detail"], str)
