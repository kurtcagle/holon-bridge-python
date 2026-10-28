"""Snapshot and restore of a dataset: ``/admin/dataset/snapshot`` and ``/restore``.

Each test asserts on what was (or was not) DONE -- files written or kept, graphs
put back or left alone -- not only on the status code. The properties:

- a snapshot taken before a Fuseki restart puts a dataset back exactly as it was;
- snapshot needs admin on the dataset; restore needs the creation gate (an empty
  dataset has no admin) and an *empty* dataset, and never overwrites;
- a snapshot of an empty dataset is refused, so one taken after a restart cannot
  replace a good one with nothing;
- a bad, oversized or tampered snapshot is refused before anything is written,
  and a restore that fails part-way leaves the dataset empty so it can be retried;
- RDF 1.2 text passes through untouched (the bridge never parses a graph);
- both are always audited.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rdflib import Dataset, URIRef
from rdflib.compare import isomorphic

from holonbridge.audit import audit_graph
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiError
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app
from holonbridge.snapshot import MANIFEST, SnapshotError, _dir, take

from .test_identity_admin import GraphStoreStub

HOLON = "https://w3id.org/holon/"
ALICE_TOKEN = "alice-identity-token-0123456789abcdef"
JANE_TOKEN = "jane-identity-token-0123456789abcdefg"
SHARED_TOKEN = "shared-legacy-token-0123456789abcdef"

SEED = f"""
@prefix holon: <{HOLON}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<urn:example:role:admin> a holon:Role ; rdfs:label "Admin" .
<urn:example:person:alice> a holon:Person ; rdfs:label "Alice" ;
    holon:hasExternalIdentity <urn:example:person:alice:identity:github> ;
    holon:hasRole <urn:example:role:admin> .
<urn:example:person:alice:identity:github> a holon:GitHubIdentity ; holon:identifier "alice" .
<urn:example:person:jane> a holon:Person ; rdfs:label "Jane" ;
    holon:hasExternalIdentity <urn:example:person:jane:identity:github> .
<urn:example:person:jane:identity:github> a holon:GitHubIdentity ; holon:identifier "jane" .
"""
DATA = '<urn:example:thing:1> <http://www.w3.org/2000/01/rdf-schema#label> "A thing" .\n'


class HostedStub(GraphStoreStub):
    def __init__(self) -> None:
        super().__init__()
        self.hosted = ["example"]
        self.put_calls: list[str] = []
        self.fail_put_on: str | None = None

    async def list_datasets(self, conn):
        return [{"name": n, "state": True, "services": []} for n in self.hosted]

    async def put_graph(self, conn, graph_iri, turtle):
        if self.fail_put_on == graph_iri:
            raise FusekiError(500, "boom", endpoint="x")
        self.put_calls.append(graph_iri)
        await super().put_graph(conn, graph_iri, turtle)

    def wipe(self) -> None:
        """What a Fuseki restart does to an in-memory dataset."""
        self.ds = Dataset()
        self.put_calls.clear()

    def graphs(self) -> set[str]:
        """Graphs with content. The audit graph is the bridge's own and is left out."""
        return {
            str(g.identifier)
            for g in self.ds.contexts()
            if len(g) and str(g.identifier) != "urn:example:audit"
        }


@pytest.fixture
def stub() -> HostedStub:
    s = HostedStub()
    s.ds.get_context(URIRef("urn:example:holons")).parse(data=SEED, format="turtle")
    s.ds.get_context(URIRef("urn:example:data")).parse(data=DATA, format="turtle")
    return s


@pytest.fixture
def snaps(tmp_path) -> Path:
    return tmp_path / "snapshots"


@pytest.fixture
def make(stub, tmp_path, snaps):
    opened = []

    def build(**settings) -> TestClient:
        settings.setdefault("identity_tokens", {ALICE_TOKEN: "alice", JANE_TOKEN: "jane"})
        settings.setdefault("dataset_creators", ("alice",))
        settings.setdefault("snapshot_dir", snaps)
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


def snap_dirs(snaps: Path) -> list[Path]:
    """The dataset's snapshot directories, oldest first."""
    return sorted(d for d in (snaps / "example").iterdir() if d.is_dir() and not d.name.startswith("."))


