"""Holon retrieval.

``get_holon`` returns a DataBook, not raw Turtle: frontmatter carrying holon
metadata and projection parameters, a Turtle block with current state, the
retrieval query that produced it, and — when present — the boundary shapes.

Neighbourhood traversal discovers its predicates rather than hardcoding
``holon:isPartOf``. Any predicate declared ``rdfs:subPropertyOf*`` a
containment or connection predicate participates, so a domain that models
``geo:administrativePartOf`` or ``org:reportsTo`` navigates without the
bridge knowing those terms exist.

CHANGED 2026-08-17: current-state and neighbourhood reads are now
persona-scoped, not pinned to ground truth alone. The caller (holon_routes.py)
resolves a ``list[ScopedGraph]`` via ``persona_scope.resolve_scope_graphs``
-- ordered user-private > persona-public > ground-truth, gated entirely by
whether this caller currently holds an active persona (which itself is
gated by Home-membership at switch_persona time, see persona_state.py) --
and passes it in as ``scope``. No persona active means scope is exactly
the two ground-truth graphs, same as before this change.

Current state is now built with ``persona_scope.build_state_query``
(precedence CONSTRUCT across ``scope``) rather than a plain two-graph
UNION. Worth naming explicitly: this is a semantic change even in the
no-persona case, from "union both graphs' triples" to "suppress a
lower-ranked graph's triple for a predicate a higher-ranked graph already
asserts." In practice these produce identical results for holons/scene,
since a predicate is always either structural (holons) or fluent (scene)
never both -- but that's a fact about how this system is actually
populated, not something either query enforces, so it's a behaviour
change worth someone's eyes rather than an invisible no-op.

NOT YET SCOPED: ``as_of_query`` (the ``observed_at``/``inserted_at``
path). It still reads ground truth only (``conn.holons_graph`` /
``conn.graph("events")``), ignoring whatever ``scope`` it's given.
Persona-scoping an as-of read means walking the events ledger per scoped
graph with the same rank-ordered precedence ``build_state_query`` applies
to current state, which ``persona_scope.py`` has no equivalent for yet --
a real gap, not an oversight, and worth its own pass rather than a rushed
extension here.

CHANGED 2026-09-30: three corrections to the as-of path, made when
``observed_at`` was first exposed through the MCP ``get_holon`` tool (until
then only the REST route reached it):

- The projection frontmatter's ``scope`` now reports the graphs an as-of
  read actually searched for state (the holons and events graphs), not the
  caller's persona scope, which the as-of query ignores. The scope that the
  neighbourhood queries did use is reported separately as
  ``neighbourScope``. Previously an as-of read listed graphs it had not
  read, which made a ground-truth-only answer look persona-scoped.
- A holon with a list-mode fluent (``holon:ListAccumulator``) is refused
  with :class:`AsOfUnsupportedError`. A list fluent's history is a sequence
  of ``ListInsert``/``ListRemove`` events whose ``hevt:hasValue`` is the
  single member touched, so the as-of projection would report the last
  member as if it were the whole set. Refusing is honest; replaying the
  membership events up to ``observed_at`` is the real fix, and is not done.
- Timezone-naive ``observed_at``/``inserted_at`` are read as UTC. Stored
  ``hevt:assertedDateTime`` values are timezone-aware, and SPARQL orders a
  naive dateTime against an aware one only when they are more than 14 hours
  apart -- otherwise the comparison is an error and the FILTER silently
  drops the row, so a naive timestamp would return an empty projection
  rather than a wrong-looking one.

CHANGED 2026-11-13: transaction time is now recorded. ``update_fluent``
stamps every ledger entry with ``hevt:insertedAt`` (the wall clock at the
write, never caller-supplied), and ``update_fluent`` takes an optional
``asserted_datetime`` over REST and MCP so valid time can be backdated
independently of it. ``inserted_at`` now also excludes entries inserted
after it. When it is not given there is no insertion bound (it used to
default to ``observed_at``, which would now hide every entry written since
the moment observed). Entries from before this change carry no
``hevt:insertedAt`` and count as known from the start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .conn import Conn
from .databook import Block, DataBook
from .fuseki import FusekiClient
from .persona_scope import ScopedGraph, build_neighbour_query, build_state_query

HOLON = "https://w3id.org/holon/"
HEVT = "https://w3id.org/holon/event/"

PROJECTION_MODES = ("immersive", "cinematic", "active_inference", "exploded_view")

_PREFIXES = f"""PREFIX holon: <{HOLON}>
PREFIX hevt:  <{HEVT}>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
"""


class AsOfUnsupportedError(ValueError):
    """An as-of read was requested for a holon this bridge cannot
    reconstruct at a past time. ``fluents`` names the offending fluents."""

    def __init__(self, message: str, *, fluents: list[str]) -> None:
        super().__init__(message)
        self.fluents = fluents


def _as_utc(value: datetime) -> datetime:
    """Read a timezone-naive datetime as UTC. See the module docstring."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _list_mode_fluents(
    client: FusekiClient, conn: Conn, holon_iri: str
) -> list[str]:
    """Fluents of ``holon_iri`` whose property is declared
    ``holon:ListAccumulator`` -- the same holon -> fluent -> FluentProperty
    -> operation-mode path ``fluent._read_operation_mode`` follows."""
    query = f"""{_PREFIXES}
SELECT DISTINCT ?fluent WHERE {{
  GRAPH <{conn.holons_graph}> {{
    <{holon_iri}> ?anyPred ?fluent .
    ?fluent a holon:Fluent ;
            holon:fluentProperty ?prop .
  }}
  GRAPH <{conn.ontology_graph}> {{
    ?prop holon:fluentOperationMode holon:ListAccumulator .
  }}
}}
ORDER BY ?fluent
LIMIT 20"""
    results = await client.select(conn, query)
    return [row["fluent"]["value"] for row in results.get("results", {}).get("bindings", [])]


