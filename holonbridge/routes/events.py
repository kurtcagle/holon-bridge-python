"""AssertionEvent submission — ``create_message`` (``POST /message/create``).

**Naming note, read before touching this file.** This is unrelated to
``holonbridge.messages`` / ``hb:Message`` (the async-run status record —
Received/Running/Completed/Failed — kept in the same-named ``messages``
graph role, per that module's own docstring). That module tracks the
progress of a long-running pipeline invocation. This module writes
domain-level ``hev:AssertionEvent`` content into the dataset's ``events``
graph role — a different graph, a different vocabulary, a different
concept, that happens to share a word in its route name because that is
the name this endpoint was specified under. Nothing in this file reads or
writes ``hb:Message``/``MessageStore``, and nothing in ``messages.py``
needs to change for this to work. If a future caller needs to poll an
event submission's outcome the way a pipeline run can be polled, that is a
new, deliberate design decision, not something to bolt onto either
existing concept by reusing its name.

**Scope, as agreed 2026-08-28.** ``holon:AssertionEvent`` submission only.
This is the passive half of the CommandEvent pipeline sketched in the
``sce`` architecture skill (validate -> authorise -> execute -> assert ->
log -> update -> project): a human or persona explicitly records that
something happened. Triggering system *action* from an event — the
CommandEvent execution half — is a separate, materially larger piece of
design work (authorisation posture for a request that causes a mutation,
not just records one) and is deliberately not built here. It is also not
the same thing as ``holonbridge/triggers.py``'s condition-driven
CommandEvents, which already exist and fire independently of anything
this route does; create_message never invokes a trigger, a rule, or the
scheduler. It writes one thing: the event record itself.

**Why merge-only, no mode parameter.** An event ledger is append-only by
its nature — the event that already happened does not get overwritten by
the next one. ``write_turtle_to_graph`` is always called with
``mode="merge"`` here; there is no request field to change that,
deliberately, unlike create_holon and push_turtle where replace is a
legitimate caller choice.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..databook import DataBook
from ..deps import AnimusDep, ClientDep, ConnDep, SettingsDep
from ..ingest import write_turtle_to_graph
from ..turtle import TurtleSyntaxError, from_json_ld, parse

router = APIRouter(tags=["events"])


class CreateMessageRequest(BaseModel):
    databook: str = Field(
        ...,
        min_length=1,
        description=(
            "Full DataBook markdown text carrying one or more "
            "hev:AssertionEvent instances, as turtle, turtle12, or json-ld."
        ),
    )
    block_id: str | None = Field(
        default=None,
        description=(
            "Select a specific databook:id block. Defaults to the "
            "DataBook's first turtle/turtle12/json-ld block."
        ),
    )
    graph_iri: str | None = Field(
        default=None,
        description=(
            "Overrides both the DataBook's own graph.named_graph "
            "frontmatter and the default events graph. Ordinarily left "
            "unset: events land in urn:{dataset}:events unless the caller "
            "has a specific reason to fork them elsewhere (a scenario "
            "graph, for instance)."
        ),
    )
    shapes_graph: str | None = Field(
        default=None,
        description=(
            "SHACL shapes to validate against. If the dataset has an "
            "EventShape or similar registered, pass its graph IRI here to "
            "enforce that submitted content actually types as "
            "hev:AssertionEvent and carries whatever envelope properties "
            "the schema requires -- this route does not check the type "
            "itself, only that the payload is well-formed RDF."
        ),
    )
    reduction_rule_id: str | None = None


@router.post("/message/create")
async def create_message(
    body: CreateMessageRequest,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    animus: AnimusDep,
) -> dict:
    """Submit a DataBook of AssertionEvent content to the events graph.

    Same DataBook-envelope pattern as create_holon (parse, pull the
    primary RDF block, validate, write via
    ``holonbridge.ingest.write_turtle_to_graph``), with two differences:
    the write is always a merge (see module docstring), and the target
    graph defaults to ``conn.graph("events")`` rather than requiring one —
    an event with no graph_iri and no frontmatter override is not an
    error, it just lands in the dataset's own event ledger, which is the
    common case.
    """
    try:
        book = DataBook.parse(body.databook)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"could not parse DataBook: {exc}"
        ) from exc

    try:
        block = book.block(body.block_id) if body.block_id else book.primary_graph_block()
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if block.lang == "json-ld":
        try:
            turtle_payload = from_json_ld(block.body)
        except TurtleSyntaxError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    else:
        turtle_payload = block.body
        if settings.parse_mode == "local":
            try:
                parse(turtle_payload)
            except TurtleSyntaxError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    graph_iri = body.graph_iri or book.named_graph or conn.graph("events")

    result = await write_turtle_to_graph(
        turtle=turtle_payload,
        graph_iri=graph_iri,
        mode="merge",
        shapes_graph=body.shapes_graph,
        reduction_rule_id=body.reduction_rule_id,
        conn=conn,
        client=client,
        shacl_required=settings.shacl_required,
        shacl_delta=settings.shacl_delta,
        animus=animus,
    )
    return {**result, "sourceBlock": block.id or "(unlabelled)", "sourceLang": block.lang}
