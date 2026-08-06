"""Projection execution.

One run of a hook:

1. Materialise the hook's scope into a scratch graph named for the delivery.
2. Diff it against the watermark — the last slice this hook successfully
   delivered. Additions are in the fresh slice and not the watermark;
   retractions are the reverse.
3. Hand the difference to the target.
4. **Only on a settled delivery**, advance the watermark to the scratch graph
   and drop it.

Step 4 is the whole retry story. A failed or unacknowledged delivery leaves
the watermark where it was, so the next run derives the same difference and
tries again. Delivery is at-least-once; a target that cannot tolerate a repeat
should key its writes, which is what ``keyPredicate`` is for.

The scratch graph survives until the delivery settles rather than being
recomputed at acknowledgement time. That way the watermark advances to exactly
what was handed over, even if the graph moved on in between.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from datetime import datetime, timedelta, timezone

from ..conn import Conn
from ..fuseki import FusekiClient, FusekiError
from ..named_queries import apply_query_params, load_named_queries
from ..sparql_kind import form
from .model import Delivery, Envelope, ProjectionHook
from .store import ProjectionStore
from .vocab import scope_graph, scratch_graph

log = logging.getLogger("holonbridge.projection")


class ProjectionError(RuntimeError):
    """A hook cannot be run as configured."""


class Sender(Protocol):
    """Delivers an envelope to a target."""

    async def send(self, hook: ProjectionHook, envelope: Envelope) -> str:
        """Return a short description of the outcome, or raise on failure."""
        ...


class HttpSender:
    """POSTs the envelope as JSON. The only delivery the bridge implements.

    Anything that is not an HTTP endpoint uses ``pull``: the bridge holds the
    envelope and the target collects it. That is what keeps SQL, XSLT, and
    everything else out of the bridge.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    async def send(self, hook: ProjectionHook, envelope: Envelope) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                hook.endpoint,
                json=envelope.as_dict(),
                headers={"Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            raise ProjectionError(
                f"{hook.endpoint} returned {response.status_code}: "
                f"{response.text.strip()[:300]}"
            )
        return f"{response.status_code} from {hook.endpoint}"


