"""Access control: resolve who is asking, decide what they may do.

Implements the model documented in the ACL architecture design record
against whatever is actually
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

from . import audit, delegation

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

    CHANGED 2026-09-18: a Person carrying ``holon:status "inactive"`` (see
    ``routes/identity_admin.py``'s ``deactivate_person``) resolves to
    ``None`` here -- treated identically to an external_id that never
    matched anything. This is the one place that has to change to make
    deactivation actually take effect: ``require_animus`` in ``deps.py``
    builds every caller's ``Animus`` through this function, so a
    deactivated Person gets a 401 on their very next request, on any
    route, not only admin-gated ones. ``deactivate_person`` also strips
    every ``holon:hasRole``/``holon:grantsWrite``/``holon:grantsReplace``
    edge the Person held, but that alone would leave anything that reads
    the graph directly -- rather than going through identity resolution --
    still seeing a Person who merely lost their Roles, not one who is
    locked out. This check is the one that's actually load-bearing;
    stripping the edges too is belt-and-suspenders.
    """
    query = f"""
    PREFIX holon: <{HOLON}>
    PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
    SELECT ?person ?label ?status WHERE {{
      GRAPH <{holons_graph}> {{
        ?person holon:hasExternalIdentity ?identity ;
                rdfs:label ?label .
        ?identity a holon:{external_id_type} ;
                  holon:identifier {_literal(external_id)} .
        OPTIONAL {{ ?person holon:status ?status . }}
      }}
    }} LIMIT 1
    """
    rows = _bindings(await _run(query_fn, query))
    if not rows:
        return None
    row = rows[0]
    if row.get("status", {}).get("value") == "inactive":
        return None
    return row["person"]["value"], row["label"]["value"]


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


def _owner_role(holons_graph: str) -> str:
    """The reserved owner Role IRI for this dataset, derived exactly as
    :func:`_admin_role` derives the admin Role.

    Owner sits above admin: an owner is an admin (every admin check passes
    for one) and is additionally the only standing that may grant or revoke
    ``admin`` or ``owner`` once the dataset has an owner -- see
    ``routes/identity_admin.py``. It is a hierarchy of one extra tier, not a
    second bypass: nothing in this module treats owner differently from admin.
    """
    suffix = ":holons"
    if not holons_graph.endswith(suffix):
        raise ValueError(f"expected holons_graph to end with {suffix!r}, got {holons_graph!r}")
    return holons_graph[: -len(suffix)] + ":role:owner"


def _scratch_area(holons_graph: str) -> str:
    """The reserved scratch-area IRI for this dataset, derived the same way
    ``_admin_role`` derives the admin role: swap the trailing ``:holons``
    for the resource's own path segment -- ``urn:bridgerton:holons`` ->
    ``urn:bridgerton:scratch``. Individual scratch graphs are named
    ``{scratch_area}:{token}`` (a UUID, typically); this resource is what a
    namespace-level ``holon:grantsWrite``/``holon:grantsReplace`` targets,
    never a data graph in its own right.

    Added 2026-09-23 on Kurt's framing: a scratch workspace needs to be
    fast and low-ceremony -- generated for a calculation or simulation,
    used, and generally cleared when done -- but a freshly-minted scratch
    graph, named from a UUID generated at write time, can never have a
    pre-existing per-graph grant naming it. Left unchanged, the ordinary
    check_write/check_replace per-target lookup would deny every non-admin
    write to a brand-new scratch graph outright, which is worse than
    ordinary graphs, not equally gated. One grant on this namespace
    resource, checked instead of one on the graph itself whenever the
    target falls under it (see ``is_scratch_graph``), is what makes
    scratch usable without abandoning "no default-permit" -- still a real
    grant, still fail-closed absent one, just resolved once at the
    namespace level rather than per minted graph.
    """
    suffix = ":holons"
    if not holons_graph.endswith(suffix):
        raise ValueError(f"expected holons_graph to end with {suffix!r}, got {holons_graph!r}")
    return holons_graph[: -len(suffix)] + ":scratch"


