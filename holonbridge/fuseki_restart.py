"""Restarting Fuseki, safely: ``POST /admin/backend/restart``.

The bridge used to say it never restarts Fuseki. That stance is reversed here, for one
reason that is now met: per-caller identity reaches the REST layer, so a restart can be
gated on *who is asking* (``FUSEKI_RESTARTERS``) rather than on one shared token. Everything
below exists because a restart is the one operation that can lose data without anyone
writing anything.

What a restart does to a dataset:

``persistent``  a TDB2 dataset on disk; comes back as it was.
``memory``      comes back with its configuration but EMPTY: data, seeded shapes and owner gone.

So the tool works out which datasets are in-memory, makes sure each one that holds data has
something to be rebuilt from, restarts, and rebuilds them.

How the restart itself happens. The bridge does not run as root and does not call systemctl.
It writes ``restart-fuseki.request`` into ``FUSEKI_RESTART_DIR``; a systemd path unit
(``deploy/fuseki-restart/``) fires a small root helper, which keeps a ``last-known-good/``
copy of Fuseki's configuration, restarts the service, and -- if Fuseki is not active within
60 s -- puts the last good configuration back and restarts again. It reports through
``restart-fuseki.result.json``. Nothing in the request is ever executed; the helper reads one
validated id from it and nothing else.

Rules, each guarding something:

* **Dry run by default.** ``commit=false`` returns the plan -- datasets, kinds, backup source,
  blockers -- and changes nothing.
* **Detect volatility from the assembler files, not from a naming convention.** A dataset is
  persistent only if its configuration says so (a TDB location). A ``ja:MemoryDataset``, a
  ``--mem--`` location, anything unrecognised, and any hosted dataset with no configuration
  file at all are treated as in-memory, because the cost of guessing wrong the other way is
  lost data. A ``text:TextDataset`` wrapper is followed through to the dataset it wraps.
* **One bad assembler file blocks the restart.** Fuseki aborts the *whole server* on a file it
  cannot parse, so restarting with one present would take down every dataset.
* **An in-memory dataset that holds data needs a way back**, or the restart is refused: a
  pinned default backup, a snapshot, or a fresh snapshot taken first (``snapshot_first``). The
  caller may knowingly give one up with ``acknowledge_loss``.
* **No restart while a write is in flight, a Fuseki backup is running, another restart is
  pending, or the last one was less than ``FUSEKI_RESTART_MIN_INTERVAL`` ago.** The nightly
  backup script aborts the whole run if Fuseki stops answering mid-dump.
* **Fuseki only, and only a local one.** A Fluree bank, or a Fuseki on another host, is
  refused: this tool restarts the service on the bridge's own machine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rdflib import RDF, Graph, Namespace

from . import snapshot as snaps
from . import write_lock
from .config import Settings
from .conn import Conn
from .fuseki import FusekiClient, FusekiError

log = logging.getLogger(__name__)

FUSEKI = Namespace("http://jena.apache.org/fuseki#")
JA = Namespace("http://jena.hpl.hp.com/2008/assembler#")
TDB1 = Namespace("http://jena.hpl.hp.com/2008/tdb#")
TDB2 = Namespace("http://jena.apache.org/2016/tdb#")
TEXT = Namespace("http://jena.apache.org/text#")

REQUEST = "restart-fuseki.request"
PROCESSING = "restart-fuseki.processing"
RESULT = "restart-fuseki.result.json"
LAST = "restart-fuseki.last"

#: Request ids are minted here and re-validated by the helper with the same pattern.
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_LOCK_WAIT_SECONDS = 10.0


class RestartError(Exception):
    """A restart that cannot proceed, with the HTTP status the route should use."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


# --- assembler detection ------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetConfig:
    name: str
    #: ``persistent``, ``memory`` or ``unknown`` (treated as memory).
    kind: str
    file: str
    reason: str

    @property
    def volatile(self) -> bool:
        return self.kind != "persistent"


@dataclass(frozen=True)
class AssemblerReport:
    datasets: dict[str, DatasetConfig]
    #: ``{"file": ..., "error": ...}`` for every file that could not be read or parsed.
    problems: list[dict[str, str]]


def _strip(name: str) -> str:
    return name.lstrip("/")


