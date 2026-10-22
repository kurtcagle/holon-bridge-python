"""P2 SHACL tools."""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def validate_turtle(
    turtle: str,
    shapes_graph: str | None = None,
    target_graph: str | None = None,
    mode: str = "auto",
    context: bool = True,
    merge_context: bool = False,
) -> dict:
    """Validate Turtle against a shapes graph already in the store.

    Passing ``target_graph`` validates the payload merged with that graph and
    reports only newly introduced violations, which is what the write path
    does. Without it, the payload is validated in isolation — useful, but it
    will flag cross-references the target graph would have satisfied.

    With no ``shapes_graph`` named, the dataset's context shapes graph
    (``urn:{dataset}:shacl-context``), if it has one, is also run against the
    target merged with the dataset's ontology and taxonomy, as the write path does
    (``context=False`` turns that off). ``merge_context=True`` instead
    validates the named ``shapes_graph`` against the target with the ontology
    merged in -- for trying a context pack before it is installed. Which
    graphs are merged is the dataset's choice; a request cannot name others.
    """
    return await _call(
        "POST",
        "/validate",
        json_body={
            "turtle": turtle,
            "shapes_graph": shapes_graph,
            "target_graph": target_graph,
            "mode": mode,
            "context": context,
            "merge_context": merge_context,
        },
    )