def is_scratch_graph(target: str, holons_graph: str) -> bool:
    """Whether ``target`` falls under this dataset's scratch namespace --
    ``{scratch_area}:{token}``, e.g. ``urn:bridgerton:scratch:<uuid>``.

    The scratch-area resource itself (with no trailing token) does not
    count as a scratch graph -- it is a grant target for check_write/
    check_replace to resolve against, never a data graph written to
    directly.
    """
    return target.startswith(_scratch_area(holons_graph) + ":")


async def is_admin(query_fn: QueryFn, holons_graph: str, *, person: str) -> bool:
    """Whether ``person`` holds the reserved admin Role for this dataset --
    or the owner Role, which implies it, so no call site had to change when
    the owner tier was added."""
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{ GRAPH <{holons_graph}> {{
        <{person}> holon:hasRole ?role .
        FILTER (?role IN (<{_admin_role(holons_graph)}>, <{_owner_role(holons_graph)}>))
    }} }}
    """
    result = await _run(query_fn, query)
    return bool(result.get("boolean"))


async def is_owner(query_fn: QueryFn, holons_graph: str, *, person: str) -> bool:
    """Whether ``person`` holds the reserved owner Role for this dataset."""
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{ GRAPH <{holons_graph}> {{ <{person}> holon:hasRole <{_owner_role(holons_graph)}> . }} }}
    """
    result = await _run(query_fn, query)
    return bool(result.get("boolean"))


async def owner_exists(query_fn: QueryFn, holons_graph: str) -> bool:
    """Whether anyone at all holds the owner Role on this dataset."""
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{ GRAPH <{holons_graph}> {{ ?anyone holon:hasRole <{_owner_role(holons_graph)}> . }} }}
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
# this module below the admin bypass, with one narrow, deliberate
# carve-out: check_write/check_replace resolve a scratch-namespaced target
# (see is_scratch_graph) to a single namespace-level grant target rather
# than the graph itself, so one grant on a dataset's scratch area covers
# every scratch graph a person creates under it, present or future. Still a
# real grant, still fail-closed absent one -- see _scratch_area's own
# comment for why a per-graph grant is structurally impossible for
# something minted fresh at write time.
#
# CHANGED 2026-09-29: a second, equally explicit path now exists for reads
# and additive writes -- a stored, time-boxed, revocable holon:Delegation
# naming the exact graph (see delegation.py). It is consulted only by
# check_write (permission "write") and authorize_query (permission "read").
# It is deliberately NOT consulted by check_replace, check_read, or
# check_invoke: no delegation reaches wholesale replace, and a delegation
# names graphs, never a Persona-wide ReadGrant or an invocable.

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
    Otherwise only an explicit holon:grantsWrite allows it: naming this
    exact (person, target) pair for an ordinary graph, or naming
    (person, scratch-area) when ``is_scratch_graph`` recognises ``target``
    as falling under this dataset's scratch namespace -- see
    ``_scratch_area`` for why a scratch graph, minted fresh at write time,
    could never have a grant naming it specifically. This governs
    additive/merge writes only -- see check_replace for wholesale graph
    overwrite, which is deliberately a stricter, independent grant.

    CHANGED 2026-09-29: an active, unrevoked holon:Delegation with the
    "write" permission that lists ``target`` among its covered graphs also
    allows the write (see ``delegation.find_delegation``). Not applicable
    to a scratch target: a delegation covers a delegator's own
    persona-user graphs, never the scratch namespace."""
    # The audit graph is written by the bridge itself and by nobody else,
    # admins included -- so this comes before the admin bypass.
    if audit.is_audit_graph(target, holons_graph):
        return AclDecision(False, "the audit graph is written only by the bridge itself", person)
    if await is_admin(query_fn, holons_graph, person=person):
        return AclDecision(True, "admin role bypass", person)
    grant_target = (
        _scratch_area(holons_graph) if is_scratch_graph(target, holons_graph) else target
    )
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{
      GRAPH <{holons_graph}> {{ <{grant_target}> holon:grantsWrite <{person}> . }}
    }}
    """
    result = await _run(query_fn, query)
    if result.get("boolean"):
        reason = (
            "namespace-level grantsWrite on the scratch area"
            if grant_target != target
            else "explicit grantsWrite"
        )
        return AclDecision(True, reason, person, grant_target)
    if grant_target == target:
        delegated = await delegation.find_delegation(
            query_fn, holons_graph, delegate=person, permission="write", target=target
        )
        if delegated:
            return AclDecision(
                True,
                f"delegated write from {delegated[0].delegator}",
                person,
                delegated[0].delegation,
            )
    if grant_target != target:
        return AclDecision(
            False, "writes are never role-based; no grantsWrite on the scratch area", person
        )
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
    confers nothing here, by design.

    Same scratch-area carve-out as check_write (see
    ``_scratch_area``/``is_scratch_graph``): a namespace-level
    grantsReplace on the scratch area covers every scratch graph, since a
    freshly-minted one can never hold a grant naming it specifically --
    still fail-closed, still a real grant, just resolved to the namespace
    for scratch targets. As of this writing nothing in any dataset holds
    grantsReplace at all -- not even on a scratch area -- which is the
    intended starting state (appends available by default, replace
    available to no one until someone deliberately decides otherwise), not
    an oversight to backfill. Same admin bypass, same fail-closed default
    as check_write.

    CHANGED 2026-09-29: a holon:Delegation is deliberately NOT consulted
    here, and cannot be made to be -- delegation.PERMISSIONS has no
    "replace". Holding a write delegation, like holding grantsWrite,
    confers nothing on this path."""
    if audit.is_audit_graph(target, holons_graph):
        return AclDecision(False, "the audit graph is written only by the bridge itself", person)
    if await is_admin(query_fn, holons_graph, person=person):
        return AclDecision(True, "admin role bypass", person)
    grant_target = (
        _scratch_area(holons_graph) if is_scratch_graph(target, holons_graph) else target
    )
    query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{
      GRAPH <{holons_graph}> {{ <{grant_target}> holon:grantsReplace <{person}> . }}
    }}
    """
    result = await _run(query_fn, query)
    if result.get("boolean"):
        reason = (
            "namespace-level grantsReplace on the scratch area"
            if grant_target != target
            else "explicit grantsReplace"
        )
        return AclDecision(True, reason, person, grant_target)
    if grant_target != target:
        return AclDecision(
            False,
            "replace is stricter than write; no grantsReplace on the scratch area "
            "(holding grantsWrite there does not imply it)",
            person,
        )
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

