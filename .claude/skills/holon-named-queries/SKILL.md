---
name: holon-named-queries
description: "Reference skill for creating, registering, and running named queries (and designing named updates) across the holon/HolonBridge stack. Load this whenever Kurt asks how to create, register, or invoke a \"named query\" or \"named update\"; when working with the HolonBridge `/named-query`, `/named-queries`, or `/query` REST endpoints; the `hb:NamedQuery` or `hquery:NamedQuery` vocabularies; the \"HGA Named Query Specification\"; `{{placeholder}}` substitution vs SPARQL `VALUES`-clause parameter binding; author-supplied SHACL parameter shapes (`sh:in`, `sh:pattern`, `sh:minInclusive`/`sh:maxInclusive`); the `register_named_query` / `list_named_queries` / `run_named_query` / `delete_named_query` MCP tools; or DataBook `databook:param` / parameterised query blocks. Also trigger on: \"named query\", \"named update\", \"query registry\", \"urn:{dataset}:named-queries\", \"queryId\", \"parameterised query\". Prefer this skill over ad-hoc SPARQL-in-chat whenever the query is meant to be reusable rather than one-off."
---

# Holon Named Queries & Updates Skill

"Named query" means three different, related things in this stack. Getting
the wrong one is the most common failure mode here — check the table first.