def _classify_node(g: Graph, node: Any) -> tuple[str, str]:
    """``(kind, reason)`` for the dataset resource ``node``, following a text-index wrapper."""
    seen: set[Any] = set()
    while node is not None and node not in seen:
        seen.add(node)
        types = set(g.objects(node, RDF.type))
        if TEXT.TextDataset in types:
            inner = g.value(node, TEXT.dataset)
            if inner is None:
                return "unknown", "text:TextDataset with no text:dataset"
            node = inner
            continue
        if JA.MemoryDataset in types:
            return "memory", "ja:MemoryDataset"
        if types & {TDB2.DatasetTDB2, TDB1.DatasetTDB, TDB1.DatasetTDB2}:
            location = g.value(node, TDB2.location) or g.value(node, TDB1.location)
            if location is None:
                return "unknown", "a TDB dataset with no location"
            if "--mem--" in str(location):
                return "memory", f"TDB location {location}"
            return "persistent", f"TDB location {location}"
        return "unknown", "dataset type not recognised: " + (
            ", ".join(sorted(str(t) for t in types)) or "no rdf:type"
        )
    return "unknown", "dataset reference does not resolve"


def read_assemblers(config_dir: Path) -> AssemblerReport:
    """Parse every ``*.ttl`` in Fuseki's configuration directory and classify its datasets."""
    datasets: dict[str, DatasetConfig] = {}
    problems: list[dict[str, str]] = []
    for path in sorted(config_dir.glob("*.ttl")):
        try:
            g = Graph()
            g.parse(data=path.read_text(encoding="utf-8"), format="turtle")
        except Exception as exc:  # noqa: BLE001 -- rdflib raises many types; all mean "unusable"
            problems.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for service in set(g.subjects(FUSEKI.name, None)):
            names = [str(o) for o in g.objects(service, FUSEKI.name)]
            node = g.value(service, FUSEKI.dataset)
            kind, reason = _classify_node(g, node) if node is not None else (
                "unknown",
                "service has no fuseki:dataset",
            )
            for name in names:
                datasets[_strip(name)] = DatasetConfig(_strip(name), kind, path.name, reason)
    return AssemblerReport(datasets, problems)


# --- plan ----------------------------------------------------------------------------------


def _is_local(conn: Conn) -> bool:
    return (urlparse(conn.base_url).hostname or "") in _LOCAL_HOSTS


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def last_restart_epoch(settings: Settings) -> float | None:
    """When the last restart was requested or finished, from the files in the restart directory."""
    root = settings.fuseki_restart_dir
    if root is None:
        return None
    stamps: list[float] = []
    for name in (LAST, RESULT):
        data = _read_json(root / name)
        for key in ("requestedAt", "finishedAt"):
            raw = (data or {}).get(key)
            if isinstance(raw, str):
                try:
                    stamps.append(datetime.fromisoformat(raw).timestamp())
                except ValueError:
                    pass
    return max(stamps) if stamps else None


def choose_source(
    settings: Settings, dataset: str, restore_from: str, will_snapshot: bool
) -> dict[str, Any] | None:
    """Where an in-memory dataset would be rebuilt from, or ``None`` if there is nothing.

    ``auto``: the pinned default if there is one, otherwise the newest snapshot (which is the
    fresh one when ``snapshot_first`` is on). ``latest`` ignores the default. ``default``
    insists on it.
    """
    pinned = snaps.has_default(settings, dataset)
    newest = snaps.newest_id(settings, dataset)
    fresh = will_snapshot and settings.snapshot_dir is not None
    if restore_from == "default" or (restore_from == "auto" and pinned):
        return {"kind": "default", "unsavedChanges": True} if pinned else None
    if fresh:
        return {"kind": "snapshot", "snapshot": "fresh", "unsavedChanges": False}
    if newest:
        return {"kind": "snapshot", "snapshot": newest, "unsavedChanges": True}
    return None


@dataclass
class Plan:
    blockers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    datasets: list[dict[str, Any]] = field(default_factory=list)
    assemblers: dict[str, Any] = field(default_factory=dict)

    @property
    def can_restart(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "canRestart": self.can_restart,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "datasets": self.datasets,
            "assemblers": self.assemblers,
        }