@dataclass(frozen=True)
class GraphAnalysis:
    """Every way a SPARQL query or update can reach a graph, as seen in the
    parsed algebra.

    ``refs`` are concrete graphs named anywhere (GRAPH <g>, FROM/FROM NAMED,
    WITH, USING/USING NAMED, INSERT/DELETE quads, LOAD ... INTO). ``replace_refs``
    are graphs hit by a *wholesale* operation (DROP, CLEAR, CREATE, and both
    ends of COPY/MOVE/ADD) -- the same shape ``check_replace`` exists for.
    ``dynamic`` means some graph position is not a fixed IRI (``GRAPH ?g``
    however ``?g`` gets bound -- VALUES, BIND, a subquery -- ``SERVICE ?v``, or
    ``ALL``/``NAMED``): what it reaches cannot be known before it runs, so it
    cannot be authorised. ``default_write`` means an update writes or clears the
    default graph. ``admin_only_ops`` names operations that are admin-only
    outright (LOAD fetches a remote URL).
    """

    refs: frozenset[str] = frozenset()
    replace_refs: frozenset[str] = frozenset()
    dynamic: bool = False
    default_write: bool = False
    admin_only_ops: tuple[str, ...] = ()

    @property
    def all_refs(self) -> frozenset[str]:
        return self.refs | self.replace_refs


