"""Projection persistence: the hook registry, the watermark, and the log.

The watermark is the interesting piece. Each hook keeps the last slice it
successfully delivered in ``urn:{dataset}:projection:{id}``, and the next run
diffs against it. That gives retraction detection for free — a triple in the
watermark and not in the fresh slice has been withdrawn — and it makes retry
idempotent: the watermark only advances on a settled delivery, so a failed
one simply re-derives the same difference next time.
"""

from __future__ import annotations

import logging
from typing import Any

from ..conn import Conn
from ..fuseki import FusekiClient, FusekiError
from ..rdfutil import collect, local_name, pick
from ..turtle import literal
from .model import Delivery, ProjectionHook, stamp
from .vocab import HOOK_CLASS_SUFFIX, PROJ, XSD, scope_graph

log = logging.getLogger("holonbridge.projection.store")

DT = f"<{XSD}dateTime>"
INT = f"<{XSD}integer>"

_HOOK_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "hookId", "identifier"),
    "target": ("target", "projectionTarget"),
    "label": ("label", "title"),
    "description": ("description", "comment"),
    "construct": ("construct", "scope", "sparql"),
    "named_query": ("namedQuery", "query"),
    "change_mode": ("changeMode", "mode"),
    "delivery": ("delivery", "deliveryMode"),
    "endpoint": ("endpoint", "url"),
    "key_predicate": ("keyPredicate", "key"),
    "media_type": ("mediaType", "format"),
    "status": ("hookStatus", "status"),
    "sequence": ("sequence",),
}


class ProjectionStore:
    def __init__(self, client: FusekiClient) -> None:
        self._client = client

    # --- registry -------------------------------------------------------------

    async def hooks(self, conn: Conn) -> list[ProjectionHook]:
        graph = conn.graph("projections")
        query = f"""SELECT ?h ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?h a ?type .
    FILTER( STRENDS(STR(?type), "{HOOK_CLASS_SUFFIX}") )
    ?h ?p ?o .
  }}
}}"""
        try:
            rows = (await self._client.select(conn, query))["results"]["bindings"]
        except (FusekiError, KeyError) as exc:
            log.warning("projection hook load failed: %s", exc)
            return []

        hooks: list[ProjectionHook] = []
        for iri, props in collect(rows, "h").items():
            raw_sequence = pick(props, _HOOK_FIELDS["sequence"])
            try:
                sequence = int(raw_sequence) if raw_sequence else 0
            except ValueError:
                sequence = 0
            hooks.append(
                ProjectionHook(
                    id=pick(props, _HOOK_FIELDS["id"]) or local_name(iri),
                    iri=iri,
                    target=pick(props, _HOOK_FIELDS["target"]) or "",
                    label=pick(props, _HOOK_FIELDS["label"]) or "",
                    description=pick(props, _HOOK_FIELDS["description"]) or "",
                    construct=pick(props, _HOOK_FIELDS["construct"]) or "",
                    named_query=pick(props, _HOOK_FIELDS["named_query"]) or "",
                    change_mode=(pick(props, _HOOK_FIELDS["change_mode"]) or "upsert").lower(),
                    delivery=(pick(props, _HOOK_FIELDS["delivery"]) or "pull").lower(),
                    endpoint=pick(props, _HOOK_FIELDS["endpoint"]) or "",
                    key_predicate=pick(props, _HOOK_FIELDS["key_predicate"]) or "",
                    media_type=pick(props, _HOOK_FIELDS["media_type"]) or "text/turtle",
                    status=local_name(pick(props, _HOOK_FIELDS["status"]) or "Active"),
                    sequence=sequence,
                )
            )
        hooks.sort(key=lambda h: h.id)
        return hooks

    async def save_hook(self, conn: Conn, hook: ProjectionHook) -> None:
        graph = conn.graph("projections")
        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{graph}> {{ <{hook.iri}> ?p ?o }} }}
