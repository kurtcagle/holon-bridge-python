"""AssertionEvent submission -- create_message.

Naming note, read before touching this file. This is unrelated to
``tools/pipelines.py``'s ``get_message``/``list_messages``, which report
``hb:Message`` pipeline-run status (Received/Running/Completed/Failed).
This tool writes domain-level ``hev:AssertionEvent`` content into the
dataset's ``events`` graph -- a different graph, a different vocabulary,
a different concept, sharing the word "message" only by coincidence of
two separate naming decisions. See ``holonbridge/routes/events.py``'s
module docstring on the bridge for the full explanation.

Scope, as of 2026-08-28: AssertionEvent submission only -- create_message
never invokes a named trigger, a rule, or the scheduler.
"""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def create_message(
    databook: str,
    block_id: str | None = None,
    graph_iri: str | None = None,
    shapes_graph: str | None = None,
    reduction_rule_id: str | None = None,
) -> dict:
    """Submit a DataBook of AssertionEvent content to the events graph.

    Same DataBook-envelope pattern as ``create_holon``: the first turtle/
    turtle12/json-ld block is extracted (or the one named by ``block_id``)
    and written. Two differences from ``create_holon``: the write is
    always a merge -- an event ledger is append-only, there is no
    ``mode`` parameter -- and ``graph_iri`` defaults to the dataset's own
    events graph when neither it nor the DataBook's ``graph.named_graph``
    frontmatter is supplied, which is the common case, not an error.

    Pass ``shapes_graph`` pointing at a registered EventShape if you want
    the bridge to actually enforce ``hev:AssertionEvent`` typing; this
    tool does not check that on its own, only that the payload is
    well-formed RDF.
    """
    return await _call(
        "POST",
        "/message/create",
        json_body={
            "databook": databook,
            "block_id": block_id,
            "graph_iri": graph_iri,
            "shapes_graph": shapes_graph,
            "reduction_rule_id": reduction_rule_id,
        },
    )