class _Collector:
    def __init__(self) -> None:
        self.refs: set[str] = set()
        self.replace_refs: set[str] = set()
        self.dynamic = False
        self.default_write = False
        self.admin_ops: set[str] = set()

    def term(self, value: object, *, wholesale: bool = False) -> None:
        """Classify one graph position. Anything that is not a fixed IRI or
        the default graph is dynamic -- fail closed."""
        from rdflib.term import URIRef

        if isinstance(value, URIRef):
            (self.replace_refs if wholesale else self.refs).add(str(value))
        elif value == "DEFAULT":
            self.default_write = True
        else:  # a Variable, "ALL", "NAMED", or anything we do not recognise
            self.dynamic = True


_WHOLESALE_SINGLE = frozenset({"Drop", "Clear", "Create"})
_WHOLESALE_PAIR = frozenset({"Copy", "Move", "Add"})

#: ``GraphGraphPattern`` is the parse-tree name for ``GRAPH <x> { ... }``.
#: rdflib leaves the pattern inside FILTER EXISTS / NOT EXISTS untranslated, so
#: a graph position there is never a ``Graph`` algebra node.
_GRAPH_PATTERNS = frozenset({"Graph", "GraphGraphPattern", "ServiceGraphPattern"})


def _field(node: object, key: str) -> object:
    """``node[key]`` if it is there, else ``None``.

    Not ``node.get(key)``: rdflib's ``CompValue.get`` returns the *key name
    itself* for a missing key, which reads as a plausible value and, here,
    as a graph position that is not an IRI.
    """
    try:
        return node[key] if key in node else None  # type: ignore[operator, index]
    except Exception:
        return None


def _collect(node: object, acc: _Collector, seen: set[int]) -> None:
    if id(node) in seen:
        return
    seen.add(id(node))
    name = getattr(node, "name", None)

    if name in _GRAPH_PATTERNS:
        acc.term(_field(node, "term"))
    elif name in ("DatasetClause", "UsingClause"):
        for key in ("default", "named"):
            if _field(node, key) is not None:
                acc.term(_field(node, key))
    elif name in _WHOLESALE_SINGLE:
        acc.term(_field(node, "graphiri"), wholesale=True)
    elif name in _WHOLESALE_PAIR:
        for graph in _field(node, "graph") or []:  # type: ignore[union-attr]
            acc.term(graph, wholesale=True)
    elif name == "Load":
        acc.admin_ops.add("LOAD")
        target = _field(node, "graphiri")
        if target is None:
            acc.default_write = True
        else:
            acc.term(target)
    elif name == "Modify":
        with_graph = _field(node, "withClause")
        if with_graph is not None:
            acc.term(with_graph)
        for part in ("delete", "insert"):
            clause = _field(node, part)
            if clause is not None and _field(clause, "triples") and with_graph is None:
                acc.default_write = True
    elif name in ("InsertData", "DeleteData", "DeleteWhere"):
        if _field(node, "triples"):
            acc.default_write = True

    if hasattr(node, "keys") and callable(node.keys):
        for key in list(node.keys()):
            value = node[key]
            if key == "quads" and isinstance(value, dict):
                for graph_ref in value.keys():
                    acc.term(graph_ref)
                continue
            _collect(value, acc, seen)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect(value, acc, seen)


