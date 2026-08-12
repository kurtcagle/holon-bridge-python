"""Holon retrieval.

``get_holon`` returns a DataBook, not raw Turtle: frontmatter carrying holon
metadata and projection parameters, a Turtle block with current state, the
retrieval query that produced it, and — when present — the boundary shapes.

Neighbourhood traversal discovers its predicates rather than hardcoding
``holon:isPartOf``. Any predicate declared ``rdfs:subPropertyOf*`` a
containment or connection predicate participates, so a domain that models
``geo:administrativePartOf`` or ``org:reportsTo`` navigates without the
bridge knowing those terms exist.

Current state is a union of two graphs: ``holons`` (structural triples,
static) and ``scene`` (fluent current-values, destructively updated on
every transition -- see :mod:`holonbridge.fluent`). Supplying ``observed_at``
switches the fluent-valued part of the projection to an as-of read over
``events`` instead of ``scene``, walking the hevt:StateAssertion ledger by
timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .conn import Conn
from .databook import Block, DataBook
from .fuseki import FusekiClient

HOLON = "https://w3id.org/holon/"
HEVT = "https://w3id.org/holon/event/"

PROJECTION_MODES = ("immersive", "cinematic", "active_inference", "exploded_view")

_PREFIXES = f"""PREFIX holon: <{HOLON}>
PREFIX hevt:  <{HEVT}>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
"""


@dataclass
class Neighbour:
    iri: str
    label: str | None
    predicate: str


def state_query(conn: Conn, holon_iri: str) -> str:
    """CONSTRUCT the holon's own triples: structural (holons) plus current
    fluent values (scene), unioned. A holon with no fluents gets an empty
    second clause and behaves exactly as before scene existed."""
    return f"""{_PREFIXES}
CONSTRUCT {{ <{holon_iri}> ?p ?o . }}
WHERE {{
  {{ GRAPH <{conn.holons_graph}> {{ <{holon_iri}> ?p ?o . }} }}
  UNION
  {{ GRAPH <{conn.scene_graph}> {{ <{holon_iri}> ?p ?o . }} }}
}}"""


def as_of_query(
    conn: Conn,
    holon_iri: str,
    *,
    observed_at: str,
    inserted_at: str | None = None,
) -> str:
    """CONSTRUCT the holon's structural triples plus what its fluents'
    values were as of valid-time ``observed_at`` (an ISO xsd:dateTime
    string), optionally also bounded by transaction-time ``inserted_at``.
    ``inserted_at`` defaults to ``observed_at`` when not supplied -- the
    common case is 'this happened and was recorded at the same moment';
    the two axes diverge only for backdated or corrective entries.

    Walks hevt:StateAssertion by hevt:assertedDateTime directly, rather
    than following hevt:supersedes chains -- consistent with
    hevt:supersedes's own documented intent: it answers lineage, not
    chronology.
    """
    inserted = inserted_at or observed_at
    return f"""{_PREFIXES}
CONSTRUCT {{
  <{holon_iri}> ?p ?o .
  ?fluent holon:currentValue ?value .
}}
WHERE {{
  {{ GRAPH <{conn.holons_graph}> {{ <{holon_iri}> ?p ?o . }} }}
  UNION
  {{
    GRAPH <{conn.holons_graph}> {{
      <{holon_iri}> ?anyPred ?fluent .
      ?fluent a holon:Fluent .
    }}
    GRAPH <{conn.graph("events")}> {{
      ?assertion hevt:forFluent ?fluent ;
                 hevt:hasValue ?value ;
                 hevt:assertedDateTime ?assertedAt .
      FILTER (?assertedAt <= "{observed_at}"^^xsd:dateTime)
      FILTER NOT EXISTS {{
        ?assertion hevt:invalidatedAt ?invalidatedAt .
        FILTER (?invalidatedAt <= "{inserted}"^^xsd:dateTime)
      }}
    }}
    FILTER NOT EXISTS {{
      GRAPH <{conn.graph("events")}> {{
        ?later hevt:forFluent ?fluent ;
               hevt:assertedDateTime ?laterAt .
        FILTER (?laterAt <= "{observed_at}"^^xsd:dateTime && ?laterAt > ?assertedAt)
      }}
    }}
  }}
}}"""


def _neighbour_query(
    conn: Conn, holon_iri: str, *, root: str, direction: str
) -> str:
    """Build a role-discovering neighbourhood query.

    ``direction`` is ``up`` (focus → parent), ``down`` (child → focus), or
    ``across`` (focus → peer, for connection predicates).
    """
    if direction == "up":
        pattern = f"<{holon_iri}> ?predicate ?neighbour ."
    elif direction == "down":
        pattern = f"?neighbour ?predicate <{holon_iri}> ."
    else:
        pattern = f"<{holon_iri}> ?predicate ?neighbour ."

    return f"""{_PREFIXES}