WHERE {{ GRAPH <{graph}> {{ <{hook.iri}> ?p ?o }} }}""",
        )

        lines = [
            f"  GRAPH <{graph}> {{",
            f"    <{hook.iri}> a <{PROJ}{HOOK_CLASS_SUFFIX}> ;",
            f"      <{PROJ}id> {literal(hook.id)} ;",
            f"      <{PROJ}target> {literal(hook.target)} ;",
            f"      <{PROJ}changeMode> {literal(hook.change_mode)} ;",
            f"      <{PROJ}delivery> {literal(hook.delivery)} ;",
            f"      <{PROJ}mediaType> {literal(hook.media_type)} ;",
            f"      <{PROJ}hookStatus> <{PROJ}{hook.status}> ;",
            f"      <{PROJ}sequence> {literal(str(hook.sequence), datatype=INT)} ;",
        ]
        for term, value in (
            ("construct", hook.construct),
            ("namedQuery", hook.named_query),
            ("endpoint", hook.endpoint),
            ("keyPredicate", hook.key_predicate),
            ("label", hook.label),
            ("description", hook.description),
        ):
            if value:
                lines.append(f"      <{PROJ}{term}> {literal(value)} ;")

        lines[-1] = lines[-1].rstrip(" ;") + " ."
        lines.append("  }")
        await self._client.update(conn, f"INSERT DATA {{ {chr(10).join(lines)} }}")

    async def set_hook_status(
        self, conn: Conn, hook: ProjectionHook, status: str
    ) -> None:
        graph = conn.graph("projections")
        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{graph}> {{ <{hook.iri}> <{PROJ}hookStatus> ?s }} }}
INSERT {{ GRAPH <{graph}> {{ <{hook.iri}> <{PROJ}hookStatus> <{PROJ}{status}> }} }}
WHERE  {{ OPTIONAL {{ GRAPH <{graph}> {{ <{hook.iri}> <{PROJ}hookStatus> ?s }} }} }}""",
        )
        hook.status = status

    async def bump_sequence(self, conn: Conn, hook: ProjectionHook) -> int:
        graph = conn.graph("projections")
        nxt = hook.sequence + 1
        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{graph}> {{ <{hook.iri}> <{PROJ}sequence> ?s }} }}
INSERT {{ GRAPH <{graph}> {{ <{hook.iri}> <{PROJ}sequence> {literal(str(nxt), datatype=INT)} }} }}
WHERE  {{ OPTIONAL {{ GRAPH <{graph}> {{ <{hook.iri}> <{PROJ}sequence> ?s }} }} }}""",
        )
        hook.sequence = nxt
        return nxt

    async def delete_hook(self, conn: Conn, hook: ProjectionHook) -> None:
        graph = conn.graph("projections")
        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{graph}> {{ <{hook.iri}> ?p ?o }} }}
WHERE {{ GRAPH <{graph}> {{ <{hook.iri}> ?p ?o }} }}""",
        )
        await self._client.drop_graph(conn, scope_graph(conn.dataset, hook.id))

    # --- watermark ------------------------------------------------------------

    async def delta_turtle(
        self, conn: Conn, *, present: str, absent: str
    ) -> str:
        """CONSTRUCT the triples in one graph and not the other.

        Server-side, so the payload never passes through a local parser and
        Turtle 1.2 output survives.
        """
        return await self._client.construct(
            conn,
            f"""CONSTRUCT {{ ?s ?p ?o }}
WHERE {{
  GRAPH <{present}> {{ ?s ?p ?o }}
  FILTER NOT EXISTS {{ GRAPH <{absent}> {{ ?s ?p ?o }} }}
}}""",
        )

    async def delta_count(self, conn: Conn, *, present: str, absent: str) -> int:
        results = await self._client.select(
            conn,
            f"""SELECT (COUNT(*) AS ?n)
WHERE {{
  GRAPH <{present}> {{ ?s ?p ?o }}
  FILTER NOT EXISTS {{ GRAPH <{absent}> {{ ?s ?p ?o }} }}
}}""",
        )
        rows = results.get("results", {}).get("bindings", [])
        return int(rows[0]["n"]["value"]) if rows else 0

    async def graph_turtle(self, conn: Conn, graph: str) -> str:
        return await self._client.construct(
            conn, f"CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}"
        )

    async def graph_count(self, conn: Conn, graph: str) -> int:
        results = await self._client.select(
            conn, f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}"
        )
        rows = results.get("results", {}).get("bindings", [])
        return int(rows[0]["n"]["value"]) if rows else 0

    async def advance_watermark(self, conn: Conn, *, hook_id: str, source: str) -> None:
        """Adopt the freshly computed slice as the new baseline.

        Only ever called after a settled delivery. That is the whole retry
        story: an unsettled delivery leaves the watermark alone, so the next
        run computes the same difference and tries again.
        """
        await self._client.update(
            conn,
            f"COPY SILENT <{source}> TO <{scope_graph(conn.dataset, hook_id)}>",
        )

    async def reset_watermark(self, conn: Conn, hook_id: str) -> None:
        """Forget what has been delivered, so the next run sends everything."""
        await self._client.drop_graph(conn, scope_graph(conn.dataset, hook_id))

    # --- delivery log ---------------------------------------------------------

    async def record_delivery(self, conn: Conn, delivery: Delivery) -> None:
        graph = conn.graph("projection-log")
        iri = f"urn:{conn.dataset}:delivery:{delivery.id}"
        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{graph}> {{ <{iri}> ?p ?o }} }}