def latest(snaps: Path) -> Path:
    return snap_dirs(snaps)[-1]


def auth(token: str = ALICE_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def snapshot(c, token=ALICE_TOKEN):
    return c.post("/admin/dataset/snapshot", json={}, headers=auth(token))


def restore(c, token=ALICE_TOKEN):
    return c.post("/admin/dataset/restore", json={}, headers=auth(token))


def triples(stub: HostedStub, iri: str):
    return stub.ds.get_context(URIRef(iri))


# --- the round trip --------------------------------------------------------------------------


def test_a_dataset_is_put_back_exactly_as_it_was_after_a_restart(make, stub, snaps):
    c = make()
    before = {iri: set(triples(stub, iri)) for iri in stub.graphs()}

    r = snapshot(c)
    assert r.status_code == 200, r.text
    stub.wipe()
    assert stub.graphs() == set()

    r = restore(c)

    assert r.status_code == 200, r.text
    assert r.json()["restored"] == len(before)
    assert stub.graphs() >= set(before)
    for iri, expected in before.items():
        assert isomorphic(triples(stub, iri), _as_graph(expected)), iri
    # including the ownership that a bare re-seed could not know about
    assert (URIRef("urn:example:person:alice"), URIRef(f"{HOLON}hasRole"), URIRef("urn:example:role:admin")) in triples(
        stub, "urn:example:holons"
    )


def _as_graph(statements):
    from rdflib import Graph

    g = Graph()
    for t in statements:
        g.add(t)
    return g


def test_the_snapshot_is_one_manifest_and_a_turtle_file_per_graph(make, stub, snaps):
    c = make()

    snapshot(c)

    manifest = json.loads((latest(snaps) / MANIFEST).read_text())
    assert manifest["dataset"] == "example" and manifest["createdBy"]
    assert {g["iri"] for g in manifest["graphs"]} >= {"urn:example:holons", "urn:example:data"}
    for entry in manifest["graphs"]:
        assert (latest(snaps) / entry["file"]).is_file()


def test_each_snapshot_is_kept_and_none_leaves_temporary_files(make, stub, snaps):
    c = make()
    first = snapshot(c).json()["snapshot"]
    triples(stub, "urn:example:data").add(
        (URIRef("urn:example:thing:2"), URIRef("urn:p"), URIRef("urn:o"))
    )

    second = snapshot(c).json()["snapshot"]

    assert first != second  # even when taken in the same second
    assert [d.name for d in snap_dirs(snaps)] == sorted([first, second])
    texts = {d.name: "".join(f.read_text() for f in d.glob("g*.ttl")) for d in snap_dirs(snaps)}
    assert "thing:2" not in texts[first] and "thing:2" in texts[second]
    assert sorted(p.name for p in (snaps / "example").iterdir()) == sorted([first, second])


def test_only_the_newest_few_are_kept_and_older_ones_are_removed(make, stub, snaps):
    c = make(snapshot_keep=3)
    ids = [snapshot(c).json()["snapshot"] for _ in range(5)]

    assert [d.name for d in snap_dirs(snaps)] == sorted(ids)[-3:]
    assert snapshot(c).json()["kept"] == 3


def test_a_failed_snapshot_does_not_cost_an_earlier_one_its_place(make, stub, snaps):
    c = make(snapshot_keep=1)
    good = snapshot(c).json()["snapshot"]

    c = make(snapshot_keep=1, snapshot_max_bytes=10)
    assert snapshot(c).status_code == 413

    assert [d.name for d in snap_dirs(snaps)] == [good]


def test_listing_shows_the_snapshots_newest_first_without_contents(make, stub, snaps):
    c = make()
    first = snapshot(c).json()["snapshot"]
    second = snapshot(c).json()["snapshot"]

    r = c.get("/admin/dataset/snapshots", headers=auth())

    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["id"] for s in body["snapshots"]] == sorted([first, second], reverse=True)
    assert body["keep"] == 5 and all(s["graphs"] > 0 and s["bytes"] > 0 for s in body["snapshots"])
    assert "thing" not in r.text  # metadata only


