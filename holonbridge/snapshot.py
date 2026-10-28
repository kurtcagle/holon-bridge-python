"""Snapshot a dataset to files, and restore it into an empty one.

Why: an in-memory dataset loses everything when Fuseki restarts (checked live:
it comes back, with its configuration, but empty). A snapshot taken beforehand
lets the dataset be put back as it was. Snapshots also work on a persistent
dataset, as a bridge-side copy. They are a checkpoint, not a backup system:
they only help if taken beforehand, are read through the bridge's memory (see
``SNAPSHOT_MAX_BYTES``), and restore only into an empty dataset.

Layout, under ``SNAPSHOT_DIR``::

    {dataset}/{id}/manifest.json     dataset name, time, who, and the graph list
    {dataset}/{id}/g0001.ttl ...     one Turtle file per named graph

``id`` is the UTC time, ``20261028T041706Z`` (with ``-2``, ``-3`` if two are
taken in the same second), so ids sort oldest to newest. The newest
``SNAPSHOT_KEEP`` (default 5) are kept and older ones removed after a new one
has been written successfully.

Each graph is stored exactly as Fuseki serialised it and put back exactly as
stored. The bridge never parses it, so RDF 1.2 content (triple terms) passes
through untouched, which an rdflib round trip would not allow.

Rules, written down because each one guards something:

* **Written atomically, and never over an earlier snapshot.** Each snapshot is
  built in a temporary directory and moved into place; a crash leaves the
  earlier ones intact. Pruning happens only after the new one is in place.
* **An empty dataset is never snapshotted.** Doing so after a restart would
  add an empty snapshot (and, once enough accumulate, push the good ones out)
  at the exact moment they are needed.
* **The audit graph does not count as content.** The bridge writes to it after
  its own requests, so a restarted dataset that has merely seen a refused call
  already has one. It is treated as absent when deciding whether a dataset is
  empty, kept in the snapshot, and *merged* (not replaced) on restore, so audit
  records written since the restart are not lost.
* **Restore only into an empty dataset, and only a snapshot of the same
  name.** It never overwrites: a dataset with any triples in any graph is
  refused. A restore that fails part-way removes the graphs it had written, so
  the dataset is empty again and the restore can be retried.
* **Bounded.** ``SNAPSHOT_MAX_BYTES`` caps each snapshot, checked before
  anything is written; disk use is at most that times ``SNAPSHOT_KEEP``.
* **Confined.** The directory name and the snapshot id are checked against
  strict patterns, graph files have fixed names, and manifest entries are
  re-checked on restore, so nothing a caller or a manifest says can name a path
  elsewhere. Only directories whose names are snapshot ids are ever removed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import audit_graph
from .claim import is_hosted
from .config import Settings
from .conn import Conn
from .fuseki import FusekiClient, FusekiError

_DATASET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ID = re.compile(r"^\d{8}T\d{6}Z(?:-\d+)?$")
_GRAPH_FILE = re.compile(r"^g\d{4,}\.ttl$")
MANIFEST = "manifest.json"
VERSION = 1


class SnapshotError(Exception):
    """A snapshot or restore that cannot proceed, with the HTTP status to use."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def _dir(settings: Settings, dataset: str) -> Path:
    """``{SNAPSHOT_DIR}/{dataset}``, refusing anything that would leave the directory."""
    root = settings.snapshot_dir
    if root is None:
        raise SnapshotError(
            503,
            "snapshot_dir_unset",
            "snapshots are off: set SNAPSHOT_DIR to a directory on the bridge's host",
        )
    if not _DATASET.match(dataset or ""):
        raise SnapshotError(400, "bad_dataset_name", f"{dataset!r} cannot be used as a snapshot name")
    path = root / dataset
    if path.resolve().parent != root.resolve():
        raise SnapshotError(400, "bad_dataset_name", f"{dataset!r} does not stay inside SNAPSHOT_DIR")
    return path


def _id_for(created_at: datetime) -> str:
    return created_at.strftime("%Y%m%dT%H%M%SZ")


