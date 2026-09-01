"""Access control: resolve who is asking, decide what they may do.

Implements the model documented in the ACL architecture DataBook
(``causalspark-acl-architecture.databook.md``) against whatever is actually
in a dataset's ``holons`` graph -- Person, Role, Persona, and grant
instances. Nothing here is dataset-specific; it queries the model, it
doesn't assume its contents.

Two things this module is deliberately *not*:

- Not a SPARQL engine. ``query_fn`` is injected so this can run against a
  real Fuseki backend in production and an in-memory graph in tests without
  the decision logic caring which.
- Not a full SPARQL sandbox. ``extract_graph_refs`` finds every graph this
  library's own parser can see referenced in a query -- GRAPH clauses,
  FROM/FROM NAMED, SERVICE targets, and INSERT/DELETE/USING/WITH clauses in
  an update. It cannot see through property paths that construct IRIs
  dynamically, nested subqueries the parser rejects, or a federated SERVICE
  call to an endpoint outside this store. Treat a parse failure as "assume
  the worst", never as "assume the best" -- see ``authorize_query``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# A query function: given a SPARQL SELECT/ASK string, return SPARQL-JSON
# bindings (the same shape python's own FusekiClient.select already returns).
# Sync or async both work -- callers await if it's a coroutine.
QueryFn = Callable[[str], "dict | Awaitable[dict]"]

HOLON = "https://w3id.org/holon/"


class Denied(Exception):
    """Raised by the strict helpers; carries the reason for the 403 body."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AclDecision:
    allowed: bool
    reason: str
    person: str | None = None
    matched_grant: str | None = None


@dataclass(frozen=True)
class Animus:
    """A resolved caller. Built once per request, passed down like ``Conn``.

    CHANGED 2026-09-01: added ``real_person``/``real_person_label``/
    ``acting_as`` for the admin ``act_as`` impersonation feature (see
    ``acting_as.py`` and ``deps.py``'s ``require_animus``). ``person`` is
    who this request should be treated as -- possibly an admin's
    impersonation target -- while ``real_person`` is always the identity
    that actually authenticated, whether or not an override is active.
    For an ordinary, non-impersonated request the two are equal and
    ``acting_as`` is False; every existing caller that only ever read
    ``.person`` keeps working unchanged. Code that needs to know "is this
    caller genuinely an admin, regardless of who they're currently acting
    as" -- the act_as/cease_acting_as routes themselves -- must check
    ``real_person``, never ``person``.
    """

    external_id: str
    external_id_type: str
    person: str | None
    person_label: str | None = None
    teams: frozenset[str] = field(default_factory=frozenset)
    real_person: str | None = None
    real_person_label: str | None = None
    acting_as: bool = False


async def _run(query_fn: QueryFn, query: str) -> dict:
    result = query_fn(query)
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[assignment]
    return result  # type: ignore[return-value]


def _bindings(result: dict) -> list[dict]:
    return result.get("results", {}).get("bindings", [])


# --------------------------------------------------------------------------
# Identity resolution
# --------------------------------------------------------------------------

async def resolve_person(
    query_fn: QueryFn,
    holons_graph: str,
    *,
    external_id: str,
    external_id_type: str = "GitHubIdentity",
) -> tuple[str, str] | None:
    """External identity -> (Person IRI, Person label), or None if unknown.

    Mirrors the query already verified by hand against the live store:
    external_id -> holon:hasExternalIdentity/holon:identifier -> Person.
    """
    query = f"""
    PREFIX holon: <{HOLON}>
    PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?person ?label WHERE {{
      GRAPH <{holons_graph}> {{
        ?person holon:hasExternalIdentity ?identity ;
                rdfs:label ?label .
        ?identity a holon:{external_id_type} ;
                  holon:identifier {_literal(external_id)} .
      }}
    }} LIMIT 1
    """
    rows = _bindings(await _run(query_fn, query))
    if not rows:
        return None
    return rows[0]["person"]["value"], rows[0]["label"]["value"]


