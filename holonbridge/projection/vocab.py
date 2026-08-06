"""Projection vocabulary.

The graph stays authoritative. A projection is materialised *off* it and
delivered to a target that does its own transformation — the bridge never
learns SQL, XSLT, or anything else about where the data is going. What it
knows is how to compute a scoped slice, how that slice changed since it was
last delivered, and how to hand the difference over.

Namespace choice is mine, not recovered from an existing spec: change
:data:`PROJ` if the HGA vocabulary settles on something else.
"""

from __future__ import annotations

from typing import Final

PROJ: Final = "https://w3id.org/holon/projection#"
XSD: Final = "http://www.w3.org/2001/XMLSchema#"

HOOK_CLASS_SUFFIX: Final = "ProjectionHook"

#: How a target wants change expressed. Per hook, never global — some
#: projections want history preserved and some want the current state only,
#: and that is a property of the target, not of the graph.
CHANGE_MODES: Final = (
    "append",       # additions only; retractions ignored (append-only log)
    "upsert",       # additions keyed by keyPredicate; retractions are deletes
    "soft-delete",  # retractions become tombstones rather than deletions
    "replace",      # whole slice every time, for targets that cannot do partial
)

#: How the envelope reaches the target.
DELIVERY_MODES: Final = (
    "webhook",  # the bridge POSTs the envelope
    "pull",     # the bridge queues it; the target collects and acknowledges
)

HOOK_STATUSES: Final = ("Active", "Suspended", "Deprecated")

DELIVERY_STATUSES: Final = ("pending", "delivered", "acknowledged", "failed")

#: Statuses that permit the watermark to advance. Anything else leaves it
#: where it is, so the next run re-derives the same difference.
SETTLED: Final = ("delivered", "acknowledged")


def scope_graph(dataset: str, hook_id: str) -> str:
    """Where the last successfully delivered slice is kept."""
    return f"urn:{dataset}:projection:{hook_id}"


def scratch_graph(dataset: str, token: str) -> str:
    return f"urn:{dataset}:projection-scratch:{token}"
