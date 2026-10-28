"""Snapshot a dataset to files, and restore it into an empty one.

Why: an in-memory dataset loses everything when Fuseki restarts (checked live:
it comes back, with its configuration, but empty). A snapshot taken beforehand
lets the dataset be put back as it was. It is a checkpoint, not a backup
system -- it only helps if it is taken before the restart, and a persistent
dataset is the right home for anything that must not be lost.

Layout, under ``SNAPSHOT_DIR``::

    {dataset}/manifest.json     dataset name, time, who, and the graph list
    {dataset}/g0001.ttl ...     one Turtle file per named graph

Each graph is stored exactly as Fuseki serialised it and put back exactly as
stored. The bridge never parses it, so RDF 1.2 content (triple terms) passes
through untouched, which an rdflib round trip would not allow.

Rules, written down because each one guards something:

* **One snapshot per dataset, replaced atomically.** Written to a temporary
  directory and moved into place, so a crash leaves the previous snapshot
  intact rather than half of a new one.
* **An empty dataset is never snapshotted.** Doing so after a restart would
  replace the good snapshot with nothing -- the exact moment it is needed.
* **The audit graph does not count as content.** The bridge writes to it after
  its own requests, so a restarted dataset that has merely seen a refused call
  already has one. It is treated as absent when deciding whether a dataset is
  empty, kept in the snapshot, and *merged* (not replaced) on restore, so audit
  records written since the restart are not lost.
* **Restore only into an empty dataset, and only a snapshot of the same
  name.** It never overwrites: a dataset with any triples in any graph is
  refused. A restore that fails part-way removes the graphs it had written, so
  the dataset is empty again and the restore can be retried.
* **Bounded.** ``SNAPSHOT_MAX_BYTES`` caps a snapshot, checked before anything
  is written.
* **Confined.** The directory name is checked (letters, digits, hyphen,
  underscore), graph files have fixed names, and manifest entries are
  re-checked on restore, so nothing a manifest says can name a path elsewhere.
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
    graphs: int
    bytes: int
    created_at: str


async def take(client: FusekiClient, conn: Conn, settings: Settings, *, by: str) -> Taken:
    """Write a snapshot of ``conn.dataset``. Raises :class:`SnapshotError`."""
    final = _dir(settings, conn.dataset)
    graphs = await named_graphs(client, conn)
    if not [g for g in graphs if g != _audit(conn)]:
        raise SnapshotError(
            409,
            "nothing_to_snapshot",
            f"dataset {conn.dataset!r} is empty; not replacing any earlier snapshot with nothing",
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

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "version": VERSION,
        "dataset": conn.dataset,
        "createdAt": created_at,
        "createdBy": by,
        "graphs": [
            {"iri": iri, "file": f"g{i:04d}.ttl", "bytes": len(text.encode("utf-8"))}
            for i, (iri, text) in enumerate(contents, start=1)
        ],
    }

    root = final.parent
    tmp = root / f".{conn.dataset}.tmp-{os.getpid()}"
    old = root / f".{conn.dataset}.old-{os.getpid()}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        for entry, (_, text) in zip(manifest["graphs"], contents):
            (tmp / entry["file"]).write_text(text, encoding="utf-8")
        (tmp / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if final.exists():
            os.replace(final, old)
        os.replace(tmp, final)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if old.exists() and not final.exists():
            os.replace(old, final)  # put the previous snapshot back
        raise SnapshotError(500, "snapshot_write_failed", f"cannot write the snapshot: {exc}") from exc
    shutil.rmtree(old, ignore_errors=True)

    return Taken(conn.dataset, len(contents), total, created_at)


@dataclass(frozen=True)
class Restored:
    dataset: str
    graphs: list[str]
    created_at: str


def _read_manifest(settings: Settings, dataset: str) -> tuple[Path, dict[str, Any]]:
    directory = _dir(settings, dataset)
    try:
        manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SnapshotError(
            404,
            "no_snapshot",
            f"no snapshot of {dataset!r}; seed_dataset_shapes can at least restore its shapes",
        ) from exc
    except (OSError, ValueError) as exc:
        raise SnapshotError(500, "bad_snapshot", f"cannot read the snapshot manifest: {exc}") from exc

    if manifest.get("version") != VERSION or manifest.get("dataset") != dataset:
        raise SnapshotError(
            409,
            "snapshot_mismatch",
            f"the snapshot in {directory.name!r} is not a version-{VERSION} snapshot of {dataset!r}",
        )
    for entry in manifest.get("graphs", []):
        if not _GRAPH_FILE.match(str(entry.get("file", ""))) or not str(entry.get("iri", "")):
            raise SnapshotError(500, "bad_snapshot", f"unusable entry in the snapshot manifest: {entry!r}")
    return directory, manifest


async def restore(client: FusekiClient, conn: Conn, settings: Settings) -> Restored:
    """Put the snapshot of ``conn.dataset`` back into it. Raises :class:`SnapshotError`."""
    directory, manifest = _read_manifest(settings, conn.dataset)

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

    return Restored(conn.dataset, written, str(manifest.get("createdAt", "")))