async def resolve_teams(
    query_fn: QueryFn, holons_graph: str, *, person: str
) -> frozenset[str]:
    """Every Team a Person belongs to, if any. Empty set is normal, not an error --
    most people will belong to no team until team structure actually exists."""
    query = f"""
    PREFIX holon: <{HOLON}>
    SELECT ?team WHERE {{
      GRAPH <{holons_graph}> {{ <{person}> holon:memberOfTeam ?team . }}
    }}
    """
    rows = _bindings(await _run(query_fn, query))
    return frozenset(r["team"]["value"] for r in rows)


async def build_animus(
    query_fn: QueryFn,
    holons_graph: str,
    *,
    external_id: str,
    external_id_type: str = "GitHubIdentity",
) -> Animus:
    """One call to go from a presented identity to everything a decision needs."""
    resolved = await resolve_person(
        query_fn, holons_graph, external_id=external_id, external_id_type=external_id_type
    )
    if resolved is None:
        return Animus(external_id, external_id_type, person=None)
    person, label = resolved
    teams = await resolve_teams(query_fn, holons_graph, person=person)
    return Animus(external_id, external_id_type, person=person, person_label=label, teams=teams)


async def build_animus_as(
    query_fn: QueryFn, holons_graph: str, *, person: str
) -> Animus:
    """Build an Animus for a known Person IRI directly, skipping the
    external-identity lookup ``build_animus`` normally starts from.

    Used only by the admin ``act_as`` override in ``deps.py``'s
    ``require_animus``: the caller already authenticated with a real
    external identity of their own; this substitutes who downstream ACL
    checks treat them as, not how they got in the door. ``external_id``/
    ``external_id_type`` deliberately record the impersonation itself
    (``"act-as:<person>"`` / ``"ActingAs"``) rather than a real external
    credential, so nothing downstream -- logging, whoami, a future audit
    query -- can mistake this for a genuine independent login.

    Does not check whether ``person`` actually exists as a ``holon:Person``
    in ``holons_graph``; the ``/admin/act-as`` route checks that itself
    before ever calling this, once, at the point an operator sets the
    override -- re-checking on every subsequent request this override
    applies to would be redundant, not safer.
    """
    query = f"""
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?label WHERE {{
      GRAPH <{holons_graph}> {{ <{person}> rdfs:label ?label }}
    }} LIMIT 1
    """
    rows = _bindings(await _run(query_fn, query))
    label = rows[0]["label"]["value"] if rows else None
    teams = await resolve_teams(query_fn, holons_graph, person=person)
    return Animus(
        external_id=f"act-as:{person}",
        external_id_type="ActingAs",
        person=person,
        person_label=label,
        teams=teams,
    )


# --------------------------------------------------------------------------
# Admin bypass
# --------------------------------------------------------------------------
#
# Deliberately NOT another entry in the most-specific-wins precedence chain
# that governs everything else in this module (individual override beats
# role grant). Admin is a separate axis -- a narrowly-held, break-glass
# capability for bootstrap and administration, not a role competing for
# access the ordinary way. Holding it bypasses every check below
# unconditionally, including past a deniedTo override. Because it skips the
# override check too, it should be granted sparingly and audited, not
# treated as "just another role with more grants."

def _admin_role(holons_graph: str) -> str:
    """The reserved admin Role IRI for this dataset, derived the same way
    every other org-tier resource is named: swap the trailing ``:holons``
    for the resource's own path segment."""
    suffix = ":holons"
    if not holons_graph.endswith(suffix):
        raise ValueError(f"expected holons_graph to end with {suffix!r}, got {holons_graph!r}")
    return holons_graph[: -len(suffix)] + ":role:admin"


async def is_admin(query_fn: QueryFn, holons_graph: str, *, person: str) -> bool:
    """Whether ``person`` holds the reserved admin Role for this dataset."""
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{ GRAPH <{holons_graph}> {{ <{person}> holon:hasRole <{_admin_role(holons_graph)}> . }} }}
    """
    result = await _run(query_fn, query)
    return bool(result.get("boolean"))


# --------------------------------------------------------------------------
# Grant checks
# --------------------------------------------------------------------------
#
# All share one shape: check admin first (unconditional bypass), then
# either an individual override or an explicit grant. Absence of a grant is
# a denial, never an allow -- there is no default-permit path anywhere in
# this module below the admin bypass.

async def _denied_by_override(
    query_fn: QueryFn, holons_graph: str, *, target: str, person: str
) -> bool:
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{ GRAPH <{holons_graph}> {{ <{target}> holon:deniedTo <{person}> . }} }}
    """
    result = await _run(query_fn, query)
    return bool(result.get("boolean"))


