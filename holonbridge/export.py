"""Export a whole dataset as one zipped TriG file, and hand it out on an expiring link.

Why: :mod:`holonbridge.snapshot` is a checkpoint for restoring into the *same* host. Its
output is a directory of Turtle files on the bridge's own disk, and nothing returns them.
If that host is the machine that has failed, the snapshots went with it. An export is
the portable counterpart: one ``{id}.trig.zip`` any triple store can load, which can be
fetched from the bridge (or copied off the host) and kept elsewhere.

Layout, under ``SNAPSHOT_DIR``::

    {dataset}/exports/{id}.trig.zip
        {dataset}-{id}.trig     every named graph as a ``GRAPH <iri> { ... }`` block,
                                and the default graph, if it has triples, as ``{ ... }``
        manifest.json           dataset, time, who, sha256 of the TriG, per-graph counts

``id`` is the UTC time, the same scheme as snapshots, so ids sort oldest to newest.
Exports sit beside the snapshot directories but are not snapshots: ``snapshot_ids`` only
looks at id-named directories and never sees ``exports``.

Rules, written down because each one guards something:

* **The bridge still never parses a graph.** Each graph is fetched as N-Triples, which is
  line-based and has no prefixes, and wrapped in a ``GRAPH`` block as text. (Turtle could
  not be wrapped: a prefix directive may not sit inside a TriG graph block, and two graphs
  can bind a prefix differently.) N-Triples is valid inside TriG, and RDF 1.2 triple terms
  pass through as text.
* **Blank nodes are scoped per graph.** A blank-node label means "the same node" across a
  whole TriG document, but Fuseki labels blank nodes per serialisation, so two graphs can
  both contain ``_:b0`` meaning different nodes. Loading the naive concatenation would merge
  them. Every label is therefore prefixed with its graph's number (``_:g0002_b0``), by a
  small tokeniser that skips IRIs and string literals so it never rewrites text that merely
  looks like a label.
* **Written atomically, streamed, and bounded.** The zip is built in a temporary file as the
  graphs arrive (memory is the largest single graph) and moved into place only when
  complete. ``EXPORT_MAX_BYTES`` caps the uncompressed TriG and is checked as it is written;
  an export that goes over leaves nothing behind.
* **An empty dataset is never exported**, and the audit graph does not count as content,
  for the same reason as snapshots: an empty export taken after a restart would push a good
  one out at the moment it is needed.
* **Retention** is the newest ``SNAPSHOT_KEEP`` and, if ``SNAPSHOT_MAX_AGE_DAYS`` is set,
  nothing older than that, except that the newest export is always kept. Pruning happens
  only after the new export is in place.
* **A download link is an HMAC over dataset, export id and expiry**, keyed by
  ``EXPORT_SIGNING_KEY``. Anything wrong with it (bad signature, expired, malformed names, or
  links turned off) gets the same 403, so a caller learns nothing about which part failed.
  The link authorises exactly one file for a short time and nothing else. Anyone holding a
  live link can download that export, so treat it like the export.
* **Confined.** The dataset name and export id are checked against strict patterns before
  they touch a path, and only files whose names are export ids are ever removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .conn import Conn
from .fuseki import FusekiClient
from .snapshot import (
    _ID,
    SnapshotError,
    _audit,
    _dir,
    _id_for,
    expired_ids,
    id_time,
    named_graphs,
)

EXPORT_DIR = "exports"
VERSION = 1
MIN_LINK_TTL = 60
MAX_LINK_TTL = 86_400
_FILE = re.compile(r"^(?P<id>\d{8}T\d{6}Z(?:-\d+)?)\.trig\.zip$")
_BAD_IRI_CHARS = re.compile(r"[\x00-\x20<>\"{}|^`\\]")
_LABEL_STOP = frozenset(" \t\r\n<>\"(){}")
_LINK_REFUSED = "this link is invalid or has expired"


def _now() -> float:
    """Seconds since the epoch. A function so tests can move the clock."""
    return time.time()


# --- blank-node scoping ------------------------------------------------------------------


def scope_bnodes(text: str, prefix: str) -> str:
    """Prefix every blank-node label in N-Triples ``text`` with ``prefix``.

    Walks the text token by token: IRIs (``<...>``) and string literals (``"..."``, with
    backslash escapes) are copied untouched, as are comments, so only a real ``_:label`` is
    rewritten. The ``<<`` and ``<<(`` that open an RDF-star / RDF 1.2 triple term are
    stepped over, so a blank node inside one is rewritten too.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "<":
            if text.startswith("<<", i):
                step = 3 if text.startswith("<<(", i) else 2
                out.append(text[i : i + step])
                i += step
                continue
            j = text.find(">", i + 1)
            j = n - 1 if j == -1 else j
            out.append(text[i : j + 1])
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
        elif c == "#":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(text[i:j])
            i = j
        elif c == "_" and text.startswith("_:", i) and (i == 0 or text[i - 1] in " \t\r\n("):
            j = i + 2
            while j < n and text[j] not in _LABEL_STOP:
                j += 1
            while j > i + 2 and text[j - 1] == ".":  # a label cannot end in '.'; that is the terminator
                j -= 1
            out.append(f"_:{prefix}{text[i + 2 : j]}")
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _statements(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))


