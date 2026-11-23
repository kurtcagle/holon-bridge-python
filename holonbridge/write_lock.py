"""Per-target write serialisation -- the Python port of the Node bridge's
``withDatasetLock`` (see ``lib/sequence.js`` on ``kurtcagle/holon-bridge``).

``holonbridge/ingest.py``'s ``write_turtle_to_graph`` validates a payload
against whatever the target graph currently holds (SHACL, closed-set/
maxCount shapes) and only writes afterwards, with real ``await`` points
between the read and the write. Two concurrent calls against the *same*
graph each validate against a baseline that predates the other's write --
a pair that is individually valid but jointly invalid (a ``maxCount``, a
closed-set, a "one X per graph" shape) can both pass validation and both
land, and under ``mode="replace"`` the second GSP PUT simply clobbers the
first outright. Reported by Ben Wortley, 2026-09-15, as the general case
of the same class of race PR #14 already fixed for named-rule Replace
executions (that fix restored single-UPDATE transactional atomicity for
one COPY+write pair; this one serialises the *whole* validate-then-write
sequence, including the separate SHACL round trip in between, which a
single Jena UPDATE cannot do because it is not one Jena transaction to
begin with).

The fix mirrors the one the Node bridge already applies to sequence
minting: a single in-process mutex per target, so no two calls against the
same target can have their validate-then-write windows overlap. This is a
process-local guarantee, not a cross-process one -- see ``sequence.py``'s
module docstring for the identical caveat there. Concurrent requests
reaching *different* HolonBridge processes against the same Fuseki dataset
are not covered by this lock; only the one-process-per-dataset deployment
shape already assumed elsewhere in this codebase (see that same docstring)
makes that an acceptable scope for now.

Keyed by ``(conn.base_url, conn.dataset)`` rather than ``conn.dataset``
alone: two banks can legitimately configure datasets with the same short
name against two different Fuseki instances (nothing stops it, and nothing
should have to), and locking by name alone would serialise those against
each other for no reason. ``base_url`` already disambiguates the physical
store, matching how ``FusekiClient`` itself addresses it.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from .conn import Conn

_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


def locked_datasets(base_url: str) -> list[str]:
    """Datasets on ``base_url`` that have a write in flight right now."""
    return sorted(ds for (url, ds), lock in _locks.items() if url == base_url and lock.locked())


def for_conn(conn: Conn) -> asyncio.Lock:
    """The mutex guarding writes against this connection's target dataset.

    One lock per ``(base_url, dataset)`` pair, created lazily on first use
    and kept for the life of the process -- nothing ever removes an entry,
    so a plain ``dict`` with a manual ``setdefault`` would work identically;
    ``defaultdict`` just keeps the get-or-create at the call site to one
    line, the same trade every other per-key store in this codebase makes
    (see ``acting_as.py``).
    """
    return _locks[(conn.base_url, conn.dataset)]