async def check_read(
    query_fn: QueryFn, holons_graph: str, *, person: str, persona: str
) -> AclDecision:
    """May ``person`` read content scoped to ``persona`` (a Persona IRI)?"""
    if await is_admin(query_fn, holons_graph, person=person):
        return AclDecision(True, "admin role bypass", person)
    if await _denied_by_override(query_fn, holons_graph, target=persona, person=person):
        return AclDecision(False, "individual deniedTo override", person)

    query = f"""
    PREFIX holon: <{HOLON}>
    SELECT ?grant WHERE {{
      GRAPH <{holons_graph}> {{
        <{person}> holon:hasRole ?role .
        ?role holon:grants ?grant .
        ?grant a holon:ReadGrant ; holon:scope <{persona}> .
      }}
    }} LIMIT 1
    """
    rows = _bindings(await _run(query_fn, query))
    if rows:
        return AclDecision(True, "role grant", person, rows[0]["grant"]["value"])
    return AclDecision(False, "no ReadGrant for this persona, on any role held", person)


async def check_invoke(
    query_fn: QueryFn, holons_graph: str, *, person: str, named_query: str
) -> AclDecision:
    """May ``person`` invoke ``named_query`` (or a NamedRule, same shape)?"""
    if await is_admin(query_fn, holons_graph, person=person):
        return AclDecision(True, "admin role bypass", person)
    if await _denied_by_override(query_fn, holons_graph, target=named_query, person=person):
        return AclDecision(False, "individual deniedTo override", person)

    query = f"""
    PREFIX holon: <{HOLON}>
    SELECT ?grant WHERE {{
      GRAPH <{holons_graph}> {{
        <{person}> holon:hasRole ?role .
        ?role holon:grants ?grant .
        ?grant a holon:InvokeGrant ; holon:scope <{named_query}> .
      }}
    }} LIMIT 1
    """
    rows = _bindings(await _run(query_fn, query))
    if rows:
        return AclDecision(True, "role grant", person, rows[0]["grant"]["value"])
    return AclDecision(False, "no InvokeGrant for this query, on any role held", person)


async def check_write(
    query_fn: QueryFn, holons_graph: str, *, person: str, target: str
) -> AclDecision:
    """May ``person`` write (append/merge) to ``target``? Never via an
    ordinary Role grant -- see the read/write asymmetry in the DataBook --
    and the admin bypass is the sole deliberate exception to that.
    Otherwise only an explicit holon:grantsWrite naming this exact
    (person, target) pair allows it. This governs additive/merge writes
    only -- see check_replace for wholesale graph overwrite, which is
    deliberately a stricter, independent grant."""
    if await is_admin(query_fn, holons_graph, person=person):
        return AclDecision(True, "admin role bypass", person)
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{
      GRAPH <{holons_graph}> {{ <{target}> holon:grantsWrite <{person}> . }}
    }}
    """
    result = await _run(query_fn, query)
    if result.get("boolean"):
        return AclDecision(True, "explicit grantsWrite", person, target)
    return AclDecision(False, "writes are never role-based; no explicit grantsWrite found", person)


async def check_replace(
    query_fn: QueryFn, holons_graph: str, *, person: str, target: str
) -> AclDecision:
    """May ``person`` wholesale-replace ``target`` (GSP PUT -- discards
    everything already in the graph, not an additive merge)?

    Added 2026-08-17 on Kurt's own framing: appends should be the norm,
    not the exception, so the ability to overwrite everyone else's
    contribution to a shared graph in one call must never be implied by an
    ordinary holon:grantsWrite. Deliberately a separate predicate, not a
    stronger reading of the same one -- holding grantsWrite for a graph
    confers nothing here, by design, and as of this writing nothing in any
    dataset holds grantsReplace at all, which is the intended starting
    state (appends available by default, replace available to no one until
    someone deliberately decides otherwise), not an oversight to backfill.
    Same admin bypass, same fail-closed default as check_write."""
    if await is_admin(query_fn, holons_graph, person=person):
        return AclDecision(True, "admin role bypass", person)
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{
      GRAPH <{holons_graph}> {{ <{target}> holon:grantsReplace <{person}> . }}
    }}
    """
    result = await _run(query_fn, query)
    if result.get("boolean"):
        return AclDecision(True, "explicit grantsReplace", person, target)
    return AclDecision(
        False,
        "replace is stricter than write; no explicit grantsReplace found "
        "(holding grantsWrite does not imply it)",
        person,
    )