WHERE {{ GRAPH <{graph}> {{ <{iri}> ?p ?o }} }}""",
        )
        lines = [
            f"  GRAPH <{graph}> {{",
            f"    <{iri}> a <{PROJ}Delivery> ;",
            f"      <{PROJ}deliveryId> {literal(delivery.id)} ;",
            f"      <{PROJ}hook> {literal(delivery.hook_id)} ;",
            f"      <{PROJ}deliveryStatus> {literal(delivery.status)} ;",
            f"      <{PROJ}sequence> {literal(str(delivery.sequence), datatype=INT)} ;",
            f"      <{PROJ}createdAt> {literal(delivery.created_at, datatype=DT)} ;",
            f"      <{PROJ}attempts> {literal(str(delivery.attempts), datatype=INT)} ;",
            f"      <{PROJ}additions> {literal(str(delivery.addition_count), datatype=INT)} ;",
            f"      <{PROJ}retractions> {literal(str(delivery.retraction_count), datatype=INT)} ;",
        ]
        if delivery.settled_at:
            lines.append(
                f"      <{PROJ}settledAt> {literal(delivery.settled_at, datatype=DT)} ;"
            )
        lines.append(f"      <{PROJ}error> {literal(delivery.error[:2000])} .")
        lines.append("  }")
        await self._client.update(conn, f"INSERT DATA {{ {chr(10).join(lines)} }}")

    async def delivery(self, conn: Conn, delivery_id: str) -> Delivery | None:
        graph = conn.graph("projection-log")
        iri = f"urn:{conn.dataset}:delivery:{delivery_id}"
        rows = (
            await self._client.select(
                conn,
                f"SELECT ?p ?o WHERE {{ GRAPH <{graph}> {{ <{iri}> ?p ?o }} }}",
            )
        )["results"]["bindings"]
        if not rows:
            return None
        props = {local_name(r["p"]["value"]): r["o"]["value"] for r in rows}
        return Delivery(
            id=props.get("deliveryId", delivery_id),
            hook_id=props.get("hook", ""),
            status=props.get("deliveryStatus", "pending"),
            sequence=int(props.get("sequence", "0") or 0),
            created_at=props.get("createdAt", ""),
            settled_at=props.get("settledAt", ""),
            attempts=int(props.get("attempts", "0") or 0),
            addition_count=int(props.get("additions", "0") or 0),
            retraction_count=int(props.get("retractions", "0") or 0),
            error=props.get("error", ""),
        )

    async def deliveries(
        self,
        conn: Conn,
        *,
        hook_id: str | None = None,
        delivery_status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        graph = conn.graph("projection-log")
        filters = ""
        if hook_id:
            filters += f'    FILTER( ?hook = {literal(hook_id)} )\n'
        if delivery_status:
            filters += f'    FILTER( ?status = {literal(delivery_status)} )\n'

        rows = (
            await self._client.select(
                conn,
                f"""SELECT ?id ?hook ?status ?createdAt ?additions ?retractions ?error