def analyse_graph_refs(sparql_text: str) -> GraphAnalysis | None:
    """Parse ``sparql_text`` (query or update) and report every way it reaches
    a graph -- see :class:`GraphAnalysis`.

    Works against the SPARQL *algebra* (``translateQuery``/``translateUpdate``),
    which is what rdflib itself executes, so a reference this misses is one
    the engine would not have executed either.

    Returns ``None`` if the text can't be parsed at all -- callers MUST treat
    that as "deny", never as "nothing to check". A query this module cannot
    read is not evidence it's safe.

    CHANGED 2026-09-30: this used to be ``extract_graph_refs`` alone, which saw
    only ``GRAPH <iri>`` clauses, SERVICE targets and INSERT/DELETE quads. It
    reported *no graphs at all* for ``GRAPH ?g`` (however ?g was bound),
    FROM/FROM NAMED, WITH, USING, DROP/CLEAR/CREATE/COPY/MOVE/ADD and LOAD --
    and an empty result is exactly what makes a caller conclude "nothing to
    check". A live test wrote to a graph the caller had no grant on through
    ``INSERT { GRAPH ?g ... } WHERE { VALUES ?g { <g> } }``; ``DROP GRAPH <g>``
    parsed as referencing nothing.
    """
    from rdflib.plugins.sparql.algebra import translateQuery, translateUpdate
    from rdflib.plugins.sparql.parser import parseQuery, parseUpdate

    def _finish(acc: _Collector) -> GraphAnalysis:
        return GraphAnalysis(
            refs=frozenset(acc.refs),
            replace_refs=frozenset(acc.replace_refs),
            dynamic=acc.dynamic,
            default_write=acc.default_write,
            admin_only_ops=tuple(sorted(acc.admin_ops)),
        )

    try:
        algebra = translateQuery(parseQuery(sparql_text))
        acc = _Collector()
        _collect(algebra.algebra, acc, set())
        return _finish(acc)
    except Exception:
        pass

    try:
        algebra = translateUpdate(parseUpdate(sparql_text))
        acc = _Collector()
        for request in algebra.algebra:
            _collect(request, acc, set())
        return _finish(acc)
    except Exception:
        pass

    fallback = _fallback_graph_refs(sparql_text)
    return None if fallback is None else GraphAnalysis(refs=frozenset(fallback))


def extract_graph_refs(sparql_text: str) -> set[str] | None:
    """Every concrete graph IRI this query or update names, or ``None`` if it
    can't be parsed (callers MUST treat that as "deny").

    Kept for callers that only need the IRIs. It now reports the full set --
    including FROM/FROM NAMED, WITH, USING and the targets of DROP/CLEAR/COPY/
    MOVE/ADD/LOAD -- but it cannot express a graph position that is *not* a
    fixed IRI (``GRAPH ?g``); use :func:`analyse_graph_refs` for that.
    """
    analysis = analyse_graph_refs(sparql_text)
    return None if analysis is None else set(analysis.all_refs)


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


_PRIVATE_SPACE = re.compile(r":persona:[^:]+:user:([^:]+):")