def _literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------
# Query-graph-reference extraction (best-effort, fail-closed on the caller)
# --------------------------------------------------------------------------

def extract_graph_refs(sparql_text: str) -> set[str] | None:
    """Every graph IRI this SPARQL query or update references, so a request
    that reaches beyond the ``graph`` field it declared can still be caught.

    Works against the SPARQL *algebra* (``translateQuery``/``translateUpdate``),
    not the raw parse tree -- the algebra is what rdflib itself executes, so
    a graph reference this function misses is a graph reference the engine
    wouldn't have executed either. Handles GRAPH clauses and SERVICE targets
    in queries, and the ``quads`` dict rdflib builds for INSERT DATA,
    DELETE WHERE, and DELETE/INSERT/WHERE.

    Returns ``None`` if the text can't be parsed at all -- callers MUST
    treat that as "deny", never as "nothing to check". A query this module
    cannot read is not evidence it's safe.
    """
    from rdflib.plugins.sparql.algebra import translateQuery, translateUpdate
    from rdflib.plugins.sparql.parser import parseQuery, parseUpdate
    from rdflib.term import URIRef

    def _walk(node: object, refs: set[str], seen: set[int]) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        name = getattr(node, "name", None)
        if name in ("Graph", "ServiceGraphPattern"):
            term = node.get("term") if hasattr(node, "get") else None
            if isinstance(term, URIRef):
                refs.add(str(term))
        if hasattr(node, "keys") and callable(node.keys):
            for key in node.keys():
                value = node[key]
                if key == "quads" and isinstance(value, dict):
                    for graph_ref in value.keys():
                        if isinstance(graph_ref, URIRef):
                            refs.add(str(graph_ref))
                    continue
                _walk(value, refs, seen)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value, refs, seen)

    refs: set[str] = set()
    try:
        algebra = translateQuery(parseQuery(sparql_text))
        _walk(algebra.algebra, refs, set())
        return refs
    except Exception:
        pass

    try:
        algebra = translateUpdate(parseUpdate(sparql_text))
        for request in algebra.algebra:
            _walk(request, refs, set())
        return refs
    except Exception:
        pass

    return _fallback_graph_refs(sparql_text)


# --------------------------------------------------------------------------
# RDF 1.2 preflight fallback -- rdflib 6.0.0's SPARQL grammar predates RDF
# 1.2 entirely and throws on both current embedded-triple notations:
#   * triple terms      <<( s p o )>> / the older informal <<s p o>>
#   * annotation syntax  s p o {| ex:prop val |}
# Rather than let every query using either construct fall through to a
# blanket deny, this does a narrow, purely TEXTUAL scan for the same
# graph-referencing keywords the algebra walk above looks for -- but only
# once we already know the real parser failed, and only when the failure
# looks explained by RDF 1.2 syntax specifically. Any other parse failure
# (a genuine typo, a malformed query, anything else) still falls through
# to the original fail-closed ``None`` from extract_graph_refs, unchanged.
#
# Deliberately conservative in its own right, independent of the above: it
# only trusts a graph reference written as a full IRIREF (``<...>``). A
# PrefixedName graph reference (``GRAPH ex:g1``) would need PREFIX
# declarations resolved to be read correctly, and a wrong resolution here
# is a silent *under*-report of what a query touches -- worse than denying
# a query that would otherwise have been fine. So the moment a non-IRIREF
# token shows up where a graph reference is expected, this bails to
# ``None`` rather than guess, same as the algebra path does on any other
# unresolvable construct.
#
# This does not make the bridge understand RDF 1.2 -- Fuseki already does,
# natively, on the storage/query side, which is why writes and hand-run
# queries against Fuseki directly are unaffected by any of this. This only
# unblocks the preflight ACL check in front of sparql_select/sparql_update
# so those bridge-level calls stop being denied outright.

