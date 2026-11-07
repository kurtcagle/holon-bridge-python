"""Export of a dataset as one zipped TriG file: ``/admin/dataset/export`` and its download link.

Each test asserts on what was (or was not) DONE -- files written, kept or removed, bytes served --
not only on the status code. The properties:

- the export is a TriG file any store can load, and loading it gives back each graph as it was;
- blank nodes stay distinct per graph, and only real blank-node labels are rewritten (never text
  inside an IRI or a string literal);
- the default graph is included when it has triples;
- the export needs admin on the dataset; an empty dataset is never exported; an oversized one
  leaves nothing behind;
- retention keeps the newest few and, with an age limit, drops the old ones, but never the newest;
- a download link is one file for a short time: any wrong part of it is the same 403, an expired
  one is refused, and it works with no bearer token;
- exports and links are audited even when the operator has turned audit down; a refused link is
  not (so an unauthenticated caller cannot write into a dataset's audit graph).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from rdflib import Dataset, URIRef
from rdflib.compare import isomorphic

from holonbridge import export as export_mod
from holonbridge.audit import audit_graph
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.export import expired_ids, scope_bnodes, take_export
from holonbridge.snapshot import SnapshotError, take

from .test_dataset_snapshot import (  # noqa: F401  (fixtures are used by name)
    ALICE_TOKEN,
    JANE_TOKEN,
    HostedStub,
    auth,
    make,
    snaps,
    stub,
    triples,
)

KEY = "export-signing-key-0123456789abcdef-0123456789"


def exports_dir(snaps: Path) -> Path:
    return snaps / "example" / "exports"


def zips(snaps: Path) -> list[Path]:
    return sorted(exports_dir(snaps).glob("*.trig.zip"))


def do_export(c, token=ALICE_TOKEN, **body):
    return c.post("/admin/dataset/export", json=body, headers=auth(token))


def read_zip(path: Path) -> tuple[str, dict]:
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        return zf.read(manifest["file"]).decode("utf-8"), manifest


def load_trig(text: str) -> Dataset:
    ds = Dataset()
    ds.parse(data=text, format="trig")
    return ds


def audit_text(stub: HostedStub) -> str:
    return stub.ds.get_context(URIRef(audit_graph("urn:example:holons"))).serialize(format="turtle")


def conn() -> Conn:
    return Conn(base_url="http://x", dataset="example", overridden=False, bank_name="local")


# --- what the export is ------------------------------------------------------------------------


def test_the_export_is_one_trig_file_that_loads_back_as_the_same_graphs(make, stub, snaps):
    triples(stub, "urn:example:data").parse(
        data='[] <urn:example:p> [ <urn:example:q> "inner" ] .', format="turtle"
    )
    triples(stub, "urn:example:more").parse(
        data='[] <urn:example:p> [ <urn:example:q> "other" ] .', format="turtle"
    )
    expected = {
        str(g.identifier): g for g in stub.ds.contexts() if len(g) and not str(g.identifier).startswith("urn:x-")
    }
    c = make()

    r = do_export(c)

    assert r.status_code == 200, r.text
    (path,) = zips(snaps)
    text, manifest = read_zip(path)
    loaded = {str(g.identifier): g for g in load_trig(text).contexts() if len(g)}
    assert set(loaded) == set(expected)
    for iri, graph in expected.items():
        assert isomorphic(loaded[iri], graph), iri
    body = r.json()
    assert body["export"] == manifest["id"] and body["graphs"] == len(expected)
    assert manifest["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest() == body["sha256"]
    assert manifest["statements"] == body["statements"] == sum(len(g) for g in expected.values())


def test_the_zip_holds_the_trig_and_a_manifest_and_nothing_else_is_left_behind(make, stub, snaps):
    do_export(make())

    (path,) = zips(snaps)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert zf.testzip() is None
    assert sorted(names) == sorted([f"example-{path.name.removesuffix('.trig.zip')}.trig", "manifest.json"])
    assert [p.name for p in exports_dir(snaps).iterdir()] == [path.name]  # no temporary file


def test_the_default_graph_is_included_when_it_has_triples(make, stub, snaps):
    stub.ds.default_context.add((URIRef("urn:example:d"), URIRef("urn:example:p"), URIRef("urn:example:o")))
    do_export(make())

    text, manifest = read_zip(zips(snaps)[0])

    loaded = load_trig(text)
    assert (URIRef("urn:example:d"), URIRef("urn:example:p"), URIRef("urn:example:o")) in loaded.default_context
    assert any(entry.get("default") for entry in manifest["graphs"])


def test_blank_nodes_with_the_same_label_in_two_graphs_stay_different_nodes(make, stub, snaps, monkeypatch):
    same = {
        "urn:example:holons": '_:b0 <urn:example:p> "holons" .\n',
        "urn:example:data": '_:b0 <urn:example:p> "data" .\n',
    }

    async def fake(self_conn, iri):
        return same.get(iri, "")

    monkeypatch.setattr(stub, "get_graph_ntriples", lambda c_, iri: fake(c_, iri))
    do_export(make())

    text, _ = read_zip(zips(snaps)[0])

    ds = load_trig(text)
    holons_subject = next(ds.get_context(URIRef("urn:example:holons")).subjects())
    data_subject = next(ds.get_context(URIRef("urn:example:data")).subjects())
    assert holons_subject != data_subject


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('_:b0 <urn:p> _:b1 .\n', '_:X_b0 <urn:p> _:X_b1 .\n'),
        ('<urn:s> <urn:p> "keep _:b0 here" .\n', '<urn:s> <urn:p> "keep _:b0 here" .\n'),
        ('<urn:s> <urn:p> "a \\" _:b0" .\n', '<urn:s> <urn:p> "a \\" _:b0" .\n'),
        ("<urn:x_:b0> <urn:p> _:b0 .\n", "<urn:x_:b0> <urn:p> _:X_b0 .\n"),
        ('<urn:s> <urn:p> "x"@en .\n', '<urn:s> <urn:p> "x"@en .\n'),
        ("<urn:s> <urn:p> _:b0.\n", "<urn:s> <urn:p> _:X_b0.\n"),  # a label cannot end in '.'
        ("# _:b0 in a comment\n_:b0 <urn:p> <urn:o> .\n", "# _:b0 in a comment\n_:X_b0 <urn:p> <urn:o> .\n"),
        (
            "<<( _:b0 <urn:p> _:b1 )>> <urn:q> <urn:o> .\n",
            "<<( _:X_b0 <urn:p> _:X_b1 )>> <urn:q> <urn:o> .\n",
        ),
        ("<< _:b0 <urn:p> <urn:o> >> <urn:q> <urn:o> .\n", "<< _:X_b0 <urn:p> <urn:o> >> <urn:q> <urn:o> .\n"),
        ('<urn:s> <urn:p> "1"^^<urn:dt_:b0> .\n', '<urn:s> <urn:p> "1"^^<urn:dt_:b0> .\n'),
    ],
)
def test_only_real_blank_node_labels_are_rewritten(text, expected):
    assert scope_bnodes(text, "X_") == expected


def test_rdf12_text_passes_through_the_export_unparsed(make, stub, snaps, monkeypatch):
    rdf12 = '<urn:s> <urn:p> <urn:o> {| <urn:by> "me" |} .\n<<( <urn:a> <urn:b> <urn:c> )>> <urn:q> "t" .\n'

    async def fake(c_, iri):
        return rdf12 if iri == "urn:example:data" else ""

    monkeypatch.setattr(stub, "get_graph_ntriples", fake)
    do_export(make())

    text, _ = read_zip(zips(snaps)[0])

    assert "<<( <urn:a> <urn:b> <urn:c> )>> <urn:q> \"t\" ." in text
    assert "{| <urn:by> \"me\" |}" in text


# --- who may, and when -------------------------------------------------------------------------


def test_export_needs_admin_on_the_dataset(make, stub, snaps):
    c = make()

    assert do_export(c, JANE_TOKEN).status_code == 403  # a Person, but not an admin
    assert not snaps.exists()


def test_being_a_creator_is_not_enough_to_export_a_whole_dataset(make, stub, snaps):
    c = make(dataset_creators=("jane",))

    assert do_export(c, JANE_TOKEN).status_code == 403
    assert not snaps.exists()


def test_listing_exports_needs_admin_too(make, stub, snaps):
    c = make()
    do_export(c)

    assert c.get("/admin/dataset/exports", headers=auth(JANE_TOKEN)).status_code == 403


def test_export_is_off_without_a_snapshot_directory(make, stub):
    c = make(snapshot_dir=None)

    r = do_export(c)

    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "snapshot_dir_unset"


# --- an export is never made worse ---------------------------------------------------------------


def test_an_empty_dataset_is_not_exported_so_a_good_export_is_not_pushed_out(make, stub, snaps):
    do_export(make())
    (good,) = zips(snaps)
    before = good.read_bytes()
    stub.wipe()

    with pytest.raises(SnapshotError) as exc:
        asyncio.run(take_export(stub, conn(), Settings(snapshot_dir=snaps), by="alice"))

    assert exc.value.code == "nothing_to_export"
    assert zips(snaps) == [good] and good.read_bytes() == before


def test_a_dataset_holding_only_its_audit_graph_counts_as_empty_for_an_export(make, stub, snaps):
    do_export(make())
    stub.wipe()
    triples(stub, "urn:example:audit").add((URIRef("urn:a"), URIRef("urn:p"), URIRef("urn:o")))

    with pytest.raises(SnapshotError) as exc:
        asyncio.run(take_export(stub, conn(), Settings(snapshot_dir=snaps), by="alice"))

    assert exc.value.code == "nothing_to_export"
    assert len(zips(snaps)) == 1


def test_an_oversized_export_is_refused_and_leaves_nothing_behind(make, stub, snaps):
    c = make(export_max_bytes=100)

    r = do_export(c)

    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "export_too_large"
    assert list(exports_dir(snaps).iterdir()) == []  # no zip and no temporary file


def test_an_oversized_export_leaves_the_earlier_one_alone(make, stub, snaps):
    do_export(make())
    (good,) = zips(snaps)
    before = good.read_bytes()

    r = do_export(make(export_max_bytes=100))

    assert r.status_code == 413
    assert zips(snaps) == [good] and good.read_bytes() == before


# --- retention ---------------------------------------------------------------------------------


def test_only_the_newest_few_exports_are_kept(make, stub, snaps):
    c = make(snapshot_keep=2)

    ids = [do_export(c).json()["export"] for _ in range(3)]

    assert [p.name for p in zips(snaps)] == [f"{i}.trig.zip" for i in sorted(ids)[1:]]


def make_old_exports(snaps: Path, *ids: str) -> None:
    directory = exports_dir(snaps)
    directory.mkdir(parents=True, exist_ok=True)
    for export_id in ids:
        (directory / f"{export_id}.trig.zip").write_bytes(b"old")


def test_exports_older_than_the_age_limit_are_removed_once_a_new_one_is_written(make, stub, snaps):
    make_old_exports(snaps, "20200101T000000Z", "20200102T000000Z")
    c = make(snapshot_keep=10, snapshot_max_age_days=30)

    new = do_export(c).json()["export"]

    assert [p.name for p in zips(snaps)] == [f"{new}.trig.zip"]


def test_without_an_age_limit_old_exports_stay_until_the_count_pushes_them_out(make, stub, snaps):
    make_old_exports(snaps, "20200101T000000Z")
    c = make(snapshot_keep=10)

    new = do_export(c).json()["export"]

    assert [p.name for p in zips(snaps)] == ["20200101T000000Z.trig.zip", f"{new}.trig.zip"]


def test_the_age_limit_never_removes_the_newest_however_old():
    ids = ["20200101T000000Z", "20200102T000000Z", "20200103T000000Z"]

    assert expired_ids(ids, 1) == ids[:-1]
    assert expired_ids(ids[:1], 1) == []
    assert expired_ids(ids, 0) == []


def test_a_failed_export_does_not_apply_the_age_limit(make, stub, snaps):
    make_old_exports(snaps, "20200101T000000Z", "20200102T000000Z")
    c = make(snapshot_keep=10, snapshot_max_age_days=30, export_max_bytes=100)

    assert do_export(c).status_code == 413

    assert len(zips(snaps)) == 2  # pruning happens only after a new export is in place


def test_the_age_limit_applies_to_snapshots_as_well(stub, snaps):
    for snap_id in ("20200101T000000Z", "20200102T000000Z"):
        (snaps / "example" / snap_id).mkdir(parents=True)
        (snaps / "example" / snap_id / "manifest.json").write_text("{}")
    settings = Settings(snapshot_dir=snaps, snapshot_keep=10, snapshot_max_age_days=30)

    taken = asyncio.run(take(stub, conn(), settings, by="alice"))

    left = sorted(p.name for p in (snaps / "example").iterdir() if p.is_dir() and p.name != "exports")
    assert left == [taken.id] and taken.kept == 1


def test_exports_do_not_show_up_as_snapshots(make, stub, snaps):
    c = make()
    do_export(c)

    r = c.get("/admin/dataset/snapshots", headers=auth())

    assert r.status_code == 200 and r.json()["count"] == 0


def test_listing_shows_the_exports_newest_first_without_contents(make, stub, snaps):
    c = make(export_signing_key=KEY)
    first = do_export(c).json()["export"]
    second = do_export(c).json()["export"]

    r = c.get("/admin/dataset/exports", headers=auth())

    body = r.json()
    assert [e["id"] for e in body["exports"]] == sorted([first, second], reverse=True)
    assert body["linksEnabled"] is True
    row = body["exports"][0]
    assert {"createdAt", "zipBytes", "graphs", "statements", "sha256", "createdBy"} <= set(row)
    listed = json.dumps(body["exports"])
    assert "urn:example:data" not in listed and "A thing" not in listed  # ids and sizes, never graph content


# --- download links ----------------------------------------------------------------------------


def test_a_link_downloads_exactly_the_export_and_needs_no_bearer_token(make, stub, snaps):
    c = make(export_signing_key=KEY)
    made = do_export(c).json()
    (path,) = zips(snaps)

    r = c.get(made["downloadPath"])  # no Authorization header

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    assert r.content == path.read_bytes()
    assert zipfile.ZipFile(io.BytesIO(r.content)).testzip() is None


def test_the_link_is_absolute_when_a_public_base_url_is_set(make, stub, snaps):
    c = make(export_signing_key=KEY, export_public_base_url="https://bridge.example.org/")

    body = do_export(c).json()

    assert body["downloadUrl"] == "https://bridge.example.org" + body["downloadPath"]


def test_without_a_signing_key_the_export_is_written_but_there_is_no_link(make, stub, snaps):
    c = make()

    body = do_export(c).json()

    assert body["ok"] is True and "downloadPath" not in body
    assert body["linkError"]["code"] == "export_links_off"
    assert len(zips(snaps)) == 1
    r = c.post(f"/admin/dataset/export/{body['export']}/link", json={}, headers=auth())
    assert r.status_code == 503 and r.json()["detail"]["code"] == "export_links_off"


def test_a_short_signing_key_is_treated_as_no_key(make, stub, snaps):
    c = make(export_signing_key="too-short")

    assert "linkError" in do_export(c).json()


def test_an_expired_link_is_refused(make, stub, snaps, monkeypatch):
    c = make(export_signing_key=KEY)
    made = do_export(c, linkTtlSeconds=60).json()
    assert c.get(made["downloadPath"]).status_code == 200
    now = export_mod._now()

    monkeypatch.setattr(export_mod, "_now", lambda: now + 3600)

    r = c.get(made["downloadPath"])
    assert r.status_code == 403


def test_a_link_cannot_be_given_a_longer_life_by_editing_it(make, stub, snaps):
    c = make(export_signing_key=KEY)
    made = do_export(c, linkTtlSeconds=60).json()
    path, query = made["downloadPath"].split("?")
    sig = query.split("sig=")[1]

    r = c.get(f"{path}?exp=99999999999&sig={sig}")

    assert r.status_code == 403


@pytest.mark.parametrize("what", ["signature", "export", "dataset", "no-signature"])
def test_every_kind_of_bad_link_is_the_same_403(make, stub, snaps, what):
    c = make(export_signing_key=KEY)
    first = do_export(c).json()
    second = do_export(c).json()
    path, query = first["downloadPath"].split("?")
    exp = query.split("&")[0].removeprefix("exp=")
    sig = query.split("sig=")[1]
    urls = {
        "signature": f"{path}?exp={exp}&sig={'0' * 64}",
        "export": f"{path.replace(first['export'], second['export'])}?exp={exp}&sig={sig}",
        "dataset": f"{path.replace('/example/', '/other/')}?exp={exp}&sig={sig}",
        "no-signature": f"{path}?exp={exp}&sig=",
    }
    reference = c.get(f"{path}?exp={exp}&sig={'f' * 64}")

    r = c.get(urls[what])

    assert r.status_code == 403
    assert r.json() == reference.json()  # nothing says which part was wrong


def test_a_link_to_an_export_that_was_pruned_is_a_404(make, stub, snaps):
    c = make(export_signing_key=KEY)
    made = do_export(c).json()
    zips(snaps)[0].unlink()

    r = c.get(made["downloadPath"])

    assert r.status_code == 404 and r.json()["detail"]["code"] == "no_export"


def test_a_link_can_be_made_later_for_an_earlier_export(make, stub, snaps):
    c = make(export_signing_key=KEY)
    first = do_export(c).json()["export"]
    do_export(c)

    r = c.post(f"/admin/dataset/export/{first}/link", json={"ttlSeconds": 120}, headers=auth())

    assert r.status_code == 200 and r.json()["expiresInSeconds"] == 120
    served = c.get(r.json()["downloadPath"])
    assert served.status_code == 200
    assert served.content == (exports_dir(snaps) / f"{first}.trig.zip").read_bytes()


def test_making_a_link_needs_admin_and_names_an_export_that_exists(make, stub, snaps):
    c = make(export_signing_key=KEY)
    export_id = do_export(c).json()["export"]

    assert c.post(f"/admin/dataset/export/{export_id}/link", json={}, headers=auth(JANE_TOKEN)).status_code == 403
    missing = c.post("/admin/dataset/export/20200101T000000Z/link", json={}, headers=auth())
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "no_export"
    bad = c.post("/admin/dataset/export/..%2F..%2Fetc/link", json={}, headers=auth())
    assert bad.status_code in (400, 404)


def test_a_link_lifetime_outside_the_allowed_range_is_refused(make, stub, snaps):
    c = make(export_signing_key=KEY)

    assert do_export(c, linkTtlSeconds=5).status_code == 422
    assert do_export(c, linkTtlSeconds=10_000_000).status_code == 422
    assert zips(snaps) == []  # refused before anything was written


# --- audit -------------------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["POST /admin/dataset/export", "POST /admin/dataset/export/{export_id}/link"])
def test_an_export_and_a_link_are_audited_even_when_audit_is_turned_down(make, stub, monkeypatch, operation):
    monkeypatch.setenv("HOLONBRIDGE_AUDIT_LEVEL", "denials")
    c = make(export_signing_key=KEY)

    export_id = do_export(c).json()["export"]
    c.post(f"/admin/dataset/export/{export_id}/link", json={}, headers=auth())

    # quoted, because the export operation's text is a prefix of the link operation's
    assert f'"{operation}"' in audit_text(stub)


def test_a_valid_download_is_audited_even_when_audit_is_turned_down(make, stub, monkeypatch):
    monkeypatch.setenv("HOLONBRIDGE_AUDIT_LEVEL", "denials")
    c = make(export_signing_key=KEY)
    made = do_export(c).json()

    c.get(made["downloadPath"])

    assert "GET /admin/dataset/export/{dataset}/{export_id}/download" in audit_text(stub)


def test_a_refused_link_writes_nothing_to_any_dataset(make, stub, snaps):
    """The dataset in the URL is the caller's until the signature checks out."""
    c = make(export_signing_key=KEY)
    export_id = do_export(c).json()["export"]
    before = {str(g.identifier): len(g) for g in stub.ds.contexts()}

    r = c.get(f"/admin/dataset/export/victim/{export_id}/download?exp=9999999999&sig={'a' * 64}")

    assert r.status_code == 403
    assert {str(g.identifier): len(g) for g in stub.ds.contexts()} == before
    assert "/download" not in audit_text(stub)