@dataclass
class Neighbour:
    iri: str
    label: str | None
    predicate: str


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
    With ``inserted_at`` the read is "what the record held at
    ``inserted_at`` about ``observed_at``": an assertion counts only if it
    had been inserted (``hevt:insertedAt``) by then. Without it there is no
    transaction-time bound, so a backdated entry written after
    ``observed_at`` still shows, which is what an ordinary valid-time read
    wants. Entries written before ``hevt:insertedAt`` was recorded carry
    none and are treated as known from the start. ``hevt:invalidatedAt`` is
    still bounded by ``inserted_at``, or by ``observed_at`` when that is
    not given, as before.

    Ground-truth only -- see this module's CHANGED note for why an as-of
    read isn't persona-scoped yet.

    Walks hevt:StateAssertion by hevt:assertedDateTime directly, rather
    than following hevt:supersedes chains -- consistent with
    hevt:supersedes's own documented intent: it answers lineage, not
    chronology.
    """
    inserted = inserted_at or observed_at

    def known_by(subject: str, inserted_var: str) -> str:
        # Counts as known at inserted_at unless inserted after it; no
        # insertedAt (a legacy entry) counts as known.
        if not inserted_at:
            return ""
        return (
            f"OPTIONAL {{ {subject} hevt:insertedAt {inserted_var} }}\n"
            f"      FILTER (!BOUND({inserted_var}) || "
            f'{inserted_var} <= "{inserted_at}"^^xsd:dateTime)'
        )

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
      {known_by("?assertion", "?insertedAt")}
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
        {known_by("?later", "?laterInsertedAt")}
      }}
    }}
  }}
}}"""


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
    scope: list[ScopedGraph],
    projection_mode: str = "immersive",
    include_shapes: bool = True,
    observed_at: datetime | None = None,
    inserted_at: datetime | None = None,
) -> DataBook:
    """Retrieve a holon and project it as a DataBook.

    ``scope`` is the caller's resolved read scope (see
    ``persona_scope.resolve_scope_graphs``) -- the route, not this
    function, resolves identity and persona; this function only ever
    consumes an already-resolved scope, the same separation ``holon_routes.py``
    keeps between identity resolution and everything downstream of it.

    With neither ``observed_at`` nor ``inserted_at``, current state comes
    from ``persona_scope.build_state_query`` across ``scope`` -- the fast
    path, no ledger walk. Supplying either switches the fluent-valued part
    of the projection to :func:`as_of_query`, which is NOT scope-aware yet
    (see the module docstring) -- ``observed_at`` defaults to now(). With
    no ``inserted_at`` there is no transaction-time bound.
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
        eff_observed = _as_utc(observed_at) if observed_at else datetime.now(timezone.utc)
        eff_inserted = _as_utc(inserted_at) if inserted_at else None
        list_fluents = await _list_mode_fluents(client, conn, holon_iri)
        if list_fluents:
            raise AsOfUnsupportedError(
                "as-of reads are not supported for a holon with list-mode "
                "(holon:ListAccumulator) fluents: the history holds one member per "
                "event, so the projection would report the last member touched rather "
                "than the set as it stood. Omit observed_at to read current state. "
                f"Affected fluents: {', '.join(list_fluents)}",
                fluents=list_fluents,
            )
        query = as_of_query(
            conn,
            holon_iri,
            observed_at=eff_observed.isoformat(),
            inserted_at=eff_inserted.isoformat() if eff_inserted else None,
        )
    else:
        query = build_state_query(holon_iri, scope)

    state = await client.construct(conn, query)

    up_query = build_neighbour_query(conn, holon_iri, scope, root="isPartOf", direction="up")
    down_query = build_neighbour_query(conn, holon_iri, scope, root="isPartOf", direction="down")
    across_query = build_neighbour_query(
        conn, holon_iri, scope, root="isConnectedTo", direction="across"
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
        # Precedence-ordered graph IRIs actually searched for this read --
        # explicit so a reader can see which scope produced this
        # projection without re-deriving it, especially now that the same
        # holon_iri can legitimately return different content to
        # different callers. An as-of read does not use the persona scope
        # for state (see the module docstring), so it reports the graphs
        # as_of_query really reads, and the scope the neighbourhood
        # queries used goes in ``neighbourScope`` below.
        "scope": (
            [conn.holons_graph, conn.graph("events")] if as_of else [g.iri for g in scope]
        ),
        "projection": {
            "mode": projection_mode,
            "parents": len(parents),
            "children": len(children),
            "connections": len(peers),
        },
    }
    if as_of:
        frontmatter["neighbourScope"] = [g.iri for g in scope]
        frontmatter["asOf"] = {
            "observedAt": eff_observed.isoformat() if eff_observed else None,
            "insertedAt": eff_inserted.isoformat() if eff_inserted else None,
            "note": (
                "Valid-time read over ground truth only (holons and events graphs); "
                "persona overlays are not applied to state. insertedAt, when given, "
                "limits the read to entries inserted (hevt:insertedAt) by then; "
                "entries recorded before insertion time was stamped count as known."
            ),
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
    """Fetch shapes whose target class matches any type of this holon.

    Deliberately ground-truth only, always -- shapes are not one of
    persona_scope's SCOPED_ROLES, and a persona having a different
    validation boundary than everyone else isn't a thing this system
    models, so there's no scope argument to thread through here.
    """
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