class ProjectionRunner:
    def __init__(self, client: FusekiClient, *, sender: Sender | None = None) -> None:
        self._client = client
        self._store = ProjectionStore(client)
        self._sender: Sender = sender or HttpSender()

    # --- scope ----------------------------------------------------------------

    async def _slice_query(
        self, conn: Conn, hook: ProjectionHook, params: dict[str, Any] | None
    ) -> str:
        if hook.construct:
            query = hook.construct
        else:
            registry = await load_named_queries(self._client, conn)
            named = registry.by_id(hook.named_query)
            if named is None:
                raise ProjectionError(
                    f"hook {hook.id!r} names unknown query {hook.named_query!r}"
                )
            query = apply_query_params(named, params or {}).sparql

        if form(query) not in {"CONSTRUCT", "DESCRIBE"}:
            raise ProjectionError(
                f"hook {hook.id!r} scope is a {form(query)}; a projection scope must "
                "CONSTRUCT the triples the target should see"
            )
        return query

    # --- running --------------------------------------------------------------

    async def run(
        self,
        conn: Conn,
        hook: ProjectionHook,
        *,
        params: dict[str, Any] | None = None,
        force: bool = False,
    ) -> tuple[Delivery, Envelope]:
        """Compute the difference and deliver it.

        ``force`` sends an envelope even when nothing changed — useful for
        proving a target is reachable without waiting for the data to move.
        """
        problems = hook.problems()
        fatal = [p for p in problems if not p.startswith("upsert with no keyPredicate")]
        if fatal:
            raise ProjectionError(f"hook {hook.id!r} is misconfigured: {'; '.join(fatal)}")
        if not hook.active:
            raise ProjectionError(f"hook {hook.id!r} is {hook.status.lower()}")

        query = await self._slice_query(conn, hook, params)

        delivery_id = Envelope.new_id()
        scratch = scratch_graph(conn.dataset, delivery_id)
        watermark = scope_graph(conn.dataset, hook.id)

        turtle = await self._client.construct(conn, query)
        await self._client.update(conn, f"CREATE SILENT GRAPH <{scratch}>")
        if turtle.strip():
            await self._client.post_graph(conn, scratch, turtle)

        try:
            if hook.sends_full_slice:
                additions = await self._store.graph_turtle(conn, scratch)
                addition_count = await self._store.graph_count(conn, scratch)
                retractions, retraction_count = "", 0
            else:
                additions = await self._store.delta_turtle(
                    conn, present=scratch, absent=watermark
                )
                addition_count = await self._store.delta_count(
                    conn, present=scratch, absent=watermark
                )
                if hook.sends_retractions:
                    retractions = await self._store.delta_turtle(
                        conn, present=watermark, absent=scratch
                    )
                    retraction_count = await self._store.delta_count(
                        conn, present=watermark, absent=scratch
                    )
                else:
                    retractions, retraction_count = "", 0
        except FusekiError:
            await self._client.drop_graph(conn, scratch)
            raise

        sequence = await self._store.bump_sequence(conn, hook)
        envelope = Envelope(
            delivery_id=delivery_id,
            hook_id=hook.id,
            target=hook.target,
            dataset=conn.dataset,
            change_mode=hook.change_mode,
            sequence=sequence,
            key_predicate=hook.key_predicate,
            media_type=hook.media_type,
            additions=additions,
            retractions=retractions,
            addition_count=addition_count,
            retraction_count=retraction_count,
            full_slice=hook.sends_full_slice,
        )
        delivery = Delivery(
            id=delivery_id,
            hook_id=hook.id,
            sequence=sequence,
            addition_count=addition_count,
            retraction_count=retraction_count,
        )

        if envelope.empty and not force and not hook.sends_full_slice:
            # Nothing moved. Settle immediately and advance — the watermark is
            # already correct, but adopting the scratch keeps the two identical
            # rather than merely equivalent.
            delivery.status = "delivered"
            delivery.attempts = 0
            await self._store.settle(conn, delivery, "delivered")
            await self._store.advance_watermark(conn, hook_id=hook.id, source=scratch)
            await self._client.drop_graph(conn, scratch)
            return delivery, envelope

        delivery.attempts = 1

        if hook.delivery == "pull":
            # Held for collection. The scratch graph stays until the target
            # acknowledges, so the watermark can advance to exactly what was
            # collected rather than to whatever the graph says later.
            await self._store.record_delivery(conn, delivery)
            return delivery, envelope

        try:
            detail = await self._sender.send(hook, envelope)
        except Exception as exc:  # noqa: BLE001 - recorded on the delivery
            await self._store.settle(conn, delivery, "failed", error=str(exc))
            await self._client.drop_graph(conn, scratch)
            log.warning("projection %s delivery failed: %s", hook.id, exc)
            return delivery, envelope

        await self._store.settle(conn, delivery, "delivered", error="")
        await self._store.advance_watermark(conn, hook_id=hook.id, source=scratch)
        await self._client.drop_graph(conn, scratch)
        log.info("projection %s delivered: %s", hook.id, detail)
        return delivery, envelope

    # --- pull-mode settlement -------------------------------------------------

    async def acknowledge(self, conn: Conn, delivery: Delivery) -> Delivery:
        """Target confirms it applied the envelope. Advance the watermark."""
        if delivery.status != "pending":
            raise ProjectionError(
                f"delivery {delivery.id} is already {delivery.status}"
            )
        scratch = scratch_graph(conn.dataset, delivery.id)
        await self._store.advance_watermark(
            conn, hook_id=delivery.hook_id, source=scratch
        )
        await self._client.drop_graph(conn, scratch)
        await self._store.settle(conn, delivery, "acknowledged")
        return delivery

    async def reject(self, conn: Conn, delivery: Delivery, reason: str) -> Delivery:
        """Target could not apply the envelope. Leave the watermark alone."""
        if delivery.status != "pending":
            raise ProjectionError(
                f"delivery {delivery.id} is already {delivery.status}"
            )
        await self._client.drop_graph(conn, scratch_graph(conn.dataset, delivery.id))
        await self._store.settle(conn, delivery, "failed", error=reason)
        return delivery

    async def sweep(
        self, conn: Conn, *, max_age_seconds: float = 86_400.0
    ) -> dict[str, Any]:
        """Reclaim deliveries a target never came back for.

        A pull delivery holds its scratch graph until acknowledgement, which is
        what lets the watermark advance to exactly what was collected. The cost
        is that a target which goes away leaves that graph behind. Sweeping
        settles the delivery as failed and drops the graph; the watermark is
        untouched, so the same difference is simply offered again next run —
        nothing is lost by sweeping too eagerly.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(max_age_seconds, 0))
        ).isoformat(timespec="seconds")

        abandoned = await self._store.pending_before(conn, cutoff)
        for delivery in abandoned:
            await self._client.drop_graph(
                conn, scratch_graph(conn.dataset, delivery.id)
            )
            await self._store.settle(
                conn,
                delivery,
                "failed",
                error=f"abandoned: never acknowledged before {cutoff}",
            )

        # Scratch graphs with no pending delivery behind them. These come from
        # a crash between creating the graph and recording the delivery, so
        # nothing will ever claim them.
        live = {
            scratch_graph(conn.dataset, d.id)
            for d in await self._store.pending_before(
                conn, datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
        }
        orphans = [g for g in await self._store.scratch_graphs(conn) if g not in live]
        for graph in orphans:
            await self._client.drop_graph(conn, graph)

        return {
            "cutoff": cutoff,
            "abandoned": [d.id for d in abandoned],
            "abandonedCount": len(abandoned),
            "orphanedGraphs": orphans,
            "orphanedCount": len(orphans),
        }

    async def envelope_for(
        self, conn: Conn, delivery: Delivery, *, include_payload: bool = True
    ) -> Envelope:
        """Rebuild a pending envelope from its scratch graph."""
        scratch = scratch_graph(conn.dataset, delivery.id)
        watermark = scope_graph(conn.dataset, delivery.hook_id)

        additions = retractions = ""
        if include_payload:
            additions = await self._store.delta_turtle(
                conn, present=scratch, absent=watermark
            )
            retractions = await self._store.delta_turtle(
                conn, present=watermark, absent=scratch
            )

        return Envelope(
            delivery_id=delivery.id,
            hook_id=delivery.hook_id,
            target="",
            dataset=conn.dataset,
            change_mode="",
            sequence=delivery.sequence,
            additions=additions,
            retractions=retractions,
            addition_count=delivery.addition_count,
            retraction_count=delivery.retraction_count,
        )
