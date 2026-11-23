"""Restarting Fuseki: ``restart_fuseki`` (``POST /admin/backend/restart``).

As elsewhere, each test asserts on what was (or was not) DONE -- a request written or
not, a snapshot taken, a dataset put back -- not only on a status code. The properties:

- which datasets are in-memory is read from the assembler files (a text-index wrapper is
  followed; anything unrecognised, and any hosted dataset with no file, counts as in-memory);
- an in-memory dataset that holds data and has no way back blocks the restart, unless the
  caller knowingly gives it up; a snapshot taken first, or a pinned default, is a way back;
- nothing is restarted while a write is in flight, a Fuseki backup is running, a restart is
  pending, the last one was too recent, or an assembler file does not parse;
- a committed restart takes the snapshots, writes one request, and rebuilds in-memory
  datasets afterwards -- from the pinned default when there is one -- and a rolled-back
  restart rebuilds nothing;
- a Fluree bank, or a Fuseki on another host, is refused;
- the gate is its own list and is checked before anything is created or written;
- a pinned default backup survives the pruning that removes ordinary snapshots;
- SNAPSHOT_DIR may not sit inside the nightly backup's folder.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rdflib import Dataset, URIRef

from holonbridge import fuseki_restart as fr
from holonbridge import snapshot as snaps
from holonbridge import write_lock
from holonbridge.config import Settings
from holonbridge.conn import Conn
from holonbridge.persona_state import PersonaStore
from holonbridge.server import create_app

from .test_dataset_create import FUSEKI_TEMPLATE, CreatingStub
from .test_dataset_snapshot import HostedStub

ALICE_TOKEN = "alice-identity-token-0123456789abcdef"
JANE_TOKEN = "jane-identity-token-0123456789abcdefg"

MEM_TTL = """@prefix :       <#> .
@prefix fuseki: <http://jena.apache.org/fuseki#> .
@prefix ja:     <http://jena.hpl.hp.com/2008/assembler#> .
:service a fuseki:Service ; fuseki:name "{name}" ; fuseki:dataset :ds .
:ds a ja:MemoryDataset .
"""
TDB_TTL = """@prefix :       <#> .
@prefix fuseki: <http://jena.apache.org/fuseki#> .
@prefix tdb2:   <http://jena.apache.org/2016/tdb#> .
:service a fuseki:Service ; fuseki:name "{name}" ; fuseki:dataset :ds .
:ds a tdb2:DatasetTDB2 ; tdb2:location "{location}" .
"""
TEXT_TTL = """@prefix :       <#> .
@prefix fuseki: <http://jena.apache.org/fuseki#> .
@prefix ja:     <http://jena.hpl.hp.com/2008/assembler#> .
@prefix tdb2:   <http://jena.apache.org/2016/tdb#> .
@prefix text:   <http://jena.apache.org/text#> .
:service a fuseki:Service ; fuseki:name "{name}" ; fuseki:dataset :text_dataset .
:text_dataset a text:TextDataset ; text:dataset :ds ; text:index :idx .
:ds a {inner} .
:idx a text:TextIndexLucene .
"""


class MultiStub(HostedStub):
    """Several datasets, each with its own graphs, plus what Fuseki's admin API reports."""

    def __init__(self) -> None:
        super().__init__()
        self.stores: dict[str, Dataset] = {}
        self.persistent: set[str] = set()
        self.tasks: list[dict] = []

    def seed(self, name: str, iri: str, turtle: str) -> None:
        self.stores.setdefault(name, Dataset()).get_context(URIRef(iri)).parse(
            data=turtle, format="turtle"
        )

    def wipe(self) -> None:  # a Fuseki restart: in-memory datasets come back empty
        self.stores = {k: (v if k in self.persistent else Dataset()) for k, v in self.stores.items()}

    async def list_tasks(self, conn):
        return list(self.tasks)

    async def ping(self, conn):
        return True


def _route(name: str):
    async def method(self, conn, *args, **kwargs):
        self.ds = self.stores.setdefault(conn.dataset, Dataset())
        return await getattr(HostedStub, name)(self, conn, *args, **kwargs)

    return method