_GRAPH_REF_KEYWORDS = re.compile(
    r"""
    \b(?:
        GRAPH
      | FROM\s+NAMED
      | FROM
      | SERVICE(?:\s+SILENT)?
      | WITH
      | USING\s+NAMED
      | USING
    )\b
    \s*
    (?:
        <(?P<iri>[^<>\s]+)>          # a full IRIREF -- the only form trusted
      | (?P<bad>\S+)                 # anything else (PrefixedName, etc.)
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _fallback_graph_refs(sparql_text: str) -> set[str] | None:
    """Textual best-effort graph-reference scan. Only ever called after the
    real parser has already failed -- see the module comment above for the
    full reasoning and the deliberate limits (RDF-1.2-triggered only,
    IRIREF-only, no PrefixedName resolution)."""
    if "<<" not in sparql_text and "{|" not in sparql_text:
        # Not an RDF 1.2 case -- don't second-guess the real parser's
        # failure by assuming this fallback would have done any better.
        return None

    refs: set[str] = set()
    for match in _GRAPH_REF_KEYWORDS.finditer(sparql_text):
        if match.group("bad") is not None:
            return None  # can't safely resolve a PrefixedName here -- deny
        refs.add(match.group("iri"))
    return refs


async def authorize_query(
    query_fn: QueryFn,
    holons_graph: str,
    *,
    person: str | None,
    sparql_text: str,
    persona_of_graph: Callable[[str], str | None],
) -> AclDecision:
    """Gate an arbitrary SPARQL SELECT/CONSTRUCT/UPDATE by every graph it
    actually touches, not just a declared ``graph`` field.

    ``persona_of_graph`` maps a graph IRI to the Persona IRI that owns it
    (or None for graphs outside the persona/user scheme -- org ground truth,
    for instance, which this function treats as always readable, matching
    the DataBook's statement that ground truth isn't role-gated). Fail
    closed on anything this function can't positively clear.
    """
    if person is None:
        return AclDecision(False, "unresolved identity")

    refs = extract_graph_refs(sparql_text)
    if refs is None:
        return AclDecision(False, "query could not be parsed for graph references; denied, not assumed safe", person)

    for graph_iri in refs:
        persona = persona_of_graph(graph_iri)
        if persona is None:
            continue  # org ground truth or a graph outside the persona scheme
        decision = await check_read(query_fn, holons_graph, person=person, persona=persona)
        if not decision.allowed:
            return AclDecision(
                False, f"no ReadGrant covering {graph_iri} (persona {persona})", person
            )
    return AclDecision(True, "every referenced graph cleared", person)


# --------------------------------------------------------------------------
# Team-scoped visibility (the in-query half -- see the DataBook: this
# cannot be a pre-query gate, because it's metadata on individual facts
# inside a shared graph, not a separate partition)
# --------------------------------------------------------------------------

def enforce(decision: AclDecision) -> AclDecision:
    """Convenience for route handlers that would rather raise than branch:
    ``enforce(await check_read(...))`` raises :class:`Denied` on refusal,
    otherwise returns the decision so a caller can still log
    ``decision.matched_grant``."""
    if not decision.allowed:
        raise Denied(decision.reason)
    return decision


def team_visibility_filter(teams: frozenset[str], *, subject_var: str = "?s") -> str:
    """A FILTER fragment to inject into any query reading a graph that may
    carry holon:visibleToTeam. Excludes a subject only if it HAS a team
    restriction and none of the caller's teams satisfy it; a subject with no
    restriction at all is always included, matching "absence means
    unscoped" in the DataBook.
    """
    if not teams:
        values = ""
    else:
        team_list = " ".join(f"<{t}>" for t in teams)
        values = f"VALUES ?__callerTeam {{ {team_list} }}"
    return f"""
    FILTER NOT EXISTS {{
      {subject_var} <{HOLON}visibleToTeam> ?__anyTeam .
      FILTER NOT EXISTS {{
        {values}
        FILTER (?__anyTeam = ?__callerTeam)
      }}
    }}
    """