def _render(label: str | None, text: str, prefix: str) -> tuple[str, int]:
    """One graph as a TriG block (``label`` is its IRI, ``None`` for the default graph)."""
    body = scope_bnodes(text, prefix)
    if body and not body.endswith("\n"):
        body += "\n"
    head = "{" if label is None else f"GRAPH <{label}> {{"
    return f"{head}\n{body}}}\n\n", _statements(text)


# --- the export --------------------------------------------------------------------------


def _exports_dir(settings: Settings, dataset: str) -> Path:
    return _dir(settings, dataset) / EXPORT_DIR


def export_ids(settings: Settings, dataset: str) -> list[str]:
    """The ids of this dataset's exports, oldest first."""
    directory = _exports_dir(settings, dataset)
    if not directory.is_dir():
        return []
    found = (_FILE.match(p.name) for p in directory.iterdir() if p.is_file())
    return sorted(m.group("id") for m in found if m)


def export_path(settings: Settings, dataset: str, export_id: str) -> Path:
    """The file for ``export_id``, refusing anything that is not an export id or does not exist."""
    if not _ID.match(export_id or ""):
        raise SnapshotError(400, "bad_export_id", f"{export_id!r} is not an export id")
    path = _exports_dir(settings, dataset) / f"{export_id}.trig.zip"
    if not path.is_file():
        have = export_ids(settings, dataset)
        listing = f"; it has {', '.join(reversed(have))}" if have else ""
        raise SnapshotError(404, "no_export", f"no export {export_id} of {dataset!r}{listing}")
    return path


def _unused_export_id(directory: Path, base: str) -> str:
    candidate, n = base, 1
    while (directory / f"{candidate}.trig.zip").exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


@dataclass(frozen=True)
class Exported:
    dataset: str
    id: str
    graphs: int
    statements: int
    uncompressed_bytes: int
    bytes: int
    sha256: str
    created_at: str
    kept: int


def _prune(settings: Settings, dataset: str) -> int:
    """Remove exports beyond ``SNAPSHOT_KEEP`` or older than ``SNAPSHOT_MAX_AGE_DAYS``,
    never the newest. Returns how many remain."""
    ids = export_ids(settings, dataset)
    keep = max(1, settings.snapshot_keep)
    drop = set(ids[:-keep]) | set(expired_ids(ids, settings.snapshot_max_age_days))
    directory = _exports_dir(settings, dataset)
    for old in drop:
        try:
            (directory / f"{old}.trig.zip").unlink()
        except OSError:
            pass
    return len(ids) - len(drop)


async def take_export(client: FusekiClient, conn: Conn, settings: Settings, *, by: str) -> Exported:
    """Write a zipped-TriG export of ``conn.dataset``. Raises :class:`SnapshotError`."""
    out_dir = _exports_dir(settings, conn.dataset)  # raises 503/400 when snapshots are off or the name is bad
    graphs = await named_graphs(client, conn)
    if not [g for g in graphs if g != _audit(conn)]:
        raise SnapshotError(
            409,
            "nothing_to_export",
            f"dataset {conn.dataset!r} is empty; not writing an empty export",
        )
    for iri in graphs:
        if _BAD_IRI_CHARS.search(iri):
            raise SnapshotError(
                500, "unexportable_graph", f"graph {iri!r} has characters a TriG graph name cannot carry"
            )

    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotError(500, "export_write_failed", f"cannot write the export: {exc}") from exc
    export_id = _unused_export_id(out_dir, _id_for(now))
    final = out_dir / f"{export_id}.trig.zip"
    tmp = out_dir / f".{export_id}.tmp-{os.getpid()}"
    trig_name = f"{conn.dataset}-{export_id}.trig"
    stamp = now.timetuple()[:6]

    sha = hashlib.sha256()
    total = 0
    statements = 0
    entries: list[dict[str, Any]] = []
    zf: zipfile.ZipFile | None = None
    handle = None
    try:
        zf = await asyncio.to_thread(zipfile.ZipFile, tmp, "w", zipfile.ZIP_DEFLATED)
        info = zipfile.ZipInfo(trig_name, date_time=stamp)
        info.compress_type = zipfile.ZIP_DEFLATED
        handle = await asyncio.to_thread(zf.open, info, "w", force_zip64=True)

        async def emit(text: str) -> None:
            nonlocal total
            data = text.encode("utf-8")
            total += len(data)
            if total > settings.export_max_bytes:
                raise SnapshotError(
                    413,
                    "export_too_large",
                    f"dataset {conn.dataset!r} is larger than EXPORT_MAX_BYTES="
                    f"{settings.export_max_bytes}; nothing was written",
                )
            sha.update(data)
            await asyncio.to_thread(handle.write, data)

        await emit(f"# holonbridge export of dataset {conn.dataset}\n# exported {created_at}\n\n")

        default_text = await client.get_graph_ntriples(conn, None)
        if default_text.strip():
            block, count = await asyncio.to_thread(_render, None, default_text, "d_")
            await emit(block)
            statements += count
            entries.append({"iri": None, "default": True, "statements": count, "bytes": len(block.encode())})

        for number, iri in enumerate(graphs, start=1):
            text = await client.get_graph_ntriples(conn, iri)
            if not text.strip():
                continue  # emptied between the listing and the read
            block, count = await asyncio.to_thread(_render, iri, text, f"g{number:04d}_")
            await emit(block)
            statements += count
            entries.append({"iri": iri, "statements": count, "bytes": len(block.encode())})

        await asyncio.to_thread(handle.close)
        handle = None
        manifest = {
            "version": VERSION,
            "id": export_id,
            "dataset": conn.dataset,
            "createdAt": created_at,
            "createdBy": by,
            "format": "application/trig",
            "file": trig_name,
            "sha256": sha.hexdigest(),
            "bytes": total,
            "statements": statements,
            "graphs": entries,
            "blankNodes": "labels are prefixed per graph (g0001_..., d_ for the default graph)",
        }
        minfo = zipfile.ZipInfo("manifest.json", date_time=stamp)
        minfo.compress_type = zipfile.ZIP_DEFLATED
        await asyncio.to_thread(zf.writestr, minfo, json.dumps(manifest, indent=2))
        await asyncio.to_thread(zf.close)
        zf = None
        await asyncio.to_thread(os.replace, tmp, final)
    except OSError as exc:
        raise SnapshotError(500, "export_write_failed", f"cannot write the export: {exc}") from exc
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()
        if zf is not None:
            with contextlib.suppress(Exception):
                zf.close()
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)

    kept = _prune(settings, conn.dataset)
    return Exported(
        conn.dataset,
        export_id,
        len(entries),
        statements,
        total,
        final.stat().st_size,
        sha.hexdigest(),
        created_at,
        kept,
    )


