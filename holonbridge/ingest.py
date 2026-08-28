"""Shared core for anything that validates-then-writes a Turtle payload
into a named graph.

Three callers share this path: ``POST /graph/push`` (raw Turtle, explicit
graph_iri, caller picks merge or replace), ``POST /holon`` (create_holon —
a DataBook envelope around the same operation), and ``POST /message/create``
(create_message — a DataBook envelope targeting the dataset's events graph
by default, merge-only). All three go through exactly this function for the
ACL check, the optional SHACL gate, and the GSP write itself.

CHANGED 2026-08-28: factored out of ``routes/graphs.py``'s ``push_turtle``
so create_holon and create_message are wrappers around the same gated path,
not parallel reimplementations of it. This matters concretely: ``/graph/push``
had no ACL check at all until 2026-08-17 (see that route's own module
docstring), and a second, independently-written write path is exactly how
that class of gap reappears. There is now one function that decides whether
a write is allowed and validated; every route that writes Turtle into a
named graph calls it.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from . import shacl as shacl_mod
from .acl import Animus, check_replace, check_write
from .conn import Conn
from .fuseki import FusekiClient, FusekiError


async def write_turtle_to_graph(
    *,
    turtle: str,
    graph_iri: str,
    mode: str,
    shapes_graph: str | None,
    reduction_rule_id: str | None,
    conn: Conn,
    client: FusekiClient,
    shacl_required: bool,
    shacl_delta: bool,
    animus: Animus,
) -> dict[str, Any]:
    """ACL-check, optionally SHACL-validate, then write ``turtle`` into
    ``graph_iri``. Raises :class:`~fastapi.HTTPException` on any refusal —
    401 for an unresolved identity, 403 for a denied ACL decision, 400/502
    for a malformed request or backend failure, 422 for a blocking SHACL
    violation (``sh:Warning``/``sh:Info`` are reported but never block —
    see ``holonbridge.shacl.is_blocking``).

    ``mode="replace"`` requires the stricter, independent ``check_replace``
    grant; ``mode="merge"`` requires ``check_write``. See
    ``holonbridge.acl`` for why the two are deliberately not the same
    grant. Every caller of this function passes ``mode`` explicitly —
    create_message always passes ``"merge"`` (events are append-only; see
    ``routes/events.py``), create_holon and push_turtle both expose the
    caller's choice.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    async def _query_fn(q: str) -> dict:
        return await client.select(conn, q)

    if mode == "replace":
        decision = await check_replace(
            _query_fn, conn.holons_graph, person=animus.person, target=graph_iri
        )
    else:
        decision = await check_write(
            _query_fn, conn.holons_graph, person=animus.person, target=graph_iri
        )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"{decision.reason} (graph: {graph_iri})",
        )

    resolved_shapes_graph = shapes_graph
    if resolved_shapes_graph is None and shacl_required:
        resolved_shapes_graph = conn.shapes_graph

    report_payload = None
    if resolved_shapes_graph:
        try:
            if shacl_delta:
                report = await shacl_mod.validate_delta(
                    client,
                    conn,
                    turtle=turtle,
                    shapes_graph=resolved_shapes_graph,
                    target_graph=graph_iri,
                    write_mode=mode,
                    reduction_rule_id=reduction_rule_id,
                )
            else:
                report = await shacl_mod.validate_full(
                    client,
                    conn,
                    turtle=turtle,
                    shapes_graph=resolved_shapes_graph,
                    target_graph=graph_iri,
                    write_mode=mode,
                    reduction_rule_id=reduction_rule_id,
                )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except FusekiError as exc:
            raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

        report_payload = report.as_dict()
        if not report.conforms:
            raise HTTPException(
                422,
                detail={"error": "shacl_violation", **report_payload},
            )

    try:
        if mode == "replace":
            await client.put_graph(conn, graph_iri, turtle)
        else:
            await client.post_graph(conn, graph_iri, turtle)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "graph": graph_iri,
        "dataset": conn.dataset,
        "mode": mode,
        "validated": bool(resolved_shapes_graph),
        "validation": report_payload,
    }