def _blocker(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


async def _has_content(client: FusekiClient, conn: Conn, name: str) -> bool | None:
    """Whether ``name`` holds any triples besides the audit graph; ``None`` if Fuseki cannot say."""
    try:
        return not await snaps._is_empty(client, replace(conn, dataset=name, overridden=True))
    except FusekiError:
        return None


async def build_plan(
    client: FusekiClient,
    conn: Conn,
    settings: Settings,
    *,
    snapshot_first: bool = True,
    restore_from: str = "auto",
    acknowledge_loss: tuple[str, ...] | list[str] = (),
) -> Plan:
    """Work out what a restart would do and what stands in its way. Changes nothing."""
    if restore_from not in ("auto", "latest", "default"):
        raise RestartError(400, "bad_restore_from", "restore_from must be auto, latest or default")
    plan = Plan()
    acknowledged = set(acknowledge_loss)

    if settings.store_kind != "fuseki" or conn.store_kind != "fuseki":
        plan.blockers.append(
            _blocker(
                "not_applicable",
                f"this tool restarts Fuseki; the active store is {conn.store_kind!r}",
            )
        )
        return plan
    if not _is_local(conn):
        plan.blockers.append(
            _blocker(
                "non_local_bank",
                f"bank {conn.bank_name!r} points at {conn.base_url}, which is not on this host; "
                "only the Fuseki next to the bridge can be restarted",
            )
        )
        return plan
    if settings.fuseki_restart_dir is None:
        plan.blockers.append(
            _blocker(
                "restart_not_configured",
                "FUSEKI_RESTART_DIR is not set, so there is no helper to ask for a restart",
            )
        )
    if settings.fuseki_config_dir is None:
        plan.blockers.append(
            _blocker(
                "config_dir_unset",
                "FUSEKI_CONFIG_DIR is not set, so the bridge cannot tell which datasets are "
                "in-memory",
            )
        )
        return plan

    report = read_assemblers(settings.fuseki_config_dir)
    plan.assemblers = {
        "configDir": str(settings.fuseki_config_dir),
        "files": len(report.datasets),
        "problems": report.problems,
    }
    for problem in report.problems:
        plan.blockers.append(
            _blocker(
                "unparseable_assembler",
                f"{problem['file']} cannot be parsed ({problem['error']}); Fuseki aborts the "
                "whole server on a file like that, so every dataset would go down",
                file=problem["file"],
            )
        )

    try:
        hosted = await client.list_datasets(conn)
    except FusekiError as exc:
        plan.blockers.append(
            _blocker(
                "admin_api_unavailable",
                "Fuseki's /$/datasets admin API did not answer, so the datasets cannot be listed",
                upstream=exc.as_dict(),
            )
        )
        return plan

    for entry in hosted:
        name = entry["name"]
        cfg = report.datasets.get(name)
        if cfg is None:
            kind, volatile, reason = (
                "unknown",
                True,
                "no assembler file declares it; treated as in-memory",
            )
        else:
            kind, volatile, reason = cfg.kind, cfg.volatile, cfg.reason
        row: dict[str, Any] = {"name": name, "kind": kind, "volatile": volatile, "reason": reason}
        if volatile:
            content = await _has_content(client, conn, name)
            row["hasContent"] = content
            if content is None:
                plan.warnings.append(
                    f"could not check whether {name!r} holds data; treating it as if it does"
                )
            if content is not False:
                source = choose_source(settings, name, restore_from, snapshot_first)
                row["backupSource"] = source
                if source is None and name in acknowledged:
                    row["dataLoss"] = "acknowledged"
                    plan.warnings.append(f"{name!r} has no backup and will be lost (acknowledged)")
                elif source is None:
                    plan.blockers.append(
                        _blocker(
                            "in_memory_no_backup",
                            f"{name!r} is in-memory, holds data, and has no pinned default, no "
                            "snapshot and no snapshot_first to take one. Restarting would empty "
                            "it. Take a snapshot, or list it in acknowledge_loss to give it up",
                            dataset=name,
                        )
                    )
        plan.datasets.append(row)

    in_flight = write_lock.locked_datasets(conn.base_url)
    if in_flight:
        plan.blockers.append(
            _blocker(
                "write_in_flight",
                f"a write is in progress on {', '.join(in_flight)}; try again in a moment",
                datasets=in_flight,
            )
        )

    try:
        tasks = await client.list_tasks(conn)
    except FusekiError:
        plan.warnings.append(
            "could not read Fuseki's task list, so a running backup would not be noticed"
        )
    else:
        running = [t for t in tasks if "finished" not in t]
        if running:
            plan.blockers.append(
                _blocker(
                    "backup_running",
                    "a Fuseki task is still running ("
                    + ", ".join(f"{t.get('task', '?')} {t.get('taskId', '')}".strip() for t in running)
                    + "); a restart now would break it. The nightly backup script aborts its whole "
                    "run if Fuseki stops answering mid-dump",
                    tasks=[t.get("taskId") for t in running],
                )
            )

    root = settings.fuseki_restart_dir
    if root is not None:
        if (root / REQUEST).exists() or (root / PROCESSING).exists():
            plan.blockers.append(
                _blocker("restart_in_progress", "a restart request is already waiting or running")
            )
        last = last_restart_epoch(settings)
        if last is not None and settings.fuseki_restart_min_interval > 0:
            wait = last + settings.fuseki_restart_min_interval - time.time()
            if wait > 0:
                plan.blockers.append(
                    _blocker(
                        "restarted_recently",
                        f"Fuseki was restarted at {_iso(last)}; the minimum interval is "
                        f"{settings.fuseki_restart_min_interval} s",
                        retryAfterSeconds=int(wait) + 1,
                    )
                )
    return plan


# --- commit --------------------------------------------------------------------------------


@asynccontextmanager
async def _writes_held(conn: Conn, names: list[str]):
    """Hold the write lock of every named dataset, so a snapshot is exact and no write races
    the request. Raises :class:`RestartError` if one cannot be had promptly."""
    async with AsyncExitStack() as stack:
        for name in sorted(names):
            lock = write_lock.for_conn(replace(conn, dataset=name, overridden=True))
            try:
                await asyncio.wait_for(lock.acquire(), timeout=_LOCK_WAIT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise RestartError(
                    409, "write_in_flight", f"a write on {name!r} did not finish; nothing was restarted"
                ) from exc
            stack.callback(lock.release)
        yield


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


async def _wait_for_result(root: Path, request_id: str, seconds: float, poll: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + seconds
    while True:
        data = _read_json(root / RESULT)
        if data and data.get("id") == request_id:
            return data
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll)


async def _wait_for_fuseki(client: FusekiClient, conn: Conn, seconds: float, poll: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            await client.list_datasets(conn)
            return True
        except Exception as exc:  # noqa: BLE001 -- connection refused, timeouts, 5xx: all "not yet"
            log.debug("fuseki not answering yet: %s", exc)
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll)


async def commit(
    client: FusekiClient,
    conn: Conn,
    settings: Settings,
    *,
    by: str,
    reason: str,
    snapshot_first: bool = True,
    restore_from: str = "auto",
    acknowledge_loss: tuple[str, ...] | list[str] = (),
    wait_seconds: int = 90,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Restart Fuseki and rebuild its in-memory datasets. Raises :class:`RestartError`.

    Re-plans first (the world may have changed since a dry run); any blocker stops it with 409
    and the plan. Then, with writes held: snapshots each in-memory dataset that holds data
    (when ``snapshot_first``), and writes the request. Writes are released before the wait.
    """
    plan = await build_plan(
        client,
        conn,
        settings,
        snapshot_first=snapshot_first,
        restore_from=restore_from,
        acknowledge_loss=acknowledge_loss,
    )
    if not plan.can_restart:
        first = plan.blockers[0]
        status = 409
        raise RestartError(status, "restart_blocked", first["message"], plan=plan.to_dict())

    root = settings.fuseki_restart_dir
    assert root is not None  # build_plan blocks otherwise
    acknowledged = set(acknowledge_loss)
    volatile_with_data = [
        d["name"] for d in plan.datasets if d["volatile"] and d.get("hasContent") is not False
    ]
    request_id = uuid.uuid4().hex
    assert REQUEST_ID.match(request_id)
    taken: list[dict[str, Any]] = []

    async with _writes_held(conn, [d["name"] for d in plan.datasets]):
        if snapshot_first:
            for name in volatile_with_data:
                if settings.snapshot_dir is None:
                    break
                scoped = replace(conn, dataset=name, overridden=True)
                try:
                    done = await snaps.take(client, scoped, settings, by=by)
                except snaps.SnapshotError as exc:
                    if name in acknowledged:
                        taken.append({"dataset": name, "error": exc.code})
                        continue
                    raise RestartError(
                        exc.status,
                        "snapshot_failed",
                        f"could not snapshot {name!r} ({exc.message}); nothing was restarted",
                        dataset=name,
                        cause=exc.detail(),
                    ) from exc
                taken.append({"dataset": name, "snapshot": done.id, "graphs": done.graphs, "bytes": done.bytes})

        requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        request = {"id": request_id, "requestedAt": requested_at, "requestedBy": by, "reason": reason}
        try:
            (root / RESULT).unlink(missing_ok=True)
            _write_atomic(root / LAST, request)
            _write_atomic(root / REQUEST, request)
        except OSError as exc:
            raise RestartError(
                500, "request_write_failed", f"cannot write the restart request: {exc}"
            ) from exc

    out: dict[str, Any] = {
        "ok": False,
        "committed": True,
        "id": request_id,
        "requestedAt": requested_at,
        "snapshots": taken,
        "restored": [],
    }
    started = time.monotonic()
    helper = await _wait_for_result(root, request_id, wait_seconds, poll_interval)
    if helper is None:
        out.update(
            status="pending",
            restarted=None,
            message=f"the restart helper had not reported after {wait_seconds} s; check "
            "get_backend_status, or the helper's journal (holonbridge-restart-fuseki.service)",
        )
        return out
    out["helper"] = helper
    if not helper.get("ok"):
        out.update(
            status="rolled_back" if helper.get("rolledBack") else "failed",
            restarted=False,
            message=helper.get("message")
            or "Fuseki did not come back healthy; see the helper's journal",
        )
        return out

    remaining = max(1.0, wait_seconds - (time.monotonic() - started))
    if not await _wait_for_fuseki(client, conn, remaining, poll_interval):
        out.update(
            status="pending",
            restarted=True,
            message="the helper restarted Fuseki but it is not answering yet; datasets were not rebuilt",
        )
        return out

    out["restored"] = await _rebuild(client, conn, settings, volatile_with_data, restore_from, acknowledged)
    failed = [r for r in out["restored"] if r.get("error")]
    out.update(
        ok=not failed,
        status="restarted",
        restarted=True,
        message="Fuseki restarted"
        + ("" if not failed else f"; {len(failed)} dataset(s) could not be rebuilt"),
    )
    return out


async def _rebuild(
    client: FusekiClient,
    conn: Conn,
    settings: Settings,
    names: list[str],
    restore_from: str,
    acknowledged: set[str],
) -> list[dict[str, Any]]:
    """Put each in-memory dataset that had data back from its backup, if it came back empty."""
    results: list[dict[str, Any]] = []
    for name in names:
        scoped = replace(conn, dataset=name, overridden=True)
        # After the restart the freshly taken snapshot is the newest one, so "auto" without a
        # pinned default simply means the newest.
        pinned = snaps.has_default(settings, name)
        use_default = restore_from == "default" or (restore_from == "auto" and pinned)
        try:
            if not await snaps._is_empty(client, scoped):
                results.append({"dataset": name, "status": "already_has_content"})
                continue
            restored = await snaps.restore(
                client, scoped, settings, snapshot=snaps.DEFAULT if use_default else None
            )
        except snaps.SnapshotError as exc:
            status = "lost_acknowledged" if name in acknowledged and exc.code in (
                "no_snapshot",
                "no_default_backup",
                "snapshot_dir_unset",
            ) else "failed"
            results.append({"dataset": name, "status": status, "error": exc.code if status == "failed" else None, "message": exc.message})
            continue
        except FusekiError as exc:
            results.append({"dataset": name, "status": "failed", "error": "fuseki", "message": exc.message})
            continue
        results.append(
            {
                "dataset": name,
                "status": "restored",
                "source": restored.id,
                "graphs": len(restored.graphs),
                "snapshotCreatedAt": restored.created_at,
            }
        )
    return results


# --- status ---------------------------------------------------------------------------------


async def backend_status(client: FusekiClient, conn: Conn, settings: Settings) -> dict[str, Any]:
    """Read-only: what a restart would face, without a plan's blockers logic being applied."""
    out: dict[str, Any] = {"store": conn.store_kind, "bank": conn.bank_name, "local": _is_local(conn)}
    root = settings.fuseki_restart_dir
    out["restartConfigured"] = root is not None and settings.fuseki_config_dir is not None
    if root is not None:
        out["pending"] = (root / REQUEST).exists() or (root / PROCESSING).exists()
        out["lastResult"] = _read_json(root / RESULT)
        last = last_restart_epoch(settings)
        out["lastRestartAt"] = _iso(last) if last else None
    if conn.store_kind == "fuseki":
        out["reachable"] = await client.ping(conn)
        try:
            out["tasks"] = await client.list_tasks(conn)
        except FusekiError:
            out["tasks"] = None
        if settings.fuseki_config_dir is not None:
            report = read_assemblers(settings.fuseki_config_dir)
            out["assemblerProblems"] = report.problems
            out["datasets"] = [
                {"name": c.name, "kind": c.kind, "volatile": c.volatile, "file": c.file}
                for c in sorted(report.datasets.values(), key=lambda c: c.name)
            ]
    return out