WHERE {{
  GRAPH <{graph}> {{
    ?d a <{PROJ}Delivery> ;
       <{PROJ}deliveryId> ?id ;
       <{PROJ}hook> ?hook ;
       <{PROJ}deliveryStatus> ?status ;
       <{PROJ}createdAt> ?createdAt ;
       <{PROJ}additions> ?additions ;
       <{PROJ}retractions> ?retractions .
    OPTIONAL {{ ?d <{PROJ}error> ?error }}
{filters}  }}
}}
ORDER BY DESC(?createdAt)
LIMIT {int(limit)}""",
            )
        )["results"]["bindings"]

        return [
            {
                "deliveryId": r["id"]["value"],
                "hook": r["hook"]["value"],
                "status": r["status"]["value"],
                "createdAt": r["createdAt"]["value"],
                "counts": {
                    "additions": int(r["additions"]["value"]),
                    "retractions": int(r["retractions"]["value"]),
                },
                "error": r.get("error", {}).get("value", ""),
            }
            for r in rows
        ]

    async def pending_before(
        self, conn: Conn, cutoff: str, *, limit: int = 500
    ) -> list[Delivery]:
        """Pending deliveries created before a cutoff.

        The cutoff carries a timezone, like every other timestamp comparison
        here — an unqualified one compares indeterminately against the stored
        stamps and a sweep would silently find nothing.
        """
        graph = conn.graph("projection-log")
        rows = (
            await self._client.select(
                conn,
                f"""SELECT ?id ?hook ?createdAt ?additions ?retractions
WHERE {{
  GRAPH <{graph}> {{
    ?d a <{PROJ}Delivery> ;
       <{PROJ}deliveryId> ?id ;
       <{PROJ}hook> ?hook ;
       <{PROJ}deliveryStatus> "pending" ;
       <{PROJ}createdAt> ?createdAt ;
       <{PROJ}additions> ?additions ;
       <{PROJ}retractions> ?retractions .
    FILTER( ?createdAt < "{cutoff}"^^<{XSD}dateTime> )
  }}
}}
ORDER BY ?createdAt
LIMIT {int(limit)}""",
            )
        )["results"]["bindings"]

        return [
            Delivery(
                id=r["id"]["value"],
                hook_id=r["hook"]["value"],
                status="pending",
                created_at=r["createdAt"]["value"],
                addition_count=int(r["additions"]["value"]),
                retraction_count=int(r["retractions"]["value"]),
            )
            for r in rows
        ]

    async def scratch_graphs(self, conn: Conn) -> list[str]:
        """Every projection scratch graph currently holding triples.

        An empty scratch graph does not appear here — a store that does not
        track empty named graphs has nothing to report. That is fine: an empty
        scratch graph costs nothing and disappears on the next drop.
        """
        prefix = f"urn:{conn.dataset}:projection-scratch:"
        rows = (
            await self._client.select(
                conn,
                f"""SELECT DISTINCT ?g
WHERE {{
  GRAPH ?g {{ ?s ?p ?o }}
  FILTER( STRSTARTS(STR(?g), "{prefix}") )
}}""",
            )
        )["results"]["bindings"]
        return [r["g"]["value"] for r in rows]

    async def settle(
        self, conn: Conn, delivery: Delivery, status: str, *, error: str = ""
    ) -> None:
        delivery.status = status
        delivery.settled_at = stamp()
        delivery.error = error
        await self.record_delivery(conn, delivery)