def test_listing_uses_the_creation_gate(make, stub, snaps):
    c = make()
    snapshot(c)

    assert c.get("/admin/dataset/snapshots", headers=auth(JANE_TOKEN)).status_code == 403


def test_an_older_snapshot_can_be_restored_by_id(make, stub, snaps):
    c = make()
    older = snapshot(c).json()["snapshot"]
    triples(stub, "urn:example:data").add(
        (URIRef("urn:example:thing:2"), URIRef("urn:p"), URIRef("urn:o"))
    )
    snapshot(c)  # the newest has thing:2
    stub.wipe()

    r = c.post("/admin/dataset/restore", json={"snapshot": older}, headers=auth())

    assert r.status_code == 200, r.text
    assert r.json()["snapshot"] == older
    assert (URIRef("urn:example:thing:2"), URIRef("urn:p"), URIRef("urn:o")) not in triples(
        stub, "urn:example:data"
    )


def test_restore_defaults_to_the_newest_snapshot(make, stub, snaps):
    c = make()
    snapshot(c)
    triples(stub, "urn:example:data").add(
        (URIRef("urn:example:thing:2"), URIRef("urn:p"), URIRef("urn:o"))
    )
    newest = snapshot(c).json()["snapshot"]
    stub.wipe()

    body = restore(c).json()

    assert body["snapshot"] == newest
    assert (URIRef("urn:example:thing:2"), URIRef("urn:p"), URIRef("urn:o")) in triples(
        stub, "urn:example:data"
    )


def test_an_unknown_snapshot_id_is_a_404_that_lists_what_exists(make, stub, snaps):
    c = make()
    have = snapshot(c).json()["snapshot"]
    stub.wipe()

    r = c.post("/admin/dataset/restore", json={"snapshot": "20000101T000000Z"}, headers=auth())

    assert r.status_code == 404 and have in r.json()["detail"]["message"]
    assert stub.put_calls == []


@pytest.mark.parametrize("bad", ["../escape", "g0001.ttl", "latest", "20261028T041706Z/../x", ""])
def test_a_snapshot_id_cannot_name_anything_but_a_snapshot(make, stub, snaps, bad):
    c = make()
    snapshot(c)
    stub.wipe()

    r = c.post("/admin/dataset/restore", json={"snapshot": bad}, headers=auth())

    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_snapshot_id"
    assert stub.put_calls == []


def test_a_snapshot_in_the_first_layout_is_migrated_and_still_restorable(make, stub, snaps):
    """The first release wrote manifest.json and the graph files straight into the dataset directory."""
    c = make()
    snapshot(c)
    snap = latest(snaps)
    flat = snaps / "example"
    for f in snap.iterdir():
        f.rename(flat / f.name)
    snap.rmdir()
    assert (flat / MANIFEST).is_file()
    stub.wipe()

    r = restore(c)

    assert r.status_code == 200, r.text
    assert not (flat / MANIFEST).exists()  # moved into an id-named directory
    assert len(snap_dirs(snaps)) == 1 and stub.graphs() >= {"urn:example:holons", "urn:example:data"}


def test_rdf12_text_passes_through_unparsed(make, stub, snaps, monkeypatch):
    rdf12 = "<urn:s> <urn:p> <<( <urn:a> <urn:b> <urn:c> )>> .\n"
    original = stub.get_graph
    received: dict[str, str] = {}

    async def get_graph(conn, iri):
        return rdf12 if iri == "urn:example:data" else await original(conn, iri)

    async def put_graph(conn, iri, text):
        received[iri] = text

    monkeypatch.setattr(stub, "get_graph", get_graph)
    c = make()
    snapshot(c)
    stub.wipe()
    monkeypatch.setattr(stub, "put_graph", put_graph)

    restore(c)

    assert received["urn:example:data"] == rdf12


# --- who may, and when -------------------------------------------------------------------------


def test_snapshot_needs_admin_on_the_dataset(make, stub, snaps):
    c = make()

    r = snapshot(c, JANE_TOKEN)  # a Person, but not an admin

    assert r.status_code == 403
    assert not snaps.exists()