for _name in ("select", "construct", "update", "get_graph", "put_graph", "post_graph", "drop_graph"):
    setattr(MultiStub, _name, _route(_name))

DATA = '<urn:t:1> <http://www.w3.org/2000/01/rdf-schema#label> "{label}" .\n'


@pytest.fixture
def stub() -> MultiStub:
    s = MultiStub()
    s.hosted = ["mem", "disk"]
    s.persistent = {"disk"}
    s.seed("mem", "urn:mem:data", DATA.format(label="in memory"))
    s.seed("disk", "urn:disk:data", DATA.format(label="on disk"))
    return s


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "configuration"
    config.mkdir()
    (config / "mem.ttl").write_text(MEM_TTL.format(name="mem"))
    (config / "disk.ttl").write_text(TDB_TTL.format(name="disk", location="databases/disk"))
    restart = tmp_path / "restart"
    restart.mkdir()
    return config, restart, tmp_path / "snapshots"


@pytest.fixture
def settings(env) -> Settings:
    config, restart, snap = env
    return Settings(
        fuseki_dataset="mem",
        fuseki_config_dir=config,
        fuseki_restart_dir=restart,
        snapshot_dir=snap,
        fuseki_restarters=("alice",),
        fuseki_restart_min_interval=60,
    )


def conn_for(dataset: str = "mem", **kw) -> Conn:
    kw.setdefault("base_url", "http://localhost:3030")
    return Conn(dataset=dataset, overridden=False, bank_name="local", **kw)


def codes(plan) -> list[str]:
    return [b["code"] for b in plan.blockers]


async def fake_helper(root: Path, stub: MultiStub, *, ok: bool = True, rolled_back: bool = False, wipe: bool = True):
    """What the root helper does: take the request, restart (empty the in-memory datasets), report."""
    deadline = time.monotonic() + 5
    while not (root / fr.REQUEST).exists():
        assert time.monotonic() < deadline, "no restart request was written"
        await asyncio.sleep(0.01)
    request = json.loads((root / fr.REQUEST).read_text())
    (root / fr.REQUEST).unlink()
    if wipe:
        stub.wipe()
    (root / fr.RESULT).write_text(
        json.dumps(
            {
                "id": request["id"],
                "ok": ok,
                "rolledBack": rolled_back,
                "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "message": "ok" if ok else "fuseki did not come back",
            }
        )
    )
    return request


async def run_commit(stub, settings, root, *, helper=None, **kw):
    kw.setdefault("by", "alice")
    kw.setdefault("reason", "test")
    kw.setdefault("poll_interval", 0.01)
    kw.setdefault("wait_seconds", 5)
    task = asyncio.create_task(helper or fake_helper(root, stub))
    try:
        return await fr.commit(stub, conn_for(), settings, **kw)
    finally:
        task.cancel()


# --- assembler detection -----------------------------------------------------------------------


def test_assembler_kinds_are_read_from_the_files(tmp_path):
    (tmp_path / "a.ttl").write_text(MEM_TTL.format(name="a"))
    (tmp_path / "b.ttl").write_text(TDB_TTL.format(name="b", location="databases/b"))
    (tmp_path / "c.ttl").write_text(TDB_TTL.format(name="c", location="--mem--"))
    (tmp_path / "d.ttl").write_text(TEXT_TTL.format(name="d", inner="ja:MemoryDataset"))
    (tmp_path / "e.ttl").write_text(TEXT_TTL.format(name="e", inner="tdb2:DatasetTDB2 ; tdb2:location \"x\""))
    (tmp_path / "f.ttl").write_text(MEM_TTL.format(name="/f").replace("ja:MemoryDataset", "ja:RDFDataset"))

    found = fr.read_assemblers(tmp_path)

    assert {n: c.kind for n, c in found.datasets.items()} == {
        "a": "memory",
        "b": "persistent",
        "c": "memory",
        "d": "memory",  # the text-index wrapper is followed to what it wraps
        "e": "persistent",
        "f": "unknown",
    }
    assert found.datasets["f"].volatile and not found.datasets["b"].volatile
    assert found.problems == []