def private_space_owner(graph_iri: str) -> str | None:
    """The slug of the person whose private space ``graph_iri`` is
    (``urn:...:persona:{persona}:user:{slug}:{role}``), or ``None`` for a graph
    outside that scheme and for the shared ``public`` user, which belongs to
    nobody in particular."""
    match = _PRIVATE_SPACE.search(graph_iri)
    if match is None or match.group(1) == "public":
        return None
    return match.group(1)


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

    CHANGED 2026-09-29: a graph the caller lacks a Persona ReadGrant for is
    still cleared if an active holon:Delegation with the "read" permission
    names that exact graph (``delegation.find_delegation``). Every
    referenced graph must be cleared one way or the other; a single
    uncovered graph still denies the whole query.

    CHANGED 2026-09-30: a query with a graph position that is not a fixed IRI
    (``GRAPH ?g``, ``SERVICE ?v``) is refused to non-admins -- there is nothing
    to check the ReadGrant against, and clearing it as "references no graph"
    let any identified caller read every graph. FROM/FROM NAMED and USING
    targets are now checked like any other reference. Admins are unaffected.

    CHANGED 2026-09-30: a persona-wide ``ReadGrant`` no longer opens another
    person's *private* space. The grant is scoped to a persona, so before this
    it opened every person's private area beneath that persona to any holder
    -- a coarser rule than "private to you". Another person's private space
    (see :func:`private_space_owner`) now opens only to an admin or to a
    holder of an active read delegation naming that graph; a ReadGrant still
    opens the caller's own private space and the persona's shared ``public``
    space, as before.
    """
    if person is None:
        return AclDecision(False, "unresolved identity")

    analysis = analyse_graph_refs(sparql_text)
    if analysis is None:
        return AclDecision(False, "query could not be parsed for graph references; denied, not assumed safe", person)

    caller_slug = person.rsplit(":", 1)[-1]

    if analysis.dynamic and not await is_admin(query_fn, holons_graph, person=person):
        return AclDecision(
            False,
            "query reaches graphs it does not name (GRAPH ?var, SERVICE ?var, ALL/NAMED); "
            "name each graph explicitly, or ask an admin",
            person,
        )

    for graph_iri in sorted(analysis.all_refs):
        if audit.is_audit_graph(graph_iri, holons_graph):
            # Not a persona graph, so it would otherwise fall through as
            # "org ground truth, always readable". Admins only.
            if await is_admin(query_fn, holons_graph, person=person):
                continue
            return AclDecision(False, "the audit graph is readable by admins only", person)
        persona = persona_of_graph(graph_iri)
        if persona is None:
            continue  # org ground truth or a graph outside the persona scheme
        owner = private_space_owner(graph_iri)
        if owner is not None and owner != caller_slug:
            if await is_admin(query_fn, holons_graph, person=person):
                continue
            if await delegation.find_delegation(
                query_fn, holons_graph, delegate=person, permission="read", target=graph_iri
            ):
                continue
            return AclDecision(
                False,
                f"no ReadGrant covering {graph_iri} (persona {persona}): it is another "
                "person's private space; only that person, an administrator, or someone "
                "they have lent it to may read it",
                person,
            )
        decision = await check_read(query_fn, holons_graph, person=person, persona=persona)
        if not decision.allowed:
            if await delegation.find_delegation(
                query_fn,
                holons_graph,
                delegate=person,
                permission="read",
                target=graph_iri,
            ):
                continue
            return AclDecision(
                False, f"no ReadGrant covering {graph_iri} (persona {persona})", person
            )
    return AclDecision(True, "every referenced graph cleared", person)


async def authorize_update(
    query_fn: QueryFn,
    holons_graph: str,
    *,
    person: str | None,
    sparql_text: str,
) -> AclDecision:
    """Gate a SPARQL UPDATE by every graph it can reach, before it runs.

    Writes are never role-based (see :func:`check_write`), so each concrete
    graph the update names needs an explicit ``holon:grantsWrite``; each graph
    hit by a *wholesale* operation (DROP, CLEAR, CREATE, COPY, MOVE, ADD) needs
    the stricter ``holon:grantsReplace``, the same grant ``DELETE /graph`` and
    ``/graph-op`` already require for the same reason. Then, failing closed:

    - an update it cannot parse is refused;
    - an update with a graph position that is not a fixed IRI (``GRAPH ?g``,
      ``DROP ALL``/``NAMED``, ...) is refused to **everyone, admins included**.
      The audit graph's protection is a per-graph check that cannot see through
      such an update, so allowing it for admins would let ``DROP ALL`` erase the
      log. Name each graph;
    - writing or clearing the default graph, and ``LOAD`` (which makes the
      store fetch a URL), are admin-only.
    """
    if person is None:
        return AclDecision(False, "unresolved identity")

    analysis = analyse_graph_refs(sparql_text)
    if analysis is None:
        return AclDecision(
            False,
            "update could not be parsed for graph references; denied, not assumed safe",
            person,
        )

    if analysis.dynamic:
        return AclDecision(
            False,
            "update reaches graphs it does not name (GRAPH ?var, DROP/CLEAR ALL or NAMED, ...); "
            "name each graph explicitly",
            person,
        )

    if analysis.default_write or analysis.admin_only_ops:
        if not await is_admin(query_fn, holons_graph, person=person):
            what = "LOAD" if analysis.admin_only_ops else "writing the default graph"
            return AclDecision(False, f"{what} is admin-only", person)

    for graph_iri in sorted(analysis.replace_refs):
        decision = await check_replace(query_fn, holons_graph, person=person, target=graph_iri)
        if not decision.allowed:
            return AclDecision(False, f"{decision.reason} (graph: {graph_iri})", person)
    for graph_iri in sorted(analysis.refs):
        decision = await check_write(query_fn, holons_graph, person=person, target=graph_iri)
        if not decision.allowed:
            return AclDecision(False, f"{decision.reason} (graph: {graph_iri})", person)
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