SELECT DISTINCT ?neighbour ?label ?predicate
WHERE {{
  GRAPH <{conn.ontology_graph}> {{
    ?predicate rdfs:subPropertyOf* holon:{root} .
  }}
  GRAPH <{conn.holons_graph}> {{
    {pattern}
    OPTIONAL {{ ?neighbour rdfs:label ?label . }}
  }}
}}
ORDER BY ?predicate ?neighbour"""


async def _neighbours(
    client: FusekiClient, conn: Conn, query: str
) -> list[Neighbour]:
    results = await client.select(conn, query)
    out: list[Neighbour] = []
    for row in results.get("results", {}).get("bindings", []):
        out.append(
            Neighbour(
                iri=row["neighbour"]["value"],
                label=row.get("label", {}).get("value"),
                predicate=row["predicate"]["value"],
            )
        )
    return out


async def get_holon(
    client: FusekiClient,
    conn: Conn,
    *,
    holon_iri: str,
    projection_mode: str = "immersive",
    include_shapes: bool = True,
    observed_at: datetime | None = None,
    inserted_at: datetime | None = None,
) -> DataBook:
    """Retrieve a holon and project it as a DataBook.

    With neither ``observed_at`` nor ``inserted_at``, current state comes
    from :func:`state_query` -- a union of ``holons`` and ``scene``, the
    fast path, no ledger walk. Supplying either switches the fluent-valued
    part of the projection to :func:`as_of_query`; ``observed_at`` defaults
    to now() and ``inserted_at`` defaults to ``observed_at`` when only one
    of the two is given.
    """

    if projection_mode not in PROJECTION_MODES:
        raise ValueError(
            f"unknown projection mode {projection_mode!r}; "
            f"expected one of {', '.join(PROJECTION_MODES)}"
        )

    as_of = observed_at is not None or inserted_at is not None
    eff_observed: datetime | None = None
    eff_inserted: datetime | None = None
    if as_of:
        eff_observed = observed_at or datetime.now(timezone.utc)
        eff_inserted = inserted_at or eff_observed
        query = as_of_query(
            conn,
            holon_iri,
            observed_at=eff_observed.isoformat(),
            inserted_at=eff_inserted.isoformat(),
        )
    else:
        query = state_query(conn, holon_iri)

    state = await client.construct(conn, query)

    up_query = _neighbour_query(conn, holon_iri, root="isPartOf", direction="up")
    down_query = _neighbour_query(conn, holon_iri, root="isPartOf", direction="down")
    across_query = _neighbour_query(
        conn, holon_iri, root="isConnectedTo", direction="across"
    )

    parents = await _neighbours(client, conn, up_query)
    children = await _neighbours(client, conn, down_query)
    peers = await _neighbours(client, conn, across_query)

    frontmatter: dict[str, Any] = {
        "id": holon_iri,
        "type": "holon-projection",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "graph": {
            "dataset": conn.dataset,
            "named_graph": conn.holons_graph,
            "scene_graph": conn.scene_graph,
        },
        "projection": {
            "mode": projection_mode,
            "parents": len(parents),
            "children": len(children),
            "connections": len(peers),
        },
    }
    if as_of:
        frontmatter["asOf"] = {
            "observedAt": eff_observed.isoformat() if eff_observed else None,
            "insertedAt": eff_inserted.isoformat() if eff_inserted else None,
        }

    book = DataBook(frontmatter=frontmatter, body=_summary(holon_iri, parents, children, peers))
    book.blocks.append(
        Block(
            lang="turtle",
            body=state.strip() or f"# no triples found for <{holon_iri}>",
            id="holon-state",
            label="Current holon state",
        )
    )
    book.blocks.append(
        Block(lang="sparql", body=query, id="state-query", label="State retrieval query")
    )
    book.blocks.append(
        Block(
            lang="sparql",
            body=(
                "# up (contained by)\n"
                + up_query
                + "\n\n# down (contains)\n"
                + down_query
                + "\n\n# across (connected to)\n"
                + across_query
            ),
            id="retrieval-query",
            label="Neighbourhood queries (role-discovering)",
        )
    )

    if include_shapes:
        shapes = await _boundary_shapes(client, conn, holon_iri)
        if shapes.strip():
            book.blocks.append(
                Block(
                    lang="shacl",
                    body=shapes.strip(),
                    id="boundary-shapes",
                    label="Boundary shapes",
                )
            )

    return book


async def _boundary_shapes(
    client: FusekiClient, conn: Conn, holon_iri: str
) -> str:
    """Fetch shapes whose target class matches any type of this holon."""
    query = f"""{_PREFIXES}
PREFIX sh: <http://www.w3.org/ns/shacl#>

CONSTRUCT {{ ?shape ?p ?o . }}
WHERE {{
  GRAPH <{conn.holons_graph}> {{ <{holon_iri}> rdf:type ?class . }}
  GRAPH <{conn.shapes_graph}> {{
    ?shape sh:targetClass ?class ;
           ?p ?o .
  }}
}}"""
    try:
        return await client.construct(conn, query)
    except Exception:  # shapes are optional; never fail a read over them
        return ""


def _summary(
    holon_iri: str,
    parents: list[Neighbour],
    children: list[Neighbour],
    peers: list[Neighbour],
) -> str:
    lines = [f"# Holon projection", "", f"`{holon_iri}`", ""]

    def section(title: str, items: list[Neighbour]) -> None:
        lines.append(f"## {title}")
        if not items:
            lines.append("")
            lines.append("_none_")
            lines.append("")
            return
        lines.append("")
        lines.append("| Holon | Label | Predicate |")
        lines.append("|---|---|---|")
        for item in items:
            lines.append(f"| `{item.iri}` | {item.label or ''} | `{item.predicate}` |")
        lines.append("")

    section("Contained by", parents)
    section("Contains", children)
    section("Connected to", peers)
    return "\n".join(lines)
