"""P2 SHACL tools."""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def validate_turtle(
    turtle: str,
    shapes_graph: str | None = None,
    target_graph: str | None = None,
    mode: str = "auto",
) -> dict:
    """Validate Turtle against a shapes graph already in the store.

    Passing ``target_graph`` validates the payload merged with that graph and
    reports only newly introduced violations, which is what the write path
    does. Without it, the payload is validated in isolation — useful, but it
    will flag cross-references the target graph would have satisfied.
    """
    return await _call(
        "POST",
        "/validate",
        json_body={
            "turtle": turtle,
            "shapes_graph": shapes_graph,
            "target_graph": target_graph,
            "mode": mode,
        },
    )