def test_a_file_that_does_not_parse_is_reported_not_skipped(tmp_path):
    (tmp_path / "ok.ttl").write_text(MEM_TTL.format(name="ok"))
    (tmp_path / "bad.ttl").write_text("this is ( not turtle")

    found = fr.read_assemblers(tmp_path)

    assert list(found.datasets) == ["ok"]
    assert [p["file"] for p in found.problems] == ["bad.ttl"]


# --- the plan ------------------------------------------------------------------------------------


async def test_an_in_memory_dataset_with_data_and_no_way_back_blocks_the_restart(stub, settings):
    plan = await fr.build_plan(stub, conn_for(), settings, snapshot_first=False)

    assert codes(plan) == ["in_memory_no_backup"]
    assert plan.blockers[0]["dataset"] == "mem"
    assert not plan.can_restart
    kinds = {d["name"]: d["kind"] for d in plan.datasets}
    assert kinds == {"mem": "memory", "disk": "persistent"}


async def test_snapshot_first_is_a_way_back(stub, settings):
    plan = await fr.build_plan(stub, conn_for(), settings, snapshot_first=True)

    assert plan.can_restart, plan.blockers
    mem = next(d for d in plan.datasets if d["name"] == "mem")
    assert mem["backupSource"] == {"kind": "snapshot", "snapshot": "fresh", "unsavedChanges": False}


async def test_the_caller_may_knowingly_give_a_dataset_up(stub, settings):
    plan = await fr.build_plan(
        stub, conn_for(), settings, snapshot_first=False, acknowledge_loss=["mem"]
    )

    assert plan.can_restart
    assert any("will be lost" in w for w in plan.warnings)


async def test_an_empty_in_memory_dataset_needs_no_backup(stub, settings):
    stub.stores["mem"] = Dataset()

    plan = await fr.build_plan(stub, conn_for(), settings, snapshot_first=False)

    assert plan.can_restart, plan.blockers


async def test_a_hosted_dataset_with_no_assembler_file_is_treated_as_in_memory(stub, settings):
    stub.hosted.append("orphan")
    stub.seed("orphan", "urn:orphan:data", DATA.format(label="x"))

    plan = await fr.build_plan(stub, conn_for(), settings, snapshot_first=False, acknowledge_loss=["mem"])

    assert codes(plan) == ["in_memory_no_backup"]
    assert plan.blockers[0]["dataset"] == "orphan"


async def test_an_assembler_file_that_does_not_parse_blocks_every_restart(stub, settings, env):
    (env[0] / "broken.ttl").write_text("@prefix : <")

    plan = await fr.build_plan(stub, conn_for(), settings)

    assert "unparseable_assembler" in codes(plan)
    assert next(b for b in plan.blockers if b["code"] == "unparseable_assembler")["file"] == "broken.ttl"


async def test_a_write_in_flight_blocks_the_restart(stub, settings):
    async with write_lock.for_conn(conn_for("disk")):
        plan = await fr.build_plan(stub, conn_for(), settings)

    assert "write_in_flight" in codes(plan)
    assert "write_in_flight" not in codes(await fr.build_plan(stub, conn_for(), settings))


async def test_a_running_fuseki_backup_blocks_the_restart_but_a_finished_one_does_not(stub, settings):
    stub.tasks = [{"task": "Backup", "taskId": "7", "started": "x"}]
    assert "backup_running" in codes(await fr.build_plan(stub, conn_for(), settings))

    stub.tasks = [{"task": "Backup", "taskId": "7", "started": "x", "finished": "y"}]
    assert "backup_running" not in codes(await fr.build_plan(stub, conn_for(), settings))


