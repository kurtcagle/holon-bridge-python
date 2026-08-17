"""Persona-scoped projection: which named graphs a request may read from,
and the two query shapes that need that scope.

Why this file exists (context for anyone opening this cold): the original
draft of this spec built persona/user graph IRIs by hand --
`f"urn:{dataset}:persona:{persona}:user:{person_id}:holons"` -- instead of
calling the naming methods `Conn` (conn.py) already has:
`conn.persona_user_graph(persona, role, user)` for a persona/user graph,
`conn.graph(role)` for org ground truth. Both already fold in the bank
segment (`urn:{bank}:{dataset}:{role}`) whenever a dataset has opted into
`bank_scoped_datasets`. Hand-built f-strings skip that, so the moment a
dataset with persona graphs is migrated onto bank-scoping, the old draft
starts pointing at IRIs that no longer exist -- silently, since a missing
named graph just reads as empty, not as an error. That's the exact
`urn:data:*` drift `Conn`'s own module docstring says it exists to
prevent. This file has no IRI-building logic of its own; it's a thin,
ordered composition of `Conn`'s methods.

Read gating (holon:ReadGrant) is deliberately NOT here -- see acl.py's
check_read / authorize_query. This module only decides which graphs a
resolved (person_id, persona) pair is entitled to have *searched*, not
whether the caller may see what turns up. The two are independent because
`person_id` and `persona` below can only ever resolve to the caller's own
identity and a persona they hold a Home under (see resolve_scope_graphs's
docstring) -- the specific cross-person leak this spec targets is ruled
out by construction, before any ReadGrant check would even run. ReadGrant
enforcement is a separate, later hardening pass, not a dependency of this
one.

INTEGRATION NOTE -- not done in this file: `get_holon` / `state_query` /
`_neighbour_query` in holon.py currently take only `conn: Conn`, no
identity or persona. Wiring this in means the route handler in
holon_routes.py adds `AnimusDep` (deps.py) to resolve `animus.person`,
plus a persona lookup (the switch_persona session-state store -- designed
in the spec, not yet built), and passes `person_id`/`persona` down to
`resolve_scope_graphs` instead of handing `get_holon` a bare `conn`.

OPEN DECISIONS this file makes explicit instead of implicit -- change the
constant, not the call sites, once you've decided:
  - SCOPED_ROLES: which graph roles get persona-scoped at all.
  - HOLONS_BEFORE_SCENE_WITHIN_TIER: sub-ordering inside one precedence
    tier (user / public / ground-truth) -- not implied by the tier
    ordering itself.
  - `include_scene` on build_neighbour_query: today's single-graph
    version never reads scene_graph for containment/connection edges.
    Scoping neighbourhood traversal across scene too is new behaviour,
    not a mechanical port -- default is False, matching current behaviour,
    until that's a decision rather than an accident of reusing the list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .conn import Conn

#: Graph roles that get persona-scoped. Conn.GRAPH_ROLES has thirteen
#: roles; only these two are in play here. `events` (the fluent ledger) is
#: deliberately excluded -- it's an append-only audit trail, treated as
#: ground-truth-only until there's a concrete reason to persona-scope it.
SCOPED_ROLES: Final[tuple[str, ...]] = ("holons", "scene")

#: Reserved userId for a persona's own curated common-knowledge graph --
#: not a real person. Matches the reserved literal Conn.persona_user_graph
#: already documents.
PUBLIC_USER: Final = "public"

#: Whether `holons` outranks `scene` *within* a single tier (user, public,
#: or ground-truth). True preserves the original spec's implicit ordering
#: (structural beats fluent within a tier). This is a real decision, not a
#: consequence of "user > public > ground truth" -- flip it here if wrong.
HOLONS_BEFORE_SCENE_WITHIN_TIER: Final = True

_PREFIXES: Final = """PREFIX holon: <https://w3id.org/holon/>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
"""


@dataclass(frozen=True)
class ScopedGraph:
    """One graph in a resolved scope: its IRI, precedence rank (0 highest),
    and the role it was built for -- kept as a field rather than parsed
    back out of the IRI, so callers never have to string-match graph names
    to tell a holons graph from a scene graph."""

    iri: str
    rank: int
    role: str


def resolve_scope_graphs(
    conn: Conn, *, person_id: str | None, persona: str | None
) -> list[ScopedGraph]:
    """The graphs a (person_id, persona) pair may read from, ordered
    highest-precedence-first: user-private > persona-public > org ground
    truth. Every IRI comes from `conn.persona_user_graph` / `conn.graph`,
    never built by hand -- see the module docstring for why that's the
    point of this function rather than an implementation detail of it.

    `person_id` and `persona` must already be resolved values, never raw
    request input: `person_id` is `Animus.person` (deps.py's
    `require_animus`, itself from the caller's credential -- see acl.py),
    `persona` is that same person's switch_persona session state. Neither
    is a parameter a caller can name arbitrarily. That is the whole
    access-control story at this layer: there is no argument through
    which a caller could ask for someone else's scope, so a scope list
    built from these two values can never contain another person's
    private graph.

    Returns an empty-persona-tier scope (ground truth only) when persona
    is falsy, and skips the user-private tier specifically when person_id
    is falsy even with a persona set -- defensive rather than load-bearing,
    since AnimusDep already refuses any request that can't resolve a
    Person before this function would run.
    """
    roles = SCOPED_ROLES if HOLONS_BEFORE_SCENE_WITHIN_TIER else tuple(reversed(SCOPED_ROLES))
    scope: list[ScopedGraph] = []
    rank = 0

    if persona and person_id:
        for role in roles:
            scope.append(ScopedGraph(conn.persona_user_graph(persona, role, person_id), rank, role))
            rank += 1

    if persona:
        for role in roles:
            scope.append(ScopedGraph(conn.persona_user_graph(persona, role, PUBLIC_USER), rank, role))
            rank += 1

    for role in roles:
        scope.append(ScopedGraph(conn.graph(role), rank, role))
        rank += 1

    return scope


def build_state_query(holon_iri: str, scope: list[ScopedGraph]) -> str:
    """CONSTRUCT the holon's current state across `scope`, with
    per-predicate override: a triple at rank r is suppressed if any graph
    at rank < r asserts the same predicate (any object). That makes
    override per-predicate and set-replacing, not per-triple or
    per-subject -- a private scope asserting one value for a multi-valued
    predicate replaces the whole set ground truth asserted for it, which
    is the correct reading of "takes precedence" but easy to get wrong, so
    it's worth re-checking against the worked example in the spec if this
    query is ever rewritten.
    """
    values = " ".join(f"(<{g.iri}> {g.rank})" for g in scope)
    return f"""CONSTRUCT {{ <{holon_iri}> ?p ?o . }}
WHERE {{
  VALUES (?g ?rank) {{ {values} }}
  GRAPH ?g {{ <{holon_iri}> ?p ?o . }}
  FILTER NOT EXISTS {{
    VALUES (?g2 ?rank2) {{ {values} }}
    GRAPH ?g2 {{ <{holon_iri}> ?p ?o2 . }}
    FILTER (?rank2 < ?rank)
  }}
}}"""


def build_neighbour_query(
    conn: Conn,
    holon_iri: str,
    scope: list[ScopedGraph],
    *,
    root: str,
    direction: str,
    include_scene: bool = False,
) -> str:
    """Role-discovering neighbourhood query (up / down / across), scoped
    across `scope` instead of pinned to conn.holons_graph alone.

    `include_scene=False` matches today's actual behaviour: the existing
    single-graph `_neighbour_query` in holon.py only ever reads
    holons_graph, never scene_graph, because containment/connection
    predicates are structural, not fluent. Passing the full scope list
    unfiltered would silently widen every neighbourhood traversal to
    include scene at every tier -- harmless in practice if that convention
    holds, but a real behaviour change, not a mechanical port. Set this to
    True only once that's a decision, not a default.
    """
    graphs = scope if include_scene else [g for g in scope if g.role == "holons"]
    values = " ".join(f"<{g.iri}>" for g in graphs)

    if direction == "down":
        pattern = f"?neighbour ?predicate <{holon_iri}> ."
    else:  # "up" and "across" share the same triple shape
        pattern = f"<{holon_iri}> ?predicate ?neighbour ."

    return f"""{_PREFIXES}
SELECT DISTINCT ?neighbour ?label ?predicate ?g
WHERE {{
  GRAPH <{conn.ontology_graph}> {{
    ?predicate rdfs:subPropertyOf* holon:{root} .
  }}
  VALUES ?g {{ {values} }}
  GRAPH ?g {{
    {pattern}
    OPTIONAL {{ ?neighbour rdfs:label ?label . }}
  }}
}}
ORDER BY ?predicate ?neighbour"""


# ---------------------------------------------------------------------------
# Example wiring (illustrative only -- AnimusDep and the switch_persona
# session-state store aren't built yet, so this doesn't run as-is):
#
#   async def get_holon(..., conn: ConnDep, animus: AnimusDep):
#       persona = await current_persona(animus.person, conn.dataset)  # TODO
#       scope = resolve_scope_graphs(conn, person_id=animus.person, persona=persona)
#       state = await client.construct(conn, build_state_query(holon_iri, scope))
#       up = build_neighbour_query(conn, holon_iri, scope, root="isPartOf", direction="up")
#       ...
# ---------------------------------------------------------------------------
