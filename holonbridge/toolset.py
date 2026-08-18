"""Toolset membership and persona-parameterised invocation for named
queries and named rules.

Design doc: kurtcagle/hga2/architecture/persona-tool-scoping-design.md.
First-pass scope per that doc's §7: this module implements §2 (Toolset
membership, the hard gate) and §4 Tier 2 (`$persona`/`persona` auto-bound
as a query/rule parameter). Tier 1 (persona style metadata) needs no code
at all -- an ordinary property on a Persona resource, already fully
readable via the existing generic SPARQL/get_holon machinery. Tier 3
(whole-definition override) and widget porting are both explicitly
deferred in the doc and not touched here.

Two independent things, kept independent on purpose -- see the design
doc's §0 for why this is a separate mechanism from persona_scope.py, not
an extension of it:

- resolve_reachable: the hard gate. Which tool ids a persona can reach at
  all. Flat containment in ground truth, no tiering -- a Toolset is not a
  per-person fact the way holon data is (it answers "what can Aimee do",
  never "what can Aimee-for-Kurt-specifically do"), so this needs none of
  persona_user_graph's per-user machinery.
- bind_persona_param: the soft variation. Auto-binds the caller's active
  persona as a query/rule parameter, server-side only -- never accepted as
  a client-supplied value, the same rule switch_persona itself follows for
  `person`.

CORRECTED 2026-08-18: resolve_reachable was originally one query using
`(EXISTS {...} AS ?var)` in the SELECT projection. Caught by actually
running it (against an rdflib-backed stub, the same technique the test
suite uses for named-queries/rules -- not against live Fuseki, which may
well have handled it): rdflib's SPARQL engine cannot evaluate EXISTS
inside a projection expression at all, only inside a FILTER. Since the
test suite's own stub story depends on rdflib evaluating whatever this
module sends it, portability to rdflib is a real requirement here, not
just a nice-to-have -- rewritten as two plain SELECT DISTINCT queries
(restricted-tools, persona-reachable-tools) combined in Python instead,
which is both simpler to read and avoids the whole question of which
engines support which EXISTS placement.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Mapping

from .conn import Conn

HOLON = "https://w3id.org/holon/"

QueryFn = Callable[[str], "dict | Awaitable[dict]"]

#: The parameter name a query/rule declares to opt into Tier-2 persona
#: binding. Not configurable per-tool beyond this -- one name, same as
#: `$this` is one name for rule focus binding.
PERSONA_PARAM_NAME = "persona"


async def _run(query_fn: QueryFn, query: str) -> dict:
    result = query_fn(query)
    if hasattr(result, "__await__"):
        result = await result
    return result


def _tool_set(result: dict) -> set[str]:
    rows = result.get("results", {}).get("bindings", [])
    return {row["tool"]["value"] for row in rows}


async def resolve_reachable(
    query_fn: QueryFn,
    conn: Conn,
    *,
    persona: str | None,
    candidate_iris: list[str],
) -> set[str]:
    """Which of `candidate_iris` (already-loaded tool IRIs -- from
    LoadResult.queries / RuleLoadResult.rules, each tool's `.iri`, never
    independently rediscovered from ground truth) this persona can reach:
    every tool with no holon:Toolset membership at all (universal) union
    every tool reachable via one of this persona's own holon:hasToolset
    links.

    Two queries, scoped to exactly the candidates the caller already
    loaded -- this never tries to independently decide "what is a tool"
    from ground truth; the registry loaders already did that, and
    duplicating that decision here would be a second place for the two to
    drift apart.

    A dataset with zero holon:Toolset resources anywhere degrades to
    "every candidate is universal" automatically -- the first query below
    returns nothing restricted when no `a holon:Toolset` triple exists at
    all, so this needs no separate "have Toolsets even been adopted yet"
    check. Nothing already registered needs migrating for this to ship
    safely.

    persona=None skips the second query entirely and returns exactly the
    universal set.
    """
    if not candidate_iris:
        return set()

    values = " ".join(f"<{iri}>" for iri in candidate_iris)
    holons = conn.graph("holons")

    restricted_query = f"""PREFIX holon: <{HOLON}>
SELECT DISTINCT ?tool WHERE {{
  GRAPH <{holons}> {{
    VALUES ?tool {{ {values} }}
    ?tool holon:isPartOf ?anyToolset .
    ?anyToolset a holon:Toolset .
  }}
}}"""
    restricted = _tool_set(await _run(query_fn, restricted_query))

    reachable_via_persona: set[str] = set()
    if persona:
        persona_query = f"""PREFIX holon: <{HOLON}>
SELECT DISTINCT ?tool WHERE {{
  GRAPH <{holons}> {{
    VALUES ?tool {{ {values} }}
    <{persona}> holon:hasToolset ?toolset .
    ?tool holon:isPartOf ?toolset .
  }}
}}"""
        reachable_via_persona = _tool_set(await _run(query_fn, persona_query))

    universal = set(candidate_iris) - restricted
    return universal | reachable_via_persona


def bind_persona_param(
    params: Mapping[str, object] | None,
    *,
    persona_iri: str | None,
    declares_persona: bool,
) -> dict[str, object]:
    """params with a `persona` entry injected when the tool declares that
    parameter name AND a persona is active -- never injected into a tool
    that hasn't declared it. That check matters specifically for hquery:
    named queries: `_bind_values` rejects any *undeclared* supplied
    parameter outright (a real, already-shipped safety check against
    typos), so blindly injecting `persona` everywhere would turn every
    hquery: query that has nothing to do with personas into a new,
    unrelated failure. A tool that hasn't declared `persona` is exactly
    the design doc's "some tools may have no differentiator whatsoever"
    case, and this makes that the silent, zero-cost default rather than
    an error.

    persona_iri must already be resolved server-side
    (personas.get(person_id=animus.person, dataset=conn.dataset)), never
    accepted from the request body -- the caller's own `persona` key, if
    supplied for some unrelated reason, is always overwritten (or
    removed, if no persona is active), the same rule switch_persona
    itself follows for `person`.
    """
    merged = dict(params or {})
    if not declares_persona:
        return merged
    if persona_iri:
        merged[PERSONA_PARAM_NAME] = persona_iri
    else:
        merged.pop(PERSONA_PARAM_NAME, None)
    return merged
