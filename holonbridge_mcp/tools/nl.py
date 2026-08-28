"""P3 natural-language query tool."""

from __future__ import annotations

import json

import httpx

from ..session import mcp, _call, ANTHROPIC_KEY, ANTHROPIC_MODEL


@mcp.tool()
async def nl_query(question: str, graph: str | None = None) -> dict:
    """Answer a natural-language question by generating and running SPARQL.

    Returns the generated query alongside the results so it can be inspected
    and corrected. Works best over well-labelled data and simple SELECT shapes.
    """
    if not ANTHROPIC_KEY:
        return {
            "error": True,
            "detail": "nl_query needs ANTHROPIC_API_KEY; write SPARQL directly instead",
        }

    schema = await _schema_digest(graph)
    query = await _translate(question, schema, graph)
    if not query:
        return {"error": True, "detail": "translation produced no query"}

    results = await _call(
        "POST", "/sparql/select", json_body={"query": query, "graph": graph}
    )
    return {"query": query, "results": results}


async def _schema_digest(graph: str | None) -> str:
    """Sample classes and predicates so the model writes against real terms."""
    scope = f"GRAPH <{graph}>" if graph else "GRAPH ?g"
    classes = await _call(
        "POST",
        "/sparql/select",
        json_body={
            "query": f"""SELECT ?class (COUNT(?s) AS ?n) WHERE {{
  {scope} {{ ?s a ?class }}
}} GROUP BY ?class ORDER BY DESC(?n) LIMIT 40"""
        },
    )
    predicates = await _call(
        "POST",
        "/sparql/select",
        json_body={
            "query": f"""SELECT ?p (COUNT(*) AS ?n) WHERE {{
  {scope} {{ ?s ?p ?o }}
}} GROUP BY ?p ORDER BY DESC(?n) LIMIT 60"""
        },
    )
    return json.dumps({"classes": classes, "predicates": predicates})[:12000]


async def _translate(question: str, schema: str, graph: str | None) -> str | None:
    system = (
        "You translate questions into SPARQL 1.1 SELECT queries for an RDF store. "
        "Use only the classes and predicates listed in the supplied schema digest. "
        "Wrap patterns in a GRAPH clause. Return the query alone, with no prose "
        "and no code fences."
    )
    target = f"The relevant named graph is <{graph}>." if graph else ""
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1200,
                "system": system,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Schema digest:\n{schema}\n\n{target}\n\nQuestion: {question}",
                    }
                ],
            },
        )
    if response.status_code >= 400:
        return None
    blocks = response.json().get("content", [])
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text.replace("```sparql", "").replace("```", "").strip() or None