def _unused_id(directory: Path, base: str) -> str:
    candidate, n = base, 1
    while (directory / candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _migrate_flat_layout(directory: Path) -> None:
    """Move a snapshot written in the first layout (files directly in the dataset
    directory, one per dataset) into an id-named subdirectory."""
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        return
    try:
        created = datetime.fromisoformat(json.loads(manifest_path.read_text("utf-8"))["createdAt"])
        base = _id_for(created.astimezone(timezone.utc))
    except (OSError, ValueError, KeyError):
        base = "19700101T000000Z"
    dest = directory / _unused_id(directory, base)
    dest.mkdir()
    for item in [manifest_path, *directory.glob("g*.ttl")]:
        if item.is_file() and (item.name == MANIFEST or _GRAPH_FILE.match(item.name)):
            os.replace(item, dest / item.name)


def snapshot_ids(settings: Settings, dataset: str) -> list[str]:
    """The ids of this dataset's snapshots, oldest first."""
    directory = _dir(settings, dataset)
    if not directory.is_dir():
        return []
    _migrate_flat_layout(directory)
    return sorted(
        p.name for p in directory.iterdir() if _ID.match(p.name) and (p / MANIFEST).is_file()
    )


def list_snapshots(settings: Settings, dataset: str) -> list[dict[str, Any]]:
    """What snapshots of ``dataset`` exist, newest first."""
    out: list[dict[str, Any]] = []
    for snap_id in reversed(snapshot_ids(settings, dataset)):
        try:
            manifest = json.loads((_dir(settings, dataset) / snap_id / MANIFEST).read_text("utf-8"))
        except (OSError, ValueError):
            out.append({"id": snap_id, "unreadable": True})
            continue
        graphs = manifest.get("graphs", [])
        out.append(
            {
                "id": snap_id,
                "createdAt": manifest.get("createdAt"),
                "createdBy": manifest.get("createdBy"),
                "graphs": len(graphs),
                "bytes": sum(int(g.get("bytes", 0)) for g in graphs),
            }
        )
    return out


def _audit(conn: Conn) -> str:
    return audit_graph(conn.holons_graph)


async def named_graphs(client: FusekiClient, conn: Conn) -> list[str]:
    """Every named graph that holds at least one triple, sorted."""
    result = await client.select(
        conn, "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g"
    )
    bindings = result.get("results", {}).get("bindings", [])
    return sorted(b["g"]["value"] for b in bindings if "g" in b)


async def _is_empty(client: FusekiClient, conn: Conn) -> bool:
    """No triples anywhere except, possibly, the audit graph."""
    result = await client.select(
        conn, f"ASK {{ GRAPH ?g {{ ?s ?p ?o }} FILTER(?g != <{_audit(conn)}>) }}"
    )
    return not result.get("boolean")


@dataclass(frozen=True)
class Taken:
    dataset: str
    id: str
    graphs: int
    bytes: int
    created_at: str
    kept: int


async def take(client: FusekiClient, conn: Conn, settings: Settings, *, by: str) -> Taken:
    """Write a snapshot of ``conn.dataset``. Raises :class:`SnapshotError`."""
    directory = _dir(settings, conn.dataset)
    graphs = await named_graphs(client, conn)
    if not [g for g in graphs if g != _audit(conn)]:
        raise SnapshotError(
            409,
            "nothing_to_snapshot",
            f"dataset {conn.dataset!r} is empty; not adding an empty snapshot",
        )

    contents: list[tuple[str, str]] = []
    total = 0
    for iri in graphs:
        text = await client.get_graph(conn, iri)
        total += len(text.encode("utf-8"))
        if total > settings.snapshot_max_bytes:
            raise SnapshotError(
                413,
                "snapshot_too_large",
                f"dataset {conn.dataset!r} is larger than SNAPSHOT_MAX_BYTES="
                f"{settings.snapshot_max_bytes}; nothing was written",
            )
        contents.append((iri, text))

    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _migrate_flat_layout(directory)
        snap_id = _unused_id(directory, _id_for(now))
        manifest = {
            "version": VERSION,
            "id": snap_id,
            "dataset": conn.dataset,
            "createdAt": created_at,
            "createdBy": by,
            "graphs": [
                {"iri": iri, "file": f"g{i:04d}.ttl", "bytes": len(text.encode("utf-8"))}
                for i, (iri, text) in enumerate(contents, start=1)
            ],
        }
        tmp = directory / f".{snap_id}.tmp-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        try:
            for entry, (_, text) in zip(manifest["graphs"], contents):
                (tmp / entry["file"]).write_text(text, encoding="utf-8")
            (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            os.replace(tmp, directory / snap_id)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    except OSError as exc:
        raise SnapshotError(500, "snapshot_write_failed", f"cannot write the snapshot: {exc}") from exc

    # Only now, with the new one safely in place, drop the oldest beyond the limit.
    ids = snapshot_ids(settings, conn.dataset)
    keep = max(1, settings.snapshot_keep)
    for old in ids[:-keep]:
        shutil.rmtree(directory / old, ignore_errors=True)

    return Taken(conn.dataset, snap_id, len(contents), total, created_at, kept=min(len(ids), keep))


@dataclass(frozen=True)
class Restored:
    dataset: str
    id: str
    graphs: list[str]
    created_at: str


def _read_manifest(settings: Settings, dataset: str, snapshot: str | None) -> tuple[Path, dict[str, Any]]:
    directory = _dir(settings, dataset)
    if snapshot is not None and not _ID.match(snapshot):
        raise SnapshotError(400, "bad_snapshot_id", f"{snapshot!r} is not a snapshot id")
    ids = snapshot_ids(settings, dataset)
    if not ids or (snapshot is not None and snapshot not in ids):
        have = f"; it has {', '.join(reversed(ids))}" if ids else ""
        raise SnapshotError(
            404,
            "no_snapshot",
            f"no snapshot{' ' + snapshot if snapshot else ''} of {dataset!r}{have}; "
            "seed_dataset_shapes can at least restore its shapes",
        )
    chosen = snapshot or ids[-1]
    snap_dir = directory / chosen
    try:
        manifest = json.loads((snap_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(500, "bad_snapshot", f"cannot read the snapshot manifest: {exc}") from exc

    if manifest.get("version") != VERSION or manifest.get("dataset") != dataset:
        raise SnapshotError(
            409,
            "snapshot_mismatch",
            f"snapshot {chosen!r} is not a version-{VERSION} snapshot of {dataset!r}",
        )
    for entry in manifest.get("graphs", []):
        if not _GRAPH_FILE.match(str(entry.get("file", ""))) or not str(entry.get("iri", "")):
            raise SnapshotError(500, "bad_snapshot", f"unusable entry in the snapshot manifest: {entry!r}")
    manifest["_id"] = chosen
    return snap_dir, manifest


async def restore(
    client: FusekiClient, conn: Conn, settings: Settings, *, snapshot: str | None = None
) -> Restored:
    """Put a snapshot of ``conn.dataset`` (the newest unless ``snapshot`` names one) back into it.

    Raises :class:`SnapshotError`.
    """
    directory, manifest = _read_manifest(settings, conn.dataset, snapshot)

    if await is_hosted(client, conn) is False:
        raise SnapshotError(
            404, "unknown_dataset", f"dataset {conn.dataset!r} does not exist on this server"
        )
    if not await _is_empty(client, conn):
        raise SnapshotError(
            409,
            "dataset_not_empty",
            f"dataset {conn.dataset!r} already has content; restore only fills an empty dataset "
            "and never overwrites",
        )

    texts: list[tuple[str, str]] = []
    for entry in manifest["graphs"]:
        try:
            texts.append((entry["iri"], (directory / entry["file"]).read_text(encoding="utf-8")))
        except OSError as exc:
            raise SnapshotError(500, "bad_snapshot", f"cannot read {entry['file']}: {exc}") from exc

    audit = _audit(conn)
    written: list[str] = []
    try:
        for iri, text in texts:
            if iri == audit:
                await client.post_graph(conn, iri, text)  # merge: keep records written since the restart
            else:
                await client.put_graph(conn, iri, text)
            written.append(iri)
    except FusekiError as exc:
        # Leave the dataset empty again so the restore can simply be retried.
        # The audit graph is never dropped: it holds records of this very attempt.
        for iri in written:
            if iri == audit:
                continue
            try:
                await client.drop_graph(conn, iri)
            except FusekiError:
                pass
        raise SnapshotError(
            502,
            "restore_failed",
            f"Fuseki refused a graph while restoring {conn.dataset!r}; the graphs already written "
            "were removed again",
            upstream=exc.as_dict(),
        ) from exc

    return Restored(conn.dataset, manifest["_id"], written, str(manifest.get("createdAt", "")))