**Canonical implementation: `kurtcagle/holon-bridge-python`** (FastAPI/FastMCP,
Python). `kurtcagle/holon-bridge` (the original Node/Express implementation)
is **deprecated** — frozen since 2026-07-25, not receiving new features. A
Node revival is possible in principle but isn't planned as of 2026-09-05
(Kurt's call). Every code reference, file path, and endpoint shape below is
the Python repo unless stated otherwise — an earlier draft of this skill
conflated the two and got the Named Updates section wrong as a result (see
that section for what actually happened).

---

## Quick orientation

| You want... | Layer | Section |
|---|---|---|
| A reusable query that travels inside one `.databook.md` file | DataBook-scoped | [DataBook-Scoped Queries](#databook-scoped-queries) |
| A query invocable by ID from any client, server-resident, dataset-wide | Bridge registry | [Bridge-Registry Queries](#bridge-registry-queries) |
| A query scoped to one holon, referenced by `add_projection` | Holon-scoped | [Holon-Scoped Queries](#holon-scoped-queries) |
| To register/run one via an MCP tool call | [MCP Tool Layer](#mcp-tool-layer) |
| A reusable **SPARQL UPDATE** template | Not a separate layer — see below | [Named Updates](#named-updates--not-a-separate-mechanism-by-design) |

All three layers can coexist for the same logical query — e.g. author it as
a DataBook block during development, then register the resolved SPARQL into
the bridge registry once it's stable enough to be called by ID.

---

## DataBook-Scoped Queries

Covered fully in the `databook` skill's "Parameterised Queries" section —
this is the summary for cross-reference.

A `databook:id`-tagged `sparql` block, with `databook:param` declaring
substitutable variables. The `VALUES` clause is both the substitution
target and the default:

````markdown
<!-- databook:id: select-active-sensors -->
<!-- databook:param: siteId type=IRI default=ex:site-01 -->
```sparql
PREFIX ex: <https://example.org/>
SELECT ?sensor ?reading WHERE {
    VALUES ?site { ex:site-01 }
    ?sensor ex:site ?site ; ex:reading ?reading .
}
```
````

Invoke with `databook sparql source.databook.md#select-active-sensors -d ds`.
**No server-side registry entry exists for this** — the file itself is the
registry. Portable and git-versioned, but only callable by clients that have
the file. Naming convention: prefix the block id with `select-`, `describe-`,
`construct-`, `validate-`, or `update-` per its query type (see the databook
skill for the full table).

---

## Bridge-Registry Queries

This is what "register a named query" means when talking to HolonBridge
directly. Queries live as RDF **inside the triplestore itself**, in the
named graph `urn:{dataset}:named-queries`, managed through HolonBridge's
REST API — not inside any file. Any client that can reach the bridge can
call one by id, with no knowledge of where it was authored.

**Any query form can be registered — read or update.** An earlier draft of
this skill said the registry was SELECT/ASK-only; that was true of the
deprecated Node implementation's binding pipeline, not of
`holon-bridge-python`. See [Named Updates](#named-updates--not-a-separate-mechanism-by-design)
for how form is actually decided (at run time, from the SPARQL itself).

### Two vocabularies — you must pick one

The bridge understands both and merges them at load time (`hb:` wins id
collisions, logged as a warning — a collision means a registry mistake, not
a normal occurrence).

#### `hb:` — the bridge's own scheme (simpler, use this by default)

Namespace: `https://w3id.org/holonbridge/`. Parameters are a JSON blob, not
first-class nodes. Substitution is **`{{placeholder}}`** text replacement —
raw string substitution, so keep placeholder names distinctive and never
build one from untrusted input.

```turtle
<https://w3id.org/holonbridge/query/select-risk-by-tier>
    a <https://w3id.org/holonbridge/NamedQuery> ;
    <http://purl.org/dc/terms/identifier>  "select-risk-by-tier" ;
    <http://purl.org/dc/terms/title>       "Risk-tier rollup" ;
    <http://purl.org/dc/terms/description> "Entities in a given risk tier" ;
    <https://w3id.org/holonbridge/targetGraph> <https://example.org/graphs/ggsc-risk> ;
    <https://w3id.org/holonbridge/parameters>  "[{\"name\":\"tier\",\"default\":\"high\"}]" ;
    <https://w3id.org/holonbridge/sparql>      """
        PREFIX ggsc: <https://example.org/ggsc/>
        SELECT ?entity ?label WHERE {
          ?entity ggsc:riskTier "{{tier}}" ; rdfs:label ?label .
        }""" .
```

You never hand-author this Turtle in practice — `POST /named-query` (or the
`register_named_query` MCP tool, see below) generates it from a plain JSON
body, confirmed live against `holonbridge/routes/named_queries.py`:

```json
POST /named-query
{
  "id": "select-risk-by-tier",
  "sparql": "PREFIX ggsc: <https://example.org/ggsc/>\nSELECT ?entity ?label WHERE {\n  ?entity ggsc:riskTier \"{{tier}}\" ; rdfs:label ?label .\n}",
  "label": "Risk-tier rollup",
  "description": "Entities in a given risk tier",
  "target_graph": "https://example.org/graphs/ggsc-risk",
  "params": [{ "name": "tier", "description": "Risk tier to filter on", "default": "high" }]
}
```

Delete with `DELETE /named-query/select-risk-by-tier` (or the
`delete_named_query` MCP tool) — see [Named Updates](#named-updates--not-a-separate-mechanism-by-design)
for the ACL each of these two operations actually requires. (The Node-era
`{ "id": ..., "delete": true }` body-flag pattern is gone — deletion is its
own HTTP verb now.)

#### `hquery:` — the HGA Named Query Specification v1.0 (richer, typed)

Namespace: `https://w3id.org/holon/named-query/`. The id is the query
resource's own IRI local name — there's no separate `dcterms:identifier`.
Parameters are first-class `hquery:Parameter` nodes carrying a name,
datatype, description, and required flag, and binding is a **`VALUES`
clause appended after the query** (never injected into the WHERE clause —
`ValuesClause` is legally the final production of a SPARQL query, so
appending can never corrupt a well-formed one):

```turtle
@prefix hquery: <https://w3id.org/holon/named-query/> .
@prefix holon:  <https://w3id.org/holon/> .
@prefix rdfs:   <http://www.w3.org/2000/01/rdf-schema#> .

<https://example.org/ggsc/named-queries/risk-by-tier>
    a hquery:NamedQuery ;
    rdfs:label        "Risk-tier rollup" ;
    holon:description  "Entities in a given risk tier" ;
    hquery:queryType   "SELECT" ;
    hquery:parameter  [ a hquery:Parameter ;
        hquery:parameterName        "tier" ;
        hquery:parameterDatatype    "xsd:string" ;
        hquery:parameterDescription "Risk tier to filter on" ;
        hquery:required             false
    ] ;
    hquery:sparql """
        PREFIX ggsc: <https://example.org/ggsc/>
        SELECT ?entity ?label ?tier WHERE {
          ?entity ggsc:riskTier ?tier ; rdfs:label ?label .
        }""" .
```

Calling with `{"tier": "high"}` appends `VALUES ?tier { "high" }` to the
query text — note the query itself must use `?tier` as an ordinary SPARQL
variable, **not** a `{{placeholder}}`; feeding `hquery:` parameters through
`{{...}}` substitution silently matches nothing and the query runs
unparameterised — a wrong answer with no error. This is why the loader
tags every query with which vocabulary it came from and dispatches binding
on that tag rather than guessing from content.

**CONFIRMED LIVE 2026-09-23 — a real, structural limitation, not a
phrasing problem.** A parameter used only inside a `BIND`/`FILTER` (never
the subject or object of a real triple pattern in the query body) can
never actually take effect via this VALUES-append mechanism, no matter how
the query is written. SPARQL joins the appended `VALUES` clause in only
*after* the entire `WHERE` clause — including every `BIND`/`FILTER` inside
it — has already evaluated, so the parameter is accepted, shows up in a
run's `"bound"` list, and silently does nothing. Reproduced against three
real parameters in the Bridgerton registry (`Person_age`'s `asOfDate`,
`Player_age-eligibility`'s `team` and `minimumAge`) that had been declared
and apparently working for weeks. A parameter genuinely bound by
pattern-matching (`person`, `player`, `season` — the subject of a real
triple pattern in the `WHERE` clause) is unaffected and works correctly;
this only bites a parameter used purely to compute or override something.
There is no `hquery:`-side fix — see "When to use which" below, and
`Person_age_as_of` for the working alternative.

**`hquery:` has no registration endpoint.** `POST /named-query` and
`register_named_query` are `hb:`-only, confirmed against
`routes/named_queries.py`. An `hquery:` entry still has to be hand-authored
as Turtle and pushed via `/graph/push` or a raw `sparql_update`.

**When to use which (revised 2026-09-23):** default to `hb:` for new
queries — not just because it's simpler to hand-author. Two concrete
reasons, both confirmed the hard way in the Bridgerton registry:

1. `{{placeholder}}` substitution happens as plain text replacement
   *before* the query is parsed, so it can affect anything — `FILTER`,
   `BIND`, a property path — with no risk of the VALUES-timing bug above,
   ever. `hquery:`'s VALUES-join can only ever correctly bind a parameter
   that's a genuine triple-pattern variable.
2. `hb:` has a smaller dynamically-assembled-syntax surface: a `VALUES`
   clause splices both a variable *name* and a value into generated
   SPARQL (guarded by a variable-name check, but still assembled at
   request time); `{{placeholder}}` substitution only ever fills a
   *value* slot the query author fixed at write time. Kurt's call
   2026-09-23: lean away from `VALUES`-based binding on exactly this
   basis, even though nothing currently exploits it — a caller can't
   control a query's own `Parameter.name`, only its supplied value, so
   this is a defense-in-depth call, not a response to a live gap.

Reach for `hquery:` specifically when a parameter's job is "match this
real entity, or omit for no constraint at all" — `person` (`Person_age`),
`player`/`season` (`Player_age-eligibility`) are the working examples.
That "omit for all" behaviour has no `hb:` equivalent: an unresolved
`{{placeholder}}` with no default fails the call outright rather than
binding nothing. Typed-parameter strictness is no longer a reason to
prefer `hquery:` either way — see "Author-supplied SHACL parameter
shapes" below; both vocabularies get the same `sh:in`/`sh:pattern`/
`sh:minInclusive`/`sh:maxInclusive` validation once a shape is registered
for a query.

**On defaults specifically (Kurt's convention, 2026-09-23):** avoid a
parameter default whose "natural" value has to be computed dynamically —
`NOW()` being the obvious case. `sh:defaultValue` and the flat `default`
field are both static RDF terms, not expressions, so "defaults to today"
can only ever mean a value fixed at authoring time and quietly going
stale, or — for `hquery:` — the VALUES-timing bug above, silently never
applying at all. Require the parameter explicitly instead and let the
caller compute it (client-side, or via a small wrapper query) rather than
giving the registry an implicit fallback that can fail without telling
anyone. `Person_age_as_of` requires both `person` and `asOfDate` for
exactly this reason; `Person_age` itself is being retrofitted the same
way for `asOfDate` (`person` stays optional — "omit to mean everyone" is
a different thing from a computed default; see above).

### Author-supplied SHACL parameter shapes (2026-09-23)

A query's parameter declarations no longer have to be the flat blank-node
style shown above, for either vocabulary. If a `sh:NodeShape` exists in
the same registry graph with `sh:targetNode` pointing at the query's own
IRI, its `sh:property` list is loaded **instead of** the flat declarations
for that query — wholesale, not merged. This is the extension point
`params.shacl_shape_for_query`'s own long-standing code comment had
already flagged as intended but unbuilt.

What it adds that the flat five-field `Parameter` shape (`name`,
`datatype`, `description`, `required`, `default`) has no room for: `sh:in`
(an enumerated set of legal values), `sh:pattern` (a regex the value's
text must match), `sh:minInclusive`/`sh:maxInclusive` (a numeric range).
All four are enforced in `render_term` as a real, local, pre-execution
check on every supplied value — rejected with a `parameter_error` 400,
same shape as an existing lexical-form failure — before it ever reaches a
SPARQL string.

Same `sh:path` convention the auto-generated `GET .../schema` shape
already uses: `sh:path <https://w3id.org/holonbridge/param/{name}>`, a
synthetic, not-a-real-predicate IRI whose local name IS the parameter
name — an author extends what would otherwise be derived, rather than
inventing a second convention. `sh:nodeKind sh:IRI` for an IRI-typed
parameter, `sh:datatype <...>` for a literal one, `sh:minCount 1` for
required, exactly mirroring the flat style's fields plus the four new
ones above.

**No registration endpoint for this either** — same situation as an
`hquery:` entry: hand-author the Turtle and push it via `/graph/push` or a
raw `sparql_update`, targeting `urn:{dataset}:named-queries`.

**Does not solve dynamic defaults.** `sh:defaultValue` is exactly as
static as the flat `default` field always was — see "On defaults
specifically" above. This mechanism is about *validating and
constraining* a supplied value, not about computing one.

Deliberately not implemented: `sh:class` (would need a live query against
the data graph to check instance membership — a fundamentally different,
heavier kind of check than everything else here, which is pure local
validation with no data access) and `sh:minLength`/`sh:maxLength` (trivial
to add, same shape as `sh:pattern`, just not built yet).

### A FILTER+OPTIONAL gotcha, and the verification habit it implies (2026-09-23)

Confirmed live and reproduced in isolation against this Jena/Fuseki stack —
a real engine quirk, not a misunderstanding of SPARQL semantics, and not
specific to either vocabulary above (this bites a query body regardless of
how its parameters are bound): a `FILTER` placed as a sibling to the
`OPTIONAL` block whose variables it references, inside the same group, can
silently match nothing — even when the identical boolean expression,
computed via `BIND` immediately after that same `OPTIONAL` and then
checked with a plain `FILTER(?thatVariable)`, evaluates correctly.
Reproduced with:

```sparql
{ ?s :hasBeginning ?begin .
  OPTIONAL { ?s :invalidatedAt ?end }
  FILTER( ?begin <= ?asOf && (!BOUND(?end) || ?end > ?asOf) )   # matches nothing
}
```
versus the fix:
```sparql
{ ?s :hasBeginning ?begin .
  OPTIONAL { ?s :invalidatedAt ?end }
}
BIND( (?begin <= ?asOf) && (!BOUND(?end) || ?end > ?asOf) AS ?valid )
FILTER(?valid)   # correctly matches
```

**Why this matters more than an ordinary bug:** the failure mode isn't an
error, and it isn't an empty result either (which would at least be
noticeable) — it can instead produce a confidently-wrong, fully-populated
result set. If the FILTER silently drops every row, any later variable
that depended on the OPTIONAL's bindings is left genuinely unbound, and if
that variable is then used as the subject of its own `OPTIONAL { GRAPH
{ ?thatVar ?p ?o } }` pattern elsewhere in the query (a common idiom for
"also fetch this entity's label if it resolved to one"), SPARQL treats the
unbound variable as a free wildcard — a cartesian match against every
matching subject in the graph, not a narrowing. First caught here only
because `Player_age-eligibility_as_of` (see Author-supplied SHACL
parameter shapes above for its sibling `Person_age_as_of`) came back with
hundreds of rows for what should have been a single-player,
single-season query: teams, coaches, stadiums, places, even the season
resource itself all showed up as candidate "evaluated teams", each
correctly reporting the same (correct) age and eligibility values — a
result that reads as plausible unless you actually count the rows.

**The resulting habit, worth generalising to every query in this
registry:** whenever a query combines a `FILTER` with an `OPTIONAL` in
the same group and the intent is to narrow results — to one row, to
"current" vs "historical", to a specific match — verify by actually
running it and counting the rows returned, not by reading the SPARQL and
confirming it looks correct. Reading the query does not catch this
failure mode; only a live run with a row count that matches what you
expect does. If a `FILTER` referencing an `OPTIONAL`-bound variable comes
back empty, or comes back with far more rows than expected, try isolating
the computation into a `BIND` immediately after the `OPTIONAL`, then
`FILTER` on that already-computed variable, before assuming the
underlying data is at fault.

### Execution

Confirmed live against `holonbridge/routes/named_queries.py`:

```
POST /named-query/{query_id}/run
{ "params": { "tier": "critical" }, "dry_run": false, "graph": null }
```

Omit `params` to run with declared defaults / no binding. Set
`"dry_run": true` to get the bound SPARQL back without executing it — useful
for checking substitution before it runs against real data. `graph`
overrides the query's own declared `target_graph` for that one call only.
A required `hquery:` parameter left unsupplied, or an `hb:` placeholder left
unresolved, is a 400 (`missing_parameters` / `parameter_error` respectively)
— the endpoint distinguishes the two failure shapes in its error body.

The response shape depends on what the bound SPARQL turns out to be, decided
at run time by `sparql_kind.classify()`: `{"results": [...]}` for a
SELECT/ASK, `{"turtle": "..."}` for a CONSTRUCT/DESCRIBE, or
`{"executed": true, "ok": true}` for an update. **There is no
`"format": "databook"` option on this endpoint** — if you need a
provenance-stamped DataBook wrapper around a result, that's a client-side
assembly step today, not a server parameter. (An earlier draft of this
skill claimed that option existed; it does not, in the current
`RunRequest` model — re-check `routes/named_queries.py` before assuming it's
been added.)

### Listing

`GET /named-queries` returns every registered query the caller's current
persona can reach (Toolset-gated — see the module docstring in
`routes/named_queries.py`), with id, label, description, target graph,
declared params, and vocabulary. Filter with `?vocabulary=hb|hquery` or
`?filter=<substring>`.

### Filesystem fallback (context bootstrap only)

A `named-queries` block in a context DataBook
(`context/{server}/{dataset}/03-named-queries.databook.md`) can also seed
queries — a JSON array of `{id, sparql, ...}` objects. **RDF is canonical;
filesystem only fills gaps for ids not already in RDF.** This exists for
bootstrapping a fresh dataset, not as an ongoing authoring path — prefer
`register_named_query` / `POST /named-query` for anything meant to persist.

---

## Holon-Scoped Queries

A *third* mechanism, distinct from the bridge-wide registry above: a
NamedQuery-shaped resource pushed into one specific **holon's own schema
graph** via the `add_schema` lifecycle verb, then referenced by IRI from
`add_projection`'s `query_iri` parameter to define that holon's projection
query. These are typically CONSTRUCT/DESCRIBE — the bridge-wide registry
above is not form-restricted in principle (see Named Updates), but a
projection query belongs to the holon it projects, not the dataset-wide
registry, so it lives here instead.

There is **no dedicated register verb** for this yet — it rides on
`add_schema`'s general "push Turtle (OWL/taxonomy/SHACL/named-query) into
the schema graph" contract. The exact predicate shape for a holon-scoped
query resource hasn't been pinned down as its own spec; treat it as an
`hb:NamedQuery` or `hquery:NamedQuery` resource (same shape as above), just
targeting the holon's schema graph instead of `urn:{dataset}:named-queries`,
until Kurt says otherwise. Confirm with him before assuming this shape in
generated code — it's inferred from `add_schema`'s docstring and
`add_projection`'s `query_iri` parameter, not from a written spec.

---

## MCP Tool Layer

`holonbridge_mcp/tools/named_queries.py` (Python — confirmed live, not a
planned addition). Seven tools:

| Tool | Wraps | Notes |
|---|---|---|
| `list_named_queries` | `GET /named-queries` | Filter by `vocabulary` (`hb`/`hquery`) or substring `filter` |
| `get_named_query` | `GET /named-query/{id}` | Full definition, including the SPARQL body |
| `get_named_query_schema` | `GET /named-query/{id}/schema` | SHACL shape generated fresh from the query's declared parameters — for a form/widget-generating client |
| `run_named_query` | `POST /named-query/{id}/run` | `query_id, params?, dry_run?, graph?` — see Execution above |
| `reload_named_queries` | `POST /named-queries/reload` | Discards the cached registry copy; deliberately ungated (reload reveals nothing, it just forces a re-read) |
| `register_named_query` | `POST /named-query` | `id, sparql, label?, description?, target_graph?, params?` — `hb:` only, see the ACL section under Named Updates below |
| `delete_named_query` | `DELETE /named-query/{id}` | `check_replace`-gated |

These talk to the bridge's own REST layer (`bridge_url` + the bridge's own
Bearer token in the MCP profile config) — not straight to Fuseki. That's the
distinction that matters operationally: `sparql_select`/`sparql_construct`/
`sparql_update` still bypass the registry entirely and talk to Fuseki
directly (`profile.fuseki_url`), same as always. Reach for the named-query
tools when the query is meant to be reusable/callable-by-id; reach for the
raw `sparql_*` tools for something genuinely one-off.

---

## Named Updates — not a separate mechanism, by design

**Confirmed directly against the live code** (`holonbridge/sparql_kind.py`,
`holonbridge/routes/named_queries.py`, `holonbridge_mcp/tools/named_queries.py`,
2026-09-05): there is no `hb:NamedUpdate` class, no
`urn:{dataset}:named-updates` graph, and no `updateId` concept anywhere in
`kurtcagle/holon-bridge-python`. **This is not a gap — an earlier draft of
this skill described it as one because it was checked against the
deprecated `kurtcagle/holon-bridge` (Node) repo instead of the Python
canonical one.** Don't repeat that mistake: if a future check needs to
confirm something about named updates again, check `holon-bridge-python`,
not `holon-bridge`.

**The actual design:** an `hb:NamedQuery` is agnostic about whether its
`hb:sparql` body is a read or a mutation. `sparql_kind.classify()` is a
lexical check (not a full parse — it exists to catch the obvious case
early; Jena remains the authority on validity) of the first real keyword
after the query's PREFIX/BASE prologue: SELECT/CONSTRUCT/ASK/DESCRIBE
classify as `read`; INSERT/DELETE/LOAD/CLEAR/DROP/CREATE/COPY/MOVE/ADD/WITH
classify as `update`. `run_named_query` calls `classify()` on the *bound*
SPARQL (after parameter substitution) at invocation time and dispatches to
`client.update()` for an update, or `client.select()`/`client.construct()`
for a read, purely based on `form()`. A registered query whose body happens
to be `DELETE WHERE {...}` already runs correctly today, with zero extra
plumbing — that *is* the named-update feature. It was never missing a
read/write distinction; it only lacked a way to register anything at all.

**What was actually missing, and was added 2026-09-02** (commit `c4311b8`):
the write side of the registry. Before that, every `hb:NamedQuery` — read or
update — had to be hand-authored as Turtle and pushed via `/graph/push` or a
raw `sparql_update`; no endpoint could register one itself. `POST
/named-query` (register/overwrite) and `DELETE /named-query/{id}` closed
that gap, along with the matching `register_named_query`/`delete_named_query`
MCP tools (see MCP Tool Layer above).

**Registration ACL is deliberately asymmetric**, because this is a
*definer's-rights* shape: once a query is registered, who can *run* it is
gated by Toolset reachability alone, not by the runner's own graph-write
grants — so registration is the only point that can check what the query
actually touches, and it has to check harder for that reason:

- Every registration needs `check_write` on the named-queries graph itself
  (installing anything at all is an ordinary content write).
- A body that `classify()` calls an update *additionally* needs
  `check_write` on every graph `extract_graph_refs` finds referenced in
  it — the same check `routes/sparql.py`'s `_authorize_write` applies to a
  raw `POST /sparql/update`. Without this, anyone who could write so much as
  a label into the named-queries graph could install a query that clears
  someone else's graph, runnable afterward by any Toolset member regardless
  of their own grants.
- Deletion needs `check_replace` instead of `check_write` — removing
  already-registered content is destructive, the same tier as
  `drop_pipeline`/`DELETE /graph`, not an ordinary append.
- `hb:` vocabulary only, for both registration and deletion. An `hquery:`
  entry (typed `Parameter` nodes, `VALUES`-clause binding) still needs
  hand-authored Turtle pushed directly via `/graph/push` — no registration
  endpoint exists for that vocabulary as of this writing.

**Practical upshot:** if you're building something that needs a reusable
mutation — a persona's classification write, a scheduled cleanup, an
audit-record append, a proposal submission — register it as an ordinary
`hb:NamedQuery` with an INSERT/DELETE body via `register_named_query`,
exactly like a read query. No second registry, no separate tool, no
"named update" terminology needed anywhere in the generated code — just an
`hb:NamedQuery` whose `sparql` happens to mutate.

---

## Related skills

- **databook** — DataBook-scoped parameterised queries; `databook:param`.
- **holonbridge** — the MCP tool layer these tools live in; existing
  `sparql_select`/`sparql_update`/`push_turtle` conventions.
- **holon-lifecycle** — `add_schema` (pushes holon-scoped named queries)
  and `add_projection` (`query_iri` consumes one). No standalone
  `holon-lifecycle` skill is loaded in this environment as of this writing
  — until one exists, `add_schema`/`add_projection` details live in the
  `holonbridge` and `sce` skills.
- **sce** — parent HGA architecture; where "projection" as a concept comes
  from.