def test_being_a_creator_is_not_enough_to_read_a_whole_dataset(make, stub, snaps):
    c = make(dataset_creators=("jane",))  # jane may create datasets, but does not administer this one

    assert snapshot(c, JANE_TOKEN).status_code == 403
    assert not snaps.exists()


@pytest.mark.parametrize(
    "settings, token",
    [
        ({}, JANE_TOKEN),  # established identity, not a creator
        ({"identity_tokens": {}, "bearer_token": SHARED_TOKEN}, SHARED_TOKEN),  # legacy shared token
    ],
)
def test_restore_needs_the_creation_gate_and_writes_nothing_otherwise(make, stub, snaps, settings, token):
    c = make()
    snapshot(c)
    stub.wipe()
    c = make(**settings)

    r = restore(c, token)

    assert r.status_code == 403
    assert stub.graphs() == set() and stub.put_calls == []


def test_restore_never_overwrites_a_dataset_that_has_content(make, stub, snaps):
    c = make()
    snapshot(c)
    triples(stub, "urn:example:data").add((URIRef("urn:later"), URIRef("urn:p"), URIRef("urn:o")))
    before = set(triples(stub, "urn:example:data"))

    r = restore(c)

    assert r.status_code == 409 and r.json()["detail"]["code"] == "dataset_not_empty"
    assert set(triples(stub, "urn:example:data")) == before and stub.put_calls == []


def test_restore_without_a_snapshot_says_so(make, stub):
    c = make()
    stub.wipe()

    r = restore(c)

    assert r.status_code == 404 and r.json()["detail"]["code"] == "no_snapshot"
    assert "seed_dataset_shapes" in r.json()["detail"]["message"]


def test_restore_of_a_dataset_fuseki_does_not_host_is_a_404(make, stub):
    c = make()
    snapshot(c)
    stub.wipe()
    stub.hosted = []

    r = restore(c)

    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_dataset"


@pytest.mark.parametrize("path", ["/admin/dataset/snapshot", "/admin/dataset/restore"])
def test_both_are_off_without_a_snapshot_directory(make, stub, path):
    c = make(snapshot_dir=None)

    r = c.post(path, json={}, headers=auth())

    assert r.status_code == 503 and r.json()["detail"]["code"] == "snapshot_dir_unset"


# --- a snapshot is never made worse ------------------------------------------------------------


def test_an_empty_dataset_is_not_snapshotted_so_good_snapshots_are_not_pushed_out(make, stub, snaps):
    c = make()
    snapshot(c)
    good = (latest(snaps) / MANIFEST).read_text()
    stub.wipe()
    # the caller is no longer admin once the data is gone, so exercise the rule directly
    conn = Conn(base_url="http://x", dataset="example", overridden=False, bank_name="local")

    with pytest.raises(SnapshotError) as exc:
        asyncio.run(take(stub, conn, Settings(snapshot_dir=snaps), by="alice"))

    assert exc.value.code == "nothing_to_snapshot"
    assert (latest(snaps) / MANIFEST).read_text() == good


def test_a_dataset_holding_only_its_audit_graph_counts_as_empty_for_a_snapshot(make, stub, snaps):
    snapshot(make())
    good = (latest(snaps) / MANIFEST).read_text()
    stub.wipe()
    triples(stub, "urn:example:audit").add((URIRef("urn:a"), URIRef("urn:p"), URIRef("urn:o")))
    conn = Conn(base_url="http://x", dataset="example", overridden=False, bank_name="local")

    with pytest.raises(SnapshotError) as exc:
        asyncio.run(take(stub, conn, Settings(snapshot_dir=snaps), by="alice"))

    assert exc.value.code == "nothing_to_snapshot"
    assert (latest(snaps) / MANIFEST).read_text() == good


def test_a_refused_call_after_a_restart_does_not_block_the_restore(make, stub, snaps):
    """The bridge audits the refusal, which creates an audit graph in the empty dataset."""
    c = make()
    snapshot(c)
    stub.wipe()
    assert restore(make(), JANE_TOKEN).status_code == 403  # audited as denied
    assert "urn:example:audit" in {str(g.identifier) for g in stub.ds.contexts() if len(g)}

    r = restore(c)

    assert r.status_code == 200, r.text
    assert stub.graphs() >= {"urn:example:holons", "urn:example:data"}