def list_exports(settings: Settings, dataset: str) -> list[dict[str, Any]]:
    """What exports of ``dataset`` exist, newest first. Metadata only, never contents."""
    directory = _exports_dir(settings, dataset)
    out: list[dict[str, Any]] = []
    for export_id in reversed(export_ids(settings, dataset)):
        path = directory / f"{export_id}.trig.zip"
        row: dict[str, Any] = {
            "id": export_id,
            "createdAt": id_time(export_id).isoformat(timespec="seconds"),
            "zipBytes": path.stat().st_size,
        }
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read("manifest.json"))
            row.update(
                createdBy=manifest.get("createdBy"),
                graphs=len(manifest.get("graphs", [])),
                statements=manifest.get("statements"),
                sha256=manifest.get("sha256"),
            )
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            row["unreadable"] = True
        out.append(row)
    return out


# --- expiring download links ---------------------------------------------------------------


def _signature(key: bytes, dataset: str, export_id: str, exp: int) -> str:
    return hmac.new(key, f"{dataset}\n{export_id}\n{exp}".encode(), hashlib.sha256).hexdigest()


def _signing_key(settings: Settings) -> bytes | None:
    key = settings.export_signing_key
    return key.encode("utf-8") if key and len(key) >= 32 else None


def mint_link(settings: Settings, dataset: str, export_id: str, *, ttl: int | None = None) -> dict[str, Any]:
    """An expiring link to an existing export. Raises :class:`SnapshotError`.

    The path is relative to the bridge; ``downloadUrl`` is added when
    ``EXPORT_PUBLIC_BASE_URL`` is set.
    """
    key = _signing_key(settings)
    if key is None:
        raise SnapshotError(
            503,
            "export_links_off",
            "download links are off: set EXPORT_SIGNING_KEY (at least 32 characters) on the bridge",
        )
    export_path(settings, dataset, export_id)  # it must exist, and the name is checked
    seconds = max(MIN_LINK_TTL, min(MAX_LINK_TTL, int(ttl or settings.export_link_ttl)))
    exp = int(_now()) + seconds
    path = (
        f"/admin/dataset/export/{dataset}/{export_id}/download"
        f"?exp={exp}&sig={_signature(key, dataset, export_id, exp)}"
    )
    link: dict[str, Any] = {
        "downloadPath": path,
        "expiresAt": datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds"),
        "expiresInSeconds": seconds,
    }
    if settings.export_public_base_url:
        link["downloadUrl"] = settings.export_public_base_url.rstrip("/") + path
    return link


def verify_link(settings: Settings, dataset: str, export_id: str, exp: int, sig: str) -> Path:
    """The file a valid, unexpired link names. Every failure is the same 403."""
    refused = SnapshotError(403, "bad_link", _LINK_REFUSED)
    key = _signing_key(settings)
    if key is None or not _ID.match(export_id or ""):
        raise refused
    try:
        _dir(settings, dataset)  # the dataset name must be a plain name inside SNAPSHOT_DIR
    except SnapshotError as exc:
        raise refused from exc
    if not hmac.compare_digest(_signature(key, dataset, export_id, exp), sig or ""):
        raise refused
    if _now() > exp:
        raise refused
    return export_path(settings, dataset, export_id)
