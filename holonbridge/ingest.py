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

CHANGED 2026-09-15: closed a validate-then-write TOCTOU flagged by Ben
Wortley. The SHACL gate (``shacl_mod.validate_full``/``validate_delta``)
takes its own snapshot of the target graph, has real ``await`` points
while it does, and only the GSP write afterwards actually commits — with
nothing in between stopping a second concurrent call from validating
against the same stale snapshot and landing a jointly-invalid write (or,
under ``mode="replace"``, simply clobbering the first write's GSP PUT
outright). The SHACL round trip and the final write are now serialised
per target dataset via ``write_lock.for_conn`` — see that module's
docstring for why a lock, not a single atomic Jena operation, is the right
fix here: unlike PR #14's Replace-mode fix, there is no single SPARQL
UPDATE that could make "validate against Fuseki, then GSP PUT/POST" one
Jena transaction. The ACL check itself stays outside the lock: it is a
plain read with no write-then-confirm shape, so serialising it buys
nothing and would only add latency ahead of the lock for every caller,
including ones that end up 403'd before ever touching SHACL.

CHANGED 2026-09-30: the returned dict now carries ``authorisedBy`` (the
grant target for an explicit ``holon:grantsWrite``/``grantsReplace``, the
``holon:Delegation`` IRI when the write was allowed by a delegation, and
``None`` for the admin bypass) and ``authorisedReason``. A delegated write
is otherwise indistinguishable from the delegator's own in the response.
This is response-level attribution only; nothing extra is written to the
graph, so the stored data still does not record who wrote it.

CHANGED 2026-09-30 (audit): the graph, mode, payload digest (never the
payload) and what authorised the write are now also handed to
``holonbridge.audit.annotate``, which the audit middleware folds into that
request's audit entry -- including for a refused write, so a denial says
which graph and why. ``annotate`` is a no-op outside a request.

CHANGED 2026-09-30 (context pack): when the caller did not name a shapes
graph of their own, the write is also validated against the dataset's
context shapes graph (``urn:{dataset}:shacl-context``) with the dataset's
ontology and taxonomy merged into the validation target (CHANGED 2026-10-21:
the taxonomy graph joined the ontology as read-only context) -- see
``shacl.validate_context_pack``. The two results are combined and either
can refuse the write with a 422. A dataset with no context shapes is
validated exactly as before. A caller-supplied ``shapes_graph`` means
"validate against exactly this", so the context pack is not added to it.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from . import audit
from . import shacl as shacl_mod
from . import write_lock
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

    The SHACL-validate-then-write sequence below runs under a per-
    (bank, dataset) lock (see ``write_lock``) so two concurrent calls
    against the same target can never have their validation windows
    overlap. The ACL decision above is deliberately made before the lock
    is taken — see the module docstring.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    digest, size = audit.payload_digest(turtle)
    audit.annotate(graphs=[graph_iri], write_mode=mode, payload_hash=digest, payload_bytes=size)

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
    audit.annotate(authorised_by=decision.matched_grant, authorised_reason=decision.reason)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=f"{decision.reason} (graph: {graph_iri})",
        )

    resolved_shapes_graph = shapes_graph
    if resolved_shapes_graph is None and shacl_required:
        resolved_shapes_graph = conn.shapes_graph

    async with write_lock.for_conn(conn):
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

                # The dataset's context pack, if it has one, runs alongside the
                # ordinary shapes -- but only when the caller did not pick a
                # shapes graph of their own: an explicit shapes_graph means
                # "validate against exactly this".
                context_report = None
                if shapes_graph is None:
                    context_report = await shacl_mod.validate_context_pack(
                        client,
                        conn,
                        turtle=turtle,
                        target_graph=graph_iri,
                        delta=shacl_delta,
                        write_mode=mode,
                        reduction_rule_id=reduction_rule_id,
                    )
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            except FusekiError as exc:
                raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

            report_payload = shacl_mod.combine_reports(
                report,
                context_report,
                context_shapes_graph=conn.context_shapes_graph,
                context_graphs=conn.validation_context_graphs,
            )
            if not report_payload["conforms"]:
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
        "authorisedBy": decision.matched_grant,
        "authorisedReason": decision.reason,
    }