def test_restore_merges_the_audit_graph_instead_of_replacing_it(make, stub, snaps):
    c = make()
    snapshot(c)  # its own audit record is written after it returns ...
    in_snapshot = set(triples(stub, "urn:example:audit"))
    assert in_snapshot
    snapshot(c)  # ... so this second snapshot is the first to contain an audit graph
    assert "urn:example:audit" in {
        g["iri"] for g in json.loads((latest(snaps) / MANIFEST).read_text())["graphs"]
    }
    stub.wipe()
    since = (URIRef("urn:audit:since-restart"), URIRef("urn:p"), URIRef("urn:o"))
    triples(stub, "urn:example:audit").add(since)

    assert restore(c).status_code == 200

    after = set(triples(stub, "urn:example:audit"))
    assert since in after  # kept
    assert in_snapshot <= after  # and the snapshot's own records are back


def test_an_oversized_snapshot_is_refused_before_anything_is_written(make, stub, snaps):
    c = make(snapshot_max_bytes=10)

    r = snapshot(c)

    assert r.status_code == 413 and r.json()["detail"]["code"] == "snapshot_too_large"
    assert not snaps.exists()


def test_an_oversized_snapshot_leaves_the_earlier_one_alone(make, stub, snaps):
    snapshot(make())
    good = (latest(snaps) / MANIFEST).read_text()

    r = snapshot(make(snapshot_max_bytes=10))

    assert r.status_code == 413
    assert (latest(snaps) / MANIFEST).read_text() == good


# --- a tampered or failing restore ---------------------------------------------------------------


def test_a_manifest_naming_a_path_outside_the_snapshot_is_refused(make, stub, snaps):
    c = make()
    snapshot(c)
    manifest_path = latest(snaps) / MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["graphs"][0]["file"] = "../../secret.ttl"
    manifest_path.write_text(json.dumps(manifest))
    stub.wipe()

    r = restore(c)

    assert r.status_code == 500 and r.json()["detail"]["code"] == "bad_snapshot"
    assert stub.put_calls == []


def test_a_snapshot_of_another_dataset_is_refused(make, stub, snaps):
    c = make()
    snapshot(c)
    manifest_path = latest(snaps) / MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["dataset"] = "someone-else"
    manifest_path.write_text(json.dumps(manifest))
    stub.wipe()

    r = restore(c)

    assert r.status_code == 409 and r.json()["detail"]["code"] == "snapshot_mismatch"
    assert stub.put_calls == []


def test_a_restore_that_fails_part_way_leaves_the_dataset_empty_and_can_be_retried(make, stub, snaps):
    c = make()
    snapshot(c)
    names = sorted(stub.graphs())
    stub.wipe()
    stub.fail_put_on = names[-1]  # the last graph is refused

    r = restore(c)

    assert r.status_code == 502 and r.json()["detail"]["code"] == "restore_failed"
    assert stub.graphs() == set()  # the graphs already written were removed again

    stub.fail_put_on = None
    assert restore(c).status_code == 200
    assert stub.graphs() >= set(names)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden", "x" * 70])
def test_the_snapshot_name_cannot_leave_the_directory(snaps, bad):
    with pytest.raises(SnapshotError):
        _dir(Settings(snapshot_dir=snaps), bad)


# --- audit, and no way to delete ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("do", "needle"),
    [(snapshot, "POST /admin/dataset/snapshot"), (restore, "POST /admin/dataset/restore")],
)
def test_both_are_audited_even_when_the_operator_has_turned_audit_down(
    make, stub, monkeypatch, do, needle
):
    monkeypatch.setenv("HOLONBRIDGE_AUDIT_LEVEL", "denials")
    c = make()
    if do is restore:
        snapshot(c)
        stub.wipe()

    do(c)

    text = stub.ds.get_context(URIRef(audit_graph("urn:example:holons"))).serialize(format="turtle")
    assert needle in text