async def test_a_pending_request_and_a_recent_restart_block_another(stub, settings, env):
    root = env[1]
    (root / fr.REQUEST).write_text("{}")
    assert "restart_in_progress" in codes(await fr.build_plan(stub, conn_for(), settings))
    (root / fr.REQUEST).unlink()

    (root / fr.LAST).write_text(
        json.dumps({"requestedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    )
    plan = await fr.build_plan(stub, conn_for(), settings)
    recent = next(b for b in plan.blockers if b["code"] == "restarted_recently")
    assert 0 < recent["retryAfterSeconds"] <= 61

    (root / fr.LAST).write_text(json.dumps({"requestedAt": "2020-01-01T00:00:00+00:00"}))
    assert "restarted_recently" not in codes(await fr.build_plan(stub, conn_for(), settings))


async def test_the_interval_can_be_turned_off(stub, settings, env):
    settings.fuseki_restart_min_interval = 0
    (env[1] / fr.LAST).write_text(
        json.dumps({"requestedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    )

    assert "restarted_recently" not in codes(await fr.build_plan(stub, conn_for(), settings))


async def test_a_fluree_bank_and_a_remote_fuseki_are_refused(stub, settings):
    fluree = await fr.build_plan(stub, conn_for(store_kind="fluree"), settings)
    remote = await fr.build_plan(stub, conn_for(base_url="https://fuseki.example.org"), settings)

    assert codes(fluree) == ["not_applicable"]
    assert codes(remote) == ["non_local_bank"]


async def test_it_refuses_to_plan_without_the_helper_directory_or_the_config_dir(stub, settings):
    settings.fuseki_restart_dir = None
    assert "restart_not_configured" in codes(await fr.build_plan(stub, conn_for(), settings))

    settings.fuseki_config_dir = None
    assert "config_dir_unset" in codes(await fr.build_plan(stub, conn_for(), settings))


async def test_a_bad_restore_from_is_refused(stub, settings):
    with pytest.raises(fr.RestartError) as exc:
        await fr.build_plan(stub, conn_for(), settings, restore_from="newest")
    assert exc.value.status == 400


# --- committing ----------------------------------------------------------------------------------


async def test_a_restart_snapshots_first_and_rebuilds_the_in_memory_dataset(stub, settings, env):
    root = env[1]
    before = set(stub.stores["mem"].get_context(URIRef("urn:mem:data")))

    result = await run_commit(stub, settings, root)

    assert result["ok"] and result["status"] == "restarted", result
    assert [s["dataset"] for s in result["snapshots"]] == ["mem"]
    assert [(r["dataset"], r["status"]) for r in result["restored"]] == [("mem", "restored")]
    assert set(stub.stores["mem"].get_context(URIRef("urn:mem:data"))) == before
    assert set(stub.stores["disk"].get_context(URIRef("urn:disk:data")))  # untouched
    assert snaps.snapshot_ids(settings, "mem") == [result["snapshots"][0]["snapshot"]]
    assert "disk" not in [s["dataset"] for s in result["snapshots"]]  # persistent: no snapshot needed


async def test_the_request_names_who_asked_and_why_and_carries_a_valid_id(stub, settings, env):
    root = env[1]
    seen: list[dict] = []

    async def helper():
        seen.append(await fake_helper(root, stub))

    await run_commit(stub, settings, root, helper=helper(), reason="new dataset needs shacl")

    assert fr.REQUEST_ID.match(seen[0]["id"])
    assert seen[0]["requestedBy"] == "alice" and seen[0]["reason"] == "new dataset needs shacl"


async def test_the_pinned_default_beats_the_newest_snapshot_under_auto(stub, settings, env):
    root = env[1]
    scoped = conn_for()
    await snaps.take(stub, scoped, settings, by="alice")
    snaps.pin_default(settings, "mem")
    # the dataset moves on after the default was pinned
    stub.stores["mem"].get_context(URIRef("urn:mem:data")).add(
        (URIRef("urn:t:2"), URIRef("urn:p"), URIRef("urn:o"))
    )

    result = await run_commit(stub, settings, root)

    assert result["restored"][0]["source"] == "default"
    restored = stub.stores["mem"].get_context(URIRef("urn:mem:data"))
    assert (URIRef("urn:t:2"), URIRef("urn:p"), URIRef("urn:o")) not in restored
    assert len(restored) == 1


async def test_latest_ignores_the_pinned_default(stub, settings, env):
    root = env[1]
    await snaps.take(stub, conn_for(), settings, by="alice")
    snaps.pin_default(settings, "mem")
    stub.stores["mem"].get_context(URIRef("urn:mem:data")).add(
        (URIRef("urn:t:2"), URIRef("urn:p"), URIRef("urn:o"))
    )

    result = await run_commit(stub, settings, root, restore_from="latest")

    assert result["restored"][0]["source"] != "default"
    assert (URIRef("urn:t:2"), URIRef("urn:p"), URIRef("urn:o")) in stub.stores["mem"].get_context(
        URIRef("urn:mem:data")
    )


async def test_a_restart_that_was_rolled_back_rebuilds_nothing_and_says_so(stub, settings, env):
    root = env[1]

    result = await run_commit(
        stub, settings, root, helper=fake_helper(root, stub, ok=False, rolled_back=True, wipe=False)
    )

    assert result["status"] == "rolled_back" and result["restarted"] is False and not result["ok"]
    assert result["restored"] == []
    assert stub.stores["mem"].get_context(URIRef("urn:mem:data"))  # data was never lost


async def test_a_silent_helper_is_reported_as_pending_not_as_success(stub, settings, env):
    result = await fr.commit(
        stub, conn_for(), settings, by="alice", reason="t", wait_seconds=0, poll_interval=0.01
    )

    assert result["status"] == "pending" and result["restarted"] is None and not result["ok"]
    assert (env[1] / fr.REQUEST).exists()  # the request really was made


async def test_a_blocked_restart_writes_nothing_and_takes_no_snapshot(stub, settings, env):
    stub.tasks = [{"task": "Backup", "taskId": "7"}]

    with pytest.raises(fr.RestartError) as exc:
        await fr.commit(stub, conn_for(), settings, by="alice", reason="t", wait_seconds=0)

    assert exc.value.status == 409 and exc.value.code == "restart_blocked"
    assert "backup_running" in [b["code"] for b in exc.value.extra["plan"]["blockers"]]
    assert not (env[1] / fr.REQUEST).exists()
    assert snaps.snapshot_ids(settings, "mem") == []


async def test_a_snapshot_that_fails_stops_the_restart_before_the_request(stub, settings, env):
    settings.snapshot_max_bytes = 10  # far too small for the dataset

    with pytest.raises(fr.RestartError) as exc:
        await fr.commit(stub, conn_for(), settings, by="alice", reason="t", wait_seconds=0)

    assert exc.value.code == "snapshot_failed" and exc.value.extra["dataset"] == "mem"
    assert not (env[1] / fr.REQUEST).exists()


async def test_a_dataset_given_up_is_reported_lost_not_failed(stub, settings, env):
    settings.snapshot_dir = None
    root = env[1]

    result = await run_commit(stub, settings, root, snapshot_first=False, acknowledge_loss=["mem"])

    assert result["restored"][0]["status"] == "lost_acknowledged"
    assert result["ok"] and result["status"] == "restarted"


# --- the default backup ----------------------------------------------------------------------------


async def test_the_pinned_default_survives_the_pruning_that_removes_snapshots(stub, settings):
    settings.snapshot_keep = 1
    scoped = conn_for()
    await snaps.take(stub, scoped, settings, by="a")
    pinned = snaps.pin_default(settings, "mem")
    for _ in range(3):
        await snaps.take(stub, scoped, settings, by="a")

    assert pinned.source not in snaps.snapshot_ids(settings, "mem")  # pruned...
    assert snaps.has_default(settings, "mem")  # ...but the default is not
    assert len(snaps.snapshot_ids(settings, "mem")) == 1


async def test_a_default_can_be_restored_by_name_and_is_replaced_by_pinning_again(stub, settings):
    scoped = conn_for()
    await snaps.take(stub, scoped, settings, by="a")
    first = snaps.pin_default(settings, "mem")
    stub.stores["mem"].get_context(URIRef("urn:mem:data")).add(
        (URIRef("urn:t:2"), URIRef("urn:p"), URIRef("urn:o"))
    )
    second_id = (await snaps.take(stub, scoped, settings, by="a")).id
    second = snaps.pin_default(settings, "mem", second_id)
    stub.wipe()

    restored = await snaps.restore(stub, scoped, settings, snapshot=snaps.DEFAULT)

    assert restored.id == snaps.DEFAULT and second.source == second_id != first.source
    assert (URIRef("urn:t:2"), URIRef("urn:p"), URIRef("urn:o")) in stub.stores["mem"].get_context(
        URIRef("urn:mem:data")
    )
    leftovers = [p.name for p in (settings.snapshot_dir / "mem").iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_pinning_with_nothing_to_pin_is_refused(settings):
    with pytest.raises(snaps.SnapshotError) as exc:
        snaps.pin_default(settings, "mem")
    assert exc.value.code == "no_snapshot"
    with pytest.raises(snaps.SnapshotError) as exc:
        snaps._read_manifest(settings, "mem", snaps.DEFAULT)
    assert exc.value.code == "no_default_backup"


def test_snapshot_dir_may_not_sit_inside_the_nightly_backup_folder(tmp_path):
    bk = tmp_path / "fuseki" / "base" / "backups"
    with pytest.raises(ValueError, match="SNAPSHOT_DIR"):
        Settings(snapshot_dir=bk / "bridge", fuseki_backup_dir=bk)
    with pytest.raises(ValueError, match="SNAPSHOT_DIR"):
        Settings(snapshot_dir=bk, fuseki_backup_dir=bk)
    Settings(snapshot_dir=tmp_path / "holon-bridge" / "backups", fuseki_backup_dir=bk)  # fine


# --- the routes ------------------------------------------------------------------------------------


@pytest.fixture
def client(stub, settings, tmp_path):
    settings.identity_tokens = {ALICE_TOKEN: "alice", JANE_TOKEN: "jane"}
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.fuseki = stub
        app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
        yield c


def auth(token: str = ALICE_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_only_a_listed_restarter_may_ask_anything(client, env):
    for call in (
        lambda h: client.post("/admin/backend/restart", json={"reason": "x"}, headers=h),
        lambda h: client.post("/admin/backend/restart", json={"reason": "x", "commit": True}, headers=h),
        lambda h: client.get("/admin/backend/status", headers=h),
    ):
        assert call(auth(JANE_TOKEN)).status_code == 403  # an established identity, not listed
        assert call({}).status_code in (401, 403)
    assert not (env[1] / fr.REQUEST).exists()


def test_a_dry_run_returns_the_plan_and_changes_nothing(client, env, settings):
    r = client.post("/admin/backend/restart", json={"reason": "x"}, headers=auth())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is False and body["canRestart"] is True
    assert {d["name"]: d["kind"] for d in body["datasets"]} == {"mem": "memory", "disk": "persistent"}
    assert not (env[1] / fr.REQUEST).exists()
    assert snaps.snapshot_ids(settings, "mem") == []


def test_a_blocked_commit_is_a_409_with_the_plan(client, stub, env):
    stub.tasks = [{"task": "Backup", "taskId": "7"}]

    r = client.post("/admin/backend/restart", json={"reason": "x", "commit": True}, headers=auth())

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "restart_blocked" and detail["plan"]["canRestart"] is False
    assert not (env[1] / fr.REQUEST).exists()


def test_a_reason_is_required(client):
    assert client.post("/admin/backend/restart", json={}, headers=auth()).status_code == 422
    assert client.post("/admin/backend/restart", json={"reason": ""}, headers=auth()).status_code == 422


def test_the_status_route_reports_kinds_tasks_and_pending(client, stub, env):
    stub.tasks = [{"task": "Backup", "taskId": "7"}]
    (env[1] / fr.REQUEST).write_text("{}")

    r = client.get("/admin/backend/status", headers=auth())

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pending"] is True and body["reachable"] is True
    assert body["tasks"] == [{"task": "Backup", "taskId": "7"}]
    assert {d["name"]: d["volatile"] for d in body["datasets"]} == {"mem": True, "disk": False}


def test_a_committed_restart_through_the_route_rebuilds_the_dataset(client, stub, env):
    root = env[1]

    def helper():
        asyncio.run(fake_helper(root, stub))

    t = threading.Thread(target=helper)
    t.start()
    r = client.post(
        "/admin/backend/restart", json={"reason": "x", "commit": True, "wait_seconds": 20}, headers=auth()
    )
    t.join()

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "restarted" and r.json()["restored"][0]["status"] == "restored"
    assert stub.stores["mem"].get_context(URIRef("urn:mem:data"))


def test_pinning_a_default_needs_admin_on_the_dataset(client, stub):
    # the stub's datasets have no identity data, so nobody is admin: the route must say no
    r = client.post("/admin/dataset/pin-default", json={}, headers=auth())

    assert r.status_code in (401, 403)


# --- creating a dataset ----------------------------------------------------------------------------


class RestartableStub(CreatingStub):
    async def list_tasks(self, conn):
        return []

    async def ping(self, conn):
        return True


@pytest.fixture
def creating(tmp_path):
    config = tmp_path / "configuration"
    config.mkdir()
    restart = tmp_path / "restart"
    restart.mkdir()
    (config / "example.ttl").write_text(FUSEKI_TEMPLATE.format(name="example"))  # a persistent dataset
    s = RestartableStub()
    s.config_dir = config
    return s, config, restart


def make_create_client(stub, config, restart, tmp_path, **extra):
    settings = Settings(
        fuseki_dataset="example",
        identity_tokens={ALICE_TOKEN: "alice", JANE_TOKEN: "jane"},
        dataset_creators=("alice", "jane"),
        fuseki_restarters=extra.pop("restarters", ("alice",)),
        fuseki_config_dir=config,
        fuseki_restart_dir=restart,
        **extra,
    )
    app = create_app(settings)
    cm = TestClient(app)
    c = cm.__enter__()
    app.state.fuseki = stub
    app.state.personas = PersonaStore(path=tmp_path / "persona-state.json")
    return cm, c


def test_creating_a_dataset_returns_the_restart_plan_and_does_not_restart(creating, tmp_path):
    stub, config, restart = creating
    cm, c = make_create_client(stub, config, restart, tmp_path)
    try:
        r = c.post("/datasets", json={"name": "fresh", "kind": "persistent"}, headers=auth())
    finally:
        cm.__exit__(None, None, None)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["restartRequired"] is True and body["restarted"] is False
    block = body["restart"]
    assert block["required"] is True and block["canRestart"] is True
    assert {d["name"] for d in block["plan"]["datasets"]} >= {"fresh"}
    assert "restart_fuseki" in block["next"]
    assert not (restart / fr.REQUEST).exists()


def test_asking_to_commit_without_the_right_creates_nothing(creating, tmp_path):
    stub, config, restart = creating
    cm, c = make_create_client(stub, config, restart, tmp_path)
    try:
        r = c.post(
            "/datasets", json={"name": "fresh", "kind": "persistent", "restart": "commit"}, headers=auth(JANE_TOKEN)
        )
    finally:
        cm.__exit__(None, None, None)

    assert r.status_code == 403
    assert stub.created == [] and not (config / "fresh.ttl").exists()


def test_creating_with_commit_restarts_when_nothing_is_in_the_way(creating, tmp_path):
    stub, config, restart = creating
    cm, c = make_create_client(stub, config, restart, tmp_path)

    def helper():
        deadline = time.monotonic() + 20
        while not (restart / fr.REQUEST).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        request = json.loads((restart / fr.REQUEST).read_text())
        (restart / fr.REQUEST).unlink()
        (restart / fr.RESULT).write_text(json.dumps({"id": request["id"], "ok": True}))

    t = threading.Thread(target=helper)
    t.start()
    try:
        r = c.post(
            "/datasets", json={"name": "fresh", "kind": "persistent", "restart": "commit"}, headers=auth()
        )
    finally:
        t.join()
        cm.__exit__(None, None, None)

    assert r.status_code == 201, r.text
    assert r.json()["restarted"] is True
    assert r.json()["restart"]["outcome"]["status"] == "restarted"
    assert stub.created == [("fresh", True)]


def test_creating_with_commit_still_creates_when_the_restart_is_blocked(creating, tmp_path):
    stub, config, restart = creating
    (restart / fr.REQUEST).write_text("{}")  # a restart is already pending
    cm, c = make_create_client(stub, config, restart, tmp_path)
    try:
        r = c.post(
            "/datasets", json={"name": "fresh", "kind": "persistent", "restart": "commit"}, headers=auth()
        )
    finally:
        cm.__exit__(None, None, None)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["restarted"] is False and stub.created == [("fresh", True)]
    assert [b["code"] for b in body["restart"]["commitSkipped"]] == ["restart_in_progress"]
