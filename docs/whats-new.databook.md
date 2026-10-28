---
id: https://w3id.org/databook/holon-bridge-python-whats-new
title: "What's New — holon-bridge-python"
type: databook
version: 1.0.16
created: 2026-09-27
updated: 2026-10-27
author:
  - name: Kurt Cagle
    iri: https://holongraph.com/people/kurt-cagle
    role: orchestrator
  - name: Claude Sonnet 5
    role: transformer
description: >
  Running log of substantive changes to holon-bridge-python, newest entry
  first. "Substantive" means a merged PR, a fixed bug, or a shipped
  feature someone would want to know about without reading the full commit
  history -- not every commit. Update both the prose entry below and the
  hb:ReleaseNote data block whenever something substantive ships; bump
  `version` and `updated` on every edit, `created` never changes.
process:
  transformer: "Claude Sonnet 5"
  transformer_type: llm
  transformer_iri: https://api.anthropic.com/v1/models/claude-sonnet-5
  timestamp: 2026-09-27T23:55:00Z
  agent:
    name: Claude Sonnet 5
    role: orchestrator
---

## How to update this file

1. Add a new dated section at the top of **Log**, below this section --
   newest first, same shape as the existing entries: a short heading, a
   bullet per substantive change, a link to the PR or commit for each.
2. Add a matching `hb:ReleaseNote` resource to the data block at the
   bottom for each bullet -- same date, same one-line label, a
   `dcterms:description` a sentence or two longer than the bullet, and
   `rdfs:seeAlso` pointing at the same PR/commit link.
3. Bump `version` (patch bump for a normal update) and `updated` in the
   frontmatter. `created` stays 2026-09-27 forever.
4. If this file gets long enough that scrolling past old entries is
   annoying, move everything older than a few months into
   `whats-new-archive.databook.md` and leave a one-line pointer here --
   not needed yet.

## Log

### 2026-10-27

- **A dataset can now be created through the bridge: `POST /datasets` /
  `create_dataset`.** Persistent (TDB2) or in-memory, via Fuseki's admin API;
  create only, with no drop route or client method (a test fails if one
  appears). Gated by a new `DATASET_CREATORS` list, separate from
  `DATASET_CLAIMANTS` and empty by default, and by the same *established
  identity* standard as claiming, so the legacy shared token and an open
  deployment can never create one. The caller becomes the new dataset's owner
  unless `claim` is false. Names are strict (lower-case, no dots, reserved
  names refused, existing names compared case-insensitively), `kind` is
  required rather than defaulted, and `MAX_DATASETS` (default 50) caps what the
  server will host. A successful creation is always audited. This reverses the
  earlier stance that dataset creation stays outside the bridge, now that
  per-caller identity reaches the REST layer. Not covered: dropping a dataset,
  and writing `bankScopedDatasets` to `config.json` -- declare a dataset there
  before its first write.
- **Every dataset the bridge creates has SHACL enabled.** Datasets made through
  Fuseki's admin form have no `/shacl` endpoint, which would leave a SHACL-gated
  bridge rejecting every write to them. Creation now uploads an assembler the
  bridge generates (standard endpoints plus `shacl`; never taken from a caller)
  and seeds the bundled identity/delegation/fluent shapes into the new
  dataset's shapes graph (`shapes=false` to skip). Persistent datasets are
  placed under the new `FUSEKI_DATABASES_DIR` (default: `databases` beside
  `FUSEKI_CONFIG_DIR`); without either, persistent creation is refused with 503.
  The response reports `shaclEnabled`, `shaclEndpoint` and `shapesSeeded`, read
  back from Fuseki rather than assumed. Verify on a live server that a new
  dataset lists `shacl` among its services.

### 2026-10-26

- **The MCP has a token of its own; the claim route audits a 409 as a refusal and returns 404 for an unknown dataset.**
  The MCP read only `BEARER_TOKEN`. Once the REST bridge moves to
  `PROXY_TOKENS`, the MCP (a proxy) cannot share the legacy token under
  one-token-one-role, and a token supplied under any other name was ignored:
  the MCP sent no `Authorization` header and every call failed with
  `401 missing bearer token`, with nothing to say why. It now reads
  `HOLONBRIDGE_MCP_TOKEN` first and falls back to `BEARER_TOKEN`, so existing
  single-token setups are unchanged. A blank value counts as unset, so a shared
  `.env` carrying `BEARER_TOKEN=` cannot hide the MCP's own token. Startup logs
  which variable supplied the token (never its value), or a warning when there
  is none. On `POST /admin/dataset/initialise`, a 409 is now audited as
  `denied` with its code as the reason, so it survives the `denials` audit
  level; a 409 on any other route is unchanged. The route also returns
  `404 unknown_dataset` when the backend does not host the dataset, instead of
  an opaque backend error. That check runs after eligibility, so an ineligible
  caller still gets the plain 403 and cannot use it to ask which datasets
  exist, and it is skipped when Fuseki's `/$/datasets` admin API is
  unavailable. `DATASET_CLAIMANTS`, the one-token-one-role startup rule, and
  the fact that a real environment variable beats `.env` are now documented in
  `.env.example` and the README. Checked live on a development bridge: the MCP
  authenticates with its own token, a claim on a claimed dataset gives the 409,
  and it is recorded as `denied`. The 404 was tested against stubs only
  ([#61](https://github.com/kurtcagle/holon-bridge-python/pull/61)).

### 2026-10-25

- **Fluent transitions now refuse a shapes graph with a graph-crossing constraint.**
  The guard from issue #2 (a `sh:select`/`sh:ask` that references `GRAPH` is
  refused, because Jena validates one flat graph and would silently conform)
  covered `push_turtle`, `/validate`, the pipeline stage and the scheduler, but
  not the fluent pre-commit check, which validates through the client directly.
  On a gated dataset such a constraint passed every fluent transition silently;
  `update_fluent` now raises a `FluentError` naming the shape. The README
  documents the flat form for a per-graph singleton constraint, which is what
  the original use case needed. Follow-on for constraints that genuinely span
  graphs: #60
  ([#59](https://github.com/kurtcagle/holon-bridge-python/pull/59)).

- **Owner role, and an in-band, allowlisted dataset claim.**
  A new reserved `owner` role sits above `admin`: owner implies admin (every
  admin check passes for one), and once a dataset has an owner **only an owner
  may grant, revoke or deactivate `admin` or `owner`** -- an admin keeps every
  other power. This is a behaviour change: before, any admin could create or
  remove any other admin. A dataset with no owner behaves as before, and
  `owner` is never grantable by a non-owner. The last owner cannot be removed;
  ownership moves by granting a second owner and revoking the first. The gate
  checks the real caller, so an admin cannot climb by `act_as`-ing the owner.
  New `POST /admin/dataset/initialise` (MCP tool `initialise_dataset`) makes
  the caller the first owner of a dataset with no owner or admin, and creates
  its Home if absent. Only an identity-bound token, or a trusted proxy's
  asserted identity, that is also listed in the new `DATASET_CLAIMANTS` setting
  may use it; a legacy shared token or an open deployment never can, and an
  empty list (the default) turns it off. The check and the grant are one SPARQL
  update, so two concurrent claims cannot both win, and the claim is always
  audited. A caller with no Person on an unclaimed dataset now gets
  `code: dataset_unclaimed` (a cue for the client to ask the person), but only
  if they could act on it. `holonbridge-bootstrap-admin` and the start scripts
  now default to `--role owner`; the new `--grant-existing` flag gives a dataset
  that predates the owner role its first owner. Closes #57
  ([#58](https://github.com/kurtcagle/holon-bridge-python/pull/58)).

### 2026-10-21

- **SHACL context: a `taxonomy` graph role, merged into the validation context beside the ontology.**
  A shape that checks a value against a controlled vocabulary (`sh:class
  skos:Concept`, concept-scheme membership, a `sh:node` into a concept shape)
  has the same problem the context pack was built for: validated against the
  data graph alone it cannot see the concept. Taxonomies get their own graph,
  `urn:{dataset}:taxonomy`, rather than living in the ontology graph, because
  they change at a different rate, are edited by different people, and should
  be grantable, versionable and replaceable on their own.
  `Conn.validation_context_graphs` is now `(ontology, taxonomy)`. Like the
  ontology, the taxonomy is read-only context: `ADD`-ed to the scratch copy,
  never written to, chosen by the dataset and never by a request.
  **No behaviour change for a dataset with no taxonomy graph** (`ADD SILENT`
  of an absent graph is a no-op), and the ordinary `shacl` graph is still
  validated against the target alone. Cost to be aware of: every validated
  write copies the context graphs into a scratch graph (twice in delta mode),
  so a very large taxonomy makes writes slower; not measured here. Shapes that
  need the taxonomy belong in `shacl-context`.
  ([#54](https://github.com/kurtcagle/holon-bridge-python/pull/54))

### 2026-10-07

- **Security: identity-bound bearer tokens -- the credential, not a header, decides who the caller is.**
  `require_animus` took the caller's identity from the `x-holon-animus-id`
  header and the only credential was one shared `BEARER_TOKEN`, so whoever
  held that token could send any identifier -- an admin's included -- and be
  that Person. New settings: `IDENTITY_TOKENS`
  (`token=identity` pairs; a request bearing one IS that identity and a header
  naming anyone else is a 403, the type header is ignored), `PROXY_TOKENS`
  (a trusted proxy such as the MCP remote may still assert the identity it has
  verified via OAuth; point the MCP process's own `BEARER_TOKEN` at one), and
  `ALLOW_LEGACY_BEARER` (default `true`). **No behaviour change on upgrade:** the
  shared token keeps its old meaning until a deployment sets
  `ALLOW_LEGACY_BEARER=false`. Misconfiguration (a token in two roles, a
  token under 24 characters, a malformed pair) refuses to start. Tokens are
  compared against every configured value with no early exit. Review follow-ups
  (same PR): `require_animus` now depends on `require_auth` itself, so there is
  no "classification missing, assume legacy" fallback (an unrecognised kind is a
  500, never a downgrade); `ALLOW_LEGACY_BEARER=false` with no identity or proxy
  token configured refuses to start; and **every audit entry records the token
  kind** (`hb:tokenKind`: `identity`, `proxy`, `legacy` or `open`), also returned
  by `GET /audit` as `token_kind` -- that is the field that tells a bound
  identity from one the caller merely asserted in the header. Bound tokens always
  resolve as a `GitHubIdentity`, so a service account must exist in the dataset
  as a Person with that identifier (documented in `.env.example`). The new tests
  run the real app and assert which external id the bridge *looked up*; each
  fix has a mutation (binding disabled, legacy flag ignored, fallback restored,
  startup check removed, audit field dropped) that fails them. Not covered here:
  per-token rotation or expiry (tokens are static config, like `BEARER_TOKEN`).
  [PR #51](https://github.com/kurtcagle/holon-bridge-python/pull/51)

### 2026-10-04

- **Fixed a Jena `ARQInternalErrorException: Peek iterator is already empty` crash in `find_delegation`.** Reproduced live against real Fuseki (not just the rdflib test harness, which cannot see this -- it's a Jena-specific ARQ optimizer bug): a 500 on every `check_write`/`authorize_query` call for an identity with no `grantsWrite`, whenever delegation lookup falls through to this function on a dataset with zero matching `holon:Delegation` triples for the bound pattern -- the common case, not an edge case. Took three rounds to actually fix, each one found by testing against the real shape `check_write` uses rather than trusting the previous round's fix: merging two `FILTER NOT EXISTS` clauses into one via `UNION` fixed the no-target case but not the with-target case every real write check hits; merging `holon:delegatedGraph` into the main predicate chain and swapping a `FILTER` equality on `?delegator` for `VALUES` each independently fixed their own isolated case but still crashed combined -- none of the SPARQL-level rewrites composed. Landed on replacing the one correlated query with the simplest possible candidate query (delegate + permission only, no `FILTER`/`FILTER NOT EXISTS`/`VALUES` beyond the plain join -- confirmed safe in every shape tried) plus validity-window, revocation, delegator-status and target/delegator narrowing in Python. Verified against the real running service, not just SPARQL in isolation: an unprivileged write attempt now returns a clean `403` instead of a `500`; a real delegation inserted the same shape the `/delegation` route produces lets the write through with correct attribution (`"delegated write from <delegator>"`). Full test suite unchanged (91 delegation tests passing; two pre-existing unrelated failure clusters confirmed present before this change too, not caused by it). [PR #43](https://github.com/kurtcagle/holon-bridge-python/pull/43)

### 2026-09-30

- **Fluent updates: a transition is confirmed by its own ledger entry.**
  `update_fluent` confirmed a transition by comparing the scene graph's
  current assertion with its own, which was wrong three ways: list-mode
  fluents never have one, so every `ListInsert`/`ListRemove` wrote its member,
  failed to confirm and looped to the attempt limit (8 ledger entries, then an
  error); a third writer landing between a transition's commit and its read-back
  made the winner retry and apply its operation a second time (an `Insert` delta
  counted twice); and `newValue` could be someone else's value. Since #41 the
  ledger entry exists exactly when the transition fired, so confirmation now asks
  the ledger for the transition's own entry. A `ListRemove` of a non-member is
  refused up front, and `POST /fluent/update` now returns 409 instead of a bare
  500 when the id minter exhausts its retries, as `/sequence/mint` does.
  Tested against rdflib's SPARQL engine, not Jena: 8 of 9 new tests fail on the
  previous code.
  [PR #42](https://github.com/kurtcagle/holon-bridge-python/pull/42)
- **Fluent updates: a lost race no longer leaves an orphan ledger entry.**
  `update_fluent` runs a ledger append and a scene update in one request. The
  scene update was a compare-and-set, but the ledger append beside it was an
  unconditional `INSERT DATA`: when two transitions on one fluent read the same
  current value, the loser's scene update correctly did nothing while its
  ledger entry still landed, leaving the fluent with two live
  `StateAssertion`s -- the condition `holon:StateAssertionNonOverlapShape`
  exists to forbid. The ledger append now carries the same `WHERE` guard as the
  scene update, so a loser writes neither half and retries as before, and the
  pre-commit trial is confined to its scratch copies. The tests run the real
  `update_fluent` against a real SPARQL engine (rdflib, not Jena): 5 of 9 fail
  on the old code and all pass now. **This does not repair existing data**; the
  PR carries a query for finding fluents that already have more than one live
  assertion. Two adjacent problems were found and deliberately left alone:
  list-mode operations can never confirm (8 ledger entries, then an error), and
  a possible double application under three-way interleaving.
  [PR #41](https://github.com/kurtcagle/holon-bridge-python/pull/41)
- **SHACL: a second, ontology-aware shapes graph.** Jena validates one flat
  graph, so a shape that needs the ontology visible -- a constraint over a
  property *classified* there, or a `sh:class` check against a class declared
  there -- was either vacuous (no focus node in the data graph) or wrong (a
  false violation on every correct node). A dataset may now keep
  `urn:{dataset}:shacl-context` (new graph role `shacl-context`), validated
  against the target **merged with the dataset's ontology** in a scratch copy;
  the ordinary `shacl` graph is unchanged. When a write names no
  `shapes_graph` of its own, both packs run and either can return a 422, each
  result tagged with its `pack`. Context is added after any reduction rule,
  and in delta mode the baseline is built with the context too, so a violation
  the ontology itself carries is never blamed on the payload. `POST /validate`
  and MCP `validate_turtle` gain `context` (default on, mirroring the write
  path) and `merge_context`. A dataset with no context shapes behaves exactly
  as before. Measured on the Bridgerton data: the holons graph alone gave 0
  violations for a planted bad write and 25 false ones for correct fluents;
  with the ontology merged in it caught the planted write and the 25 went
  away. Not fixed here: `POST /validate` has no identity check (see the PR).
  [PR #40](https://github.com/kurtcagle/holon-bridge-python/pull/40)
- **`get_holon` can now read as of a past time, and the as-of read stopped
  misleading.** The MCP `get_holon` tool gains `observed_at` (ISO 8601), so an
  LLM client can ask what a holon's fluents were worth at a given moment;
  until now only the REST route could. Three things in that path were wrong
  and are fixed: the frontmatter `scope` listed the caller's persona scope
  although an as-of read never uses it (it now names the graphs actually
  read, with `neighbourScope` for the neighbourhood queries and a note saying
  the read is ground-truth only); a holon with a list-mode
  (`holon:ListAccumulator`) fluent is refused with a 400, because its history
  is one member per event and the projection would have shown the last member
  as the whole set; and a timezone-naive `observed_at` is now read as UTC,
  because against the stored timezone-aware values a naive timestamp within
  14 hours of an assertion made Fuseki return **no fluent value at all**,
  silently. `inserted_at` is deliberately not exposed on MCP: nothing writes
  `hevt:invalidatedAt` and the insertion time is not consulted, so it would
  be inert. Verified against Jena Fuseki 5.5.0 (see
  `tests/test_get_holon_asof_live.py`).
  [PR #37](https://github.com/kurtcagle/holon-bridge-python/pull/37)
- **Security fix: raw SPARQL could reach graphs the ACL never checked.** A
  caller with no grants could write to a graph through `INSERT { GRAPH ?g ...
  } WHERE { VALUES ?g { <g> } }`, and the old check reported "references no
  graph" -- so allowed -- for `GRAPH ?g` however `?g` was bound, `FROM` /
  `FROM NAMED`, `WITH`, `USING`, and `DROP` / `CLEAR` / `CREATE` / `COPY` /
  `MOVE` / `ADD` / `LOAD`. New `acl.analyse_graph_refs` classifies every graph
  position (concrete, wholesale, dynamic) and new `acl.authorize_update`
  applies the rules: wholesale operations need `grantsReplace`; default-graph
  writes and `LOAD` are admin-only; a dynamic position (`GRAPH ?g`,
  `SERVICE ?v`, `DROP ALL`) is refused to non-admins for reads and to
  **everyone, admins included,** for updates, because the audit graph's
  per-graph protection cannot see through an update that names no graph.
  **Behaviour change:** non-admin queries using `GRAPH ?g` now return 403,
  and admins can no longer run `DROP ALL` or an update with `GRAPH ?g`; name
  the graphs. Restricting `GRAPH ?g` to the graphs a caller may read (via
  `FROM NAMED`) is left as a later hardening item.
  [PR #34](https://github.com/kurtcagle/holon-bridge-python/pull/34)
- **Audit: who may turn it down, and how.** A live test showed an admin could
  silence the log with one plain SPARQL update to the `meta` graph, and that
  update went unrecorded. The setting now lives in a bridge-owned graph,
  `urn:{dataset}:audit:config`, protected exactly like the audit graph:
  nobody can write it through the API, admin included, so the audited
  `/audit/*` routes are the only way to change it. `POST /audit/level` has a
  floor of `mutations` (accepts `mutations`, `standard`, or `default` to clear
  the override); `off` and `denials` are refused. Going lower is either a
  **time-boxed suspension** (`POST /audit/suspend`, reason mandatory, at most
  240 minutes, reverts on its own, start and end both recorded;
  `POST /audit/resume` ends it early) or a **permanent operator decision** in
  the bridge's environment (`HOLONBRIDGE_AUDIT_LEVEL`, and new
  `HOLONBRIDGE_AUDIT_LEVELS=dataset=level,...`). New MCP tools `suspend_audit`
  and `resume_audit`. **Behaviour change:** `set_audit_level('off')` now
  returns a 400, and `GET /audit/level` returns `effective`, `api_level`,
  `operator_level` and `suspension`. Also fixed: `GET /audit` never returned
  `hb:reason`, so refusal reasons and change notes were stored but invisible;
  it now returns them as `note`.
  [PR #33](https://github.com/kurtcagle/holon-bridge-python/pull/33)
- **Security fix: `GET /graph?iri=` now requires an identity and applies the
  `/sparql/select` read rules.** It previously had no identity check at all,
  so anyone holding the shared bearer token could read any graph in full --
  other people's private persona graphs, delegations, grants and the audit
  log. A persona graph now needs a `ReadGrant`, an admin bypass or a read
  delegation; the audit graph is admin-only; ground truth stays readable by
  any identified caller. **Behaviour change:** callers sending only the
  bearer token now get a 401. A refused read is audited with its reason, and
  a read of another person's private graph is recorded as a security read.
  `GET /graphs` (names and counts only) is unchanged.
  [PR #32](https://github.com/kurtcagle/holon-bridge-python/pull/32)
- **Audit log.** The bridge now records who did what, under what authority,
  and what it refused. One ASGI middleware (`holonbridge/audit.py`) captures
  every mutation, every 401/403 and -- at the default `standard` level --
  "security reads" (a read under `act_as`, `GET /holon?on_behalf_of=`, or of
  another person's private persona graph). Entries carry the acting person
  (and the real person behind an `act_as`), the target graphs, the write
  mode, what authorised the write (grant, delegation or admin bypass) and
  the reason for any refusal. Payloads are never stored, only a `sha256`
  digest and a size. Entries live in `urn:{dataset}:audit`, in an
  `hb:AuditEntry` vocabulary kept separate from `hev:AssertionEvent` domain
  events. **Nobody can write that graph through the API, admin included**
  (`acl.check_write`/`check_replace` refuse it before the admin bypass), and
  only admins can read it, through the new `GET /audit`, `GET /audit/level`,
  `POST /audit/level` and the `list_audit` / `get_audit_level` /
  `set_audit_level` MCP tools. Per-dataset levels `off` < `denials` <
  `mutations` < `standard`; a level change is always recorded. Fails open
  (a failed audit write is logged, never fails the request). See the PR for
  the known gaps. [PR #31](https://github.com/kurtcagle/holon-bridge-python/pull/31)
- **Delegation follow-ups.** `POST /admin/person/deactivate` now revokes
  every still-live delegation the Person gave or received, in the same
  atomic update, and returns them as `delegations_revoked`; reactivating a
  Person does not revive them. `write_turtle_to_graph` (and so
  `/graph/push`, `POST /holon`, `/message/create`) now reports
  `authorisedBy` / `authorisedReason` in its response, so a delegated write
  is distinguishable from the owner's own.
  [PR #30](https://github.com/kurtcagle/holon-bridge-python/pull/30)
- **MCP `get_holon` and `sparql_construct` return `str | dict`.** A refusal
  (for example a 403) used to fail FastMCP's output validation and surface as
  an opaque "Input should be a valid string" error instead of the bridge's
  reason. [PR #28](https://github.com/kurtcagle/holon-bridge-python/pull/28),
  [PR #29](https://github.com/kurtcagle/holon-bridge-python/pull/29)

### 2026-09-29

- **Delegation: a Person can share their own persona graphs with another
  Person, revocably, without impersonation.** New `holonbridge/delegation.py`,
  `holonbridge/routes/delegation.py` (`POST /delegation`,
  `POST /delegation/revoke`, `GET /delegation`), `shapes/delegation.ttl`, and
  the `create_delegation` / `revoke_delegation` / `list_delegations` MCP
  tools. A `holon:Delegation` is a stored, time-boxed record naming the
  delegator, delegate, persona, the exact graphs covered and the
  permissions (`read`, `write`); `acl.check_write` and
  `acl.authorize_query` honour an active one, so every existing write and
  raw-SPARQL path picks it up with no call-site changes. Never confers
  replace, admin or a Role; only ever covers the delegator's own
  persona-user graphs; bounded lifetime (default 7 days, max 90); a
  deactivated delegator's delegations stop working immediately.
  [PR #27](https://github.com/kurtcagle/holon-bridge-python/pull/27)
- **`GET /holon` and the `get_holon` MCP tool gain `on_behalf_of`.** Reads
  the delegator's scope, limited to exactly the graphs the delegation names
  -- the one deliberate, guarded exception to "a caller can only ever get
  their own scope". Refused with a 403 when no active read delegation
  exists, never silently falling back to the caller's own scope.
  [PR #27](https://github.com/kurtcagle/holon-bridge-python/pull/27)
- **Request-supplied IRIs are now validated before they reach generated
  Turtle or SPARQL** in the delegation code (`delegation.safe_iri`), with
  control characters escaped in literals. Written with regression tests
  that fail if the defence is removed.
  [PR #27](https://github.com/kurtcagle/holon-bridge-python/pull/27)

### 2026-09-27

- **Lucene full-text search provisioning.** New `holonbridge/provisioning.py`,
  `POST /datasets/{name}/text-index`, and the `provision_text_index` MCP
  tool -- adds a `jena-text` Lucene index to an existing dataset's Fuseki
  assembler file, with a sane default field set (`rdfs:label`/`comment`,
  `skos:prefLabel`/`altLabel`/`definition`/`comment`, `dcterms:title`,
  `sh:name`/`description`) and per-dataset `extra_predicates`. Admin-gated,
  preview-before-commit by default. [PR #24](https://github.com/kurtcagle/holon-bridge-python/pull/24)
- **Scheduler no longer takes the whole bridge down on a Fuseki hiccup at
  startup.** `Scheduler.start()` now degrades to an empty task list plus a
  loud warning if `reload()` fails, instead of letting the exception abort
  the entire FastAPI lifespan. Closes HB_SCHEDULER_STARTUP_HARD_DEPENDENCY,
  hit for real in production the same day this was found.
  [PR #25](https://github.com/kurtcagle/holon-bridge-python/pull/25)
- **Fixed invalid Turtle in the text-index provisioning tool.** The
  `text:map ( ... )` collection items were being joined with commas, which
  is invalid inside a Turtle collection (valid only in a predicate-object
  list). Because Fuseki's directory-config loader aborts the *entire*
  server on one bad assembler file, this took every dataset on the
  instance down, not just the one being provisioned. Fixed the join
  separator and added a parse-before-write safety check so a syntax error
  is now a raised error, never a written file discovered broken at the
  next restart. [PR #26](https://github.com/kurtcagle/holon-bridge-python/pull/26)
- Verified the whole Lucene path end to end: offline backfill of
  pre-existing data (`jena.textindexer`) against `chloe`, live auto-indexing
  of new writes against a fresh `test2` dataset, and the per-dataset
  identity-bootstrap requirement (`holonbridge/bootstrap.py`,
  `holonbridge-bootstrap-admin`) that a brand-new dataset needs before any
  `AnimusDep`-gated write will succeed in it. See
  `docs/provisioning-new-dataset-for-search.databook.md` for the full
  runbook.

<!-- databook:id: release-log -->
```turtle
@prefix hb:      <https://w3id.org/holonbridge/> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .

hb:release-2026-10-26-a a hb:ReleaseNote ;
    dcterms:date "2026-10-26"^^xsd:date ;
    rdfs:label "MCP own token; claim route 409 audited as a refusal, 404 for unknown dataset" ;
    dcterms:description "The MCP now reads HOLONBRIDGE_MCP_TOKEN, falling back to BEARER_TOKEN (a blank value counts as unset), so it can hold a PROXY_TOKENS token distinct from the legacy one; before, a token under any other name was ignored and every call was a 401 missing bearer token. Startup logs which variable was used, never the value. POST /admin/dataset/initialise now audits a 409 as denied with its code as the reason, and returns 404 unknown_dataset for a dataset the backend does not host (checked after eligibility, skipped if the Fuseki admin API is unavailable). DATASET_CLAIMANTS, the one-token-one-role rule and environment-over-file precedence are documented in .env.example and the README. The 409 audit outcome and the MCP token path were checked live; the 404 against stubs only." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/61> .

hb:release-2026-10-25-b a hb:ReleaseNote ;
    dcterms:date "2026-10-25"^^xsd:date ;
    rdfs:label "Fluent gate applies the graph-crossing SHACL guard" ;
    dcterms:description "The fluent pre-commit check validates through the client directly and so skipped the guard that refuses a SPARQLConstraint referencing GRAPH. On a gated dataset such a constraint silently passed every fluent transition; update_fluent now raises a FluentError naming the shape. Documented the flat form for a per-graph singleton constraint. Constraints that genuinely span graphs remain unenforceable, tracked in #60." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/59> .

hb:release-2026-10-25-a a hb:ReleaseNote ;
    dcterms:date "2026-10-25"^^xsd:date ;
    rdfs:label "Owner role and allowlisted dataset claim" ;
    dcterms:description "Added a reserved owner role above admin (owner implies admin; once an owner exists only an owner may grant, revoke or deactivate admin or owner) and POST /admin/dataset/initialise, which makes an allowlisted, identity-established caller the first owner of a dataset with no owner or admin. New setting DATASET_CLAIMANTS; empty by default, which disables the in-band claim. Legacy shared tokens and open deployments can never claim. A caller with no Person on an unclaimed dataset gets a typed dataset_unclaimed error, shown only if they could act on it. holonbridge-bootstrap-admin now defaults to --role owner and gains --grant-existing." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/58> .

hb:release-2026-10-21-a a hb:ReleaseNote ;
    dcterms:date "2026-10-21"^^xsd:date ;
    rdfs:label "SHACL context: a taxonomy graph role, merged into the validation context beside the ontology" ;
    dcterms:description "New graph role taxonomy (urn:{dataset}:taxonomy) for controlled vocabularies, kept apart from the ontology so they can be granted, versioned and replaced independently. Conn.validation_context_graphs is now (ontology, taxonomy), so shapes in shacl-context that check a value against a concept can see the concept. The taxonomy is read-only context, added to the scratch copy with ADD SILENT and never written to, chosen by the dataset and never by a request. A dataset with no taxonomy graph behaves exactly as before. Every validated write now copies the context graphs into a scratch graph (twice in delta mode), so a very large taxonomy will slow writes; not measured. Tested with the existing fake client, not a live Fuseki run." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/54> .

hb:release-2026-10-07-a a hb:ReleaseNote ;
    dcterms:date "2026-10-07"^^xsd:date ;
    rdfs:label "Identity-bound bearer tokens" ;
    dcterms:description "Added IDENTITY_TOKENS, PROXY_TOKENS and ALLOW_LEGACY_BEARER. A token in IDENTITY_TOKENS is bound to one external identity and require_animus uses it instead of the x-holon-animus-id header (a conflicting header is a 403); only PROXY_TOKENS may assert an identity via the header; the shared BEARER_TOKEN keeps its old behaviour unless ALLOW_LEGACY_BEARER is false. Previously whoever held the shared token could claim any identity, including admin." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/51> .

hb:release-2026-09-27-a a hb:ReleaseNote ;
    dcterms:date "2026-09-27"^^xsd:date ;
    rdfs:label "Lucene full-text search provisioning" ;
    dcterms:description "Added holonbridge/provisioning.py, POST /datasets/{name}/text-index, and the provision_text_index MCP tool for adding a jena-text Lucene index to an existing dataset's assembler file, with sane defaults and per-dataset extra_predicates." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/24> .

hb:release-2026-09-27-b a hb:ReleaseNote ;
    dcterms:date "2026-09-27"^^xsd:date ;
    rdfs:label "Scheduler no longer kills the whole bridge on a Fuseki hiccup at startup" ;
    dcterms:description "Scheduler.start() now degrades to an empty task list with a warning instead of letting a connection error abort the entire FastAPI lifespan on startup. Closes HB_SCHEDULER_STARTUP_HARD_DEPENDENCY." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/25> .

hb:release-2026-09-27-c a hb:ReleaseNote ;
    dcterms:date "2026-09-27"^^xsd:date ;
    rdfs:label "Fixed invalid Turtle in text-index provisioning" ;
    dcterms:description "text:map collection items were being comma-joined, which is invalid Turtle and took down every dataset on the instance when Fuseki's directory-config loader hit it. Fixed the join separator and added a parse-before-write safety check." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/26> .

hb:release-2026-09-27-d a hb:ReleaseNote ;
    dcterms:date "2026-09-27"^^xsd:date ;
    rdfs:label "End-to-end verification of dataset text-search provisioning" ;
    dcterms:description "Confirmed offline backfill (jena.textindexer) against chloe's pre-existing data, live auto-indexing of new writes against a fresh test2 dataset, and documented the per-dataset identity-bootstrap requirement for AnimusDep-gated writes on a brand-new dataset." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/blob/main/docs/provisioning-new-dataset-for-search.databook.md> .

hb:release-2026-09-29-a a hb:ReleaseNote ;
    dcterms:date "2026-09-29"^^xsd:date ;
    rdfs:label "Delegation: revocable, time-boxed read/write access to a Person's own persona graphs" ;
    dcterms:description "Added holon:Delegation (holonbridge/delegation.py, routes/delegation.py, shapes/delegation.ttl) and the create_delegation, revoke_delegation and list_delegations MCP tools. acl.check_write and acl.authorize_query honour an active delegation; check_replace never does. Covers only the delegator's own persona-user graphs, never confers replace, admin or a Role, and a deactivated delegator's delegations stop working immediately." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/27> .

hb:release-2026-09-29-b a hb:ReleaseNote ;
    dcterms:date "2026-09-29"^^xsd:date ;
    rdfs:label "GET /holon and get_holon gain on_behalf_of" ;
    dcterms:description "Reads a delegator's scope restricted to exactly the graphs a read delegation names; the one deliberate, guarded exception to callers only ever getting their own scope. Refused with a 403 when no active read delegation exists." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/27> .

hb:release-2026-09-29-c a hb:ReleaseNote ;
    dcterms:date "2026-09-29"^^xsd:date ;
    rdfs:label "Request-supplied IRIs validated before reaching generated Turtle or SPARQL" ;
    dcterms:description "delegation.safe_iri refuses characters that could change a generated statement's structure, and literals now escape control characters. Regression tests fail if either layer is removed." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/27> .

hb:release-2026-09-30-a a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "Audit log: mutations, refusals and security reads" ;
    dcterms:description "Added holonbridge/audit.py (an ASGI middleware, per-dataset levels off/denials/mutations/standard, hb:AuditEntry vocabulary), routes/audit.py (GET /audit, GET and POST /audit/level) and the list_audit, get_audit_level and set_audit_level MCP tools. Entries record actor, real actor under act_as, target graphs, write mode, authority and refusal reason, with a payload digest and never the payload. The audit graph cannot be written through the API by anyone, admin included, and is readable by admins only. Fails open." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/31> .

hb:release-2026-09-30-b a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "GET /graph refuses the audit graph" ;
    dcterms:description "GET /graph?iri= has no identity check of its own, so it now refuses the audit graph explicitly; that graph is read through the admin-only GET /audit." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/31> .

hb:release-2026-09-30-c a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "Delegation follow-ups: deactivate revokes delegations; writes report authorisedBy" ;
    dcterms:description "deactivate_person revokes every live delegation the Person gave or received in the same atomic update and returns delegations_revoked; write_turtle_to_graph responses now include authorisedBy and authorisedReason." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/30> .

hb:release-2026-09-30-d a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "MCP get_holon and sparql_construct return str | dict" ;
    dcterms:description "A refusal used to fail FastMCP's output validation and surface as an opaque validation error; the tools now return the bridge's error object." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/29> .

hb:release-2026-09-30-e a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "GET /graph requires an identity and applies the /sparql/select read rules" ;
    dcterms:description "GET /graph?iri= previously had no identity check, so anyone with the shared bearer token could read any graph in full. It now depends on AnimusDep and runs acl.authorize_query on a probe for that one graph: a persona graph needs a ReadGrant, an admin bypass or a read delegation, the audit graph is admin-only, and ground truth is readable by any identified caller. Callers sending only the bearer token now get a 401. Supersedes the audit-graph-only refusal from PR 31." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/32> .

hb:release-2026-09-30-f a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "Audit: bridge-owned config, an API floor, time-boxed suspensions, operator levels" ;
    dcterms:description "The audit setting moved from the admin-writable meta graph to urn:{dataset}:audit:config, protected like the audit graph. POST /audit/level accepts only mutations, standard or default; off and denials are either a time-boxed POST /audit/suspend (mandatory reason, at most 240 minutes, self-reverting, start and end recorded) or an operator decision via HOLONBRIDGE_AUDIT_LEVEL / HOLONBRIDGE_AUDIT_LEVELS. Also fixed GET /audit omitting hb:reason. set_audit_level('off') now returns a 400." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/33> .

hb:release-2026-09-30-g a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "Raw SPARQL can no longer reach graphs the ACL never checked" ;
    dcterms:description "acl.analyse_graph_refs classifies every graph position in a query or update (concrete, wholesale, dynamic) and acl.authorize_update applies the rules. Previously GRAPH ?g however bound, FROM/FROM NAMED, WITH, USING and DROP/CLEAR/CREATE/COPY/MOVE/ADD/LOAD all reported no graph and were allowed; a live test wrote to a graph with no grant via INSERT { GRAPH ?g } WHERE { VALUES }. Wholesale operations need grantsReplace, default-graph writes and LOAD are admin-only, dynamic reads are refused to non-admins and dynamic updates to everyone, admins included." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/34> .

hb:release-2026-09-30-h a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "get_holon as-of reads: observed_at on MCP, honest scope, list-fluent guard, UTC-normalised timestamps" ;
    dcterms:description "The MCP get_holon tool gains observed_at. The as-of path now reports the graphs it actually read in scope (neighbourScope carries the neighbourhood scope, asOf.note says the read is ground-truth only), refuses holons with list-mode fluents with a 400 rather than returning the last member as the set, and reads timezone-naive timestamps as UTC (a naive timestamp within 14 hours of an assertion made Fuseki return no fluent value at all). inserted_at is not exposed on MCP because the transaction-time axis is not yet recorded. Verified on Jena Fuseki 5.5.0." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/37> .

hb:release-2026-09-30-i a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "SHACL: a second, ontology-aware shapes graph (context pack)" ;
    dcterms:description "A dataset may keep urn:{dataset}:shacl-context (new graph role shacl-context), validated against the target merged with the dataset's ontology in a scratch copy, because Jena's SHACL service validates one flat graph and a shape that needs the ontology visible is otherwise vacuous or wrong. Writes naming no shapes_graph run both packs and either can return a 422, each result tagged with its pack; context is added after any reduction rule and, in delta mode, to the baseline as well so the ontology's own violations are not blamed on the payload. POST /validate and MCP validate_turtle gain context and merge_context. No context shapes graph means unchanged behaviour. Not fixed here: POST /validate has no identity check." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/40> .

hb:release-2026-09-30-j a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "Fluent updates: a lost race no longer leaves an orphan ledger entry" ;
    dcterms:description "update_fluent ran a compare-and-set scene update beside an unconditional INSERT DATA ledger append in one request, so when two transitions on a fluent read the same current value the loser's scene update correctly no-opped while its ledger entry still landed, leaving two live StateAssertions. The ledger append now carries the same WHERE guard as the scene update, so a loser writes neither half and retries as before, and the pre-commit trial substitutes the scene graph IRI in the ledger statement as well. Tested against a real SPARQL engine (rdflib, not Jena): 5 of 9 new tests fail on the old code. Does not repair existing orphans; the PR has a detection query. Left alone: list-mode operations cannot confirm (8 ledger entries, then an error), and a possible double application under three-way interleaving." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/41> .

hb:release-2026-09-30-k a hb:ReleaseNote ;
    dcterms:date "2026-09-30"^^xsd:date ;
    rdfs:label "Fluent updates: a transition is confirmed by its own ledger entry; mint exhaustion is a 409" ;
    dcterms:description "update_fluent confirmed a transition by comparing the scene graph's current assertion with its own, which failed for list-mode fluents (they never have one, so every ListInsert/ListRemove looped to the attempt limit writing 8 ledger entries, then raised), made a winner retry and re-apply its operation when a third writer landed before its read-back (an Insert delta counted twice), and could report someone else's value as newValue. Since PR 41 a transition's ledger entry exists exactly when it fired, so confirmation now asks the ledger for the transition's own entry. A ListRemove of a non-member is refused up front before an id is minted, and POST /fluent/update now maps SequenceError to a 409 like /sequence/mint instead of a bare 500. Tested against rdflib's SPARQL engine, not Jena: 8 of 9 new tests fail on the previous code." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/42> .

hb:release-2026-10-04-a a hb:ReleaseNote ;
    dcterms:date "2026-10-04"^^xsd:date ;
    rdfs:label "find_delegation: fixed a Jena ARQ crash on the common zero-delegations case" ;
    dcterms:description "find_delegation 500'd against real Fuseki with ARQInternalErrorException: Peek iterator is already empty, on any dataset with no matching Delegation triples for the bound pattern -- the common case, hit via check_write/authorize_query for any identity with no grantsWrite. The rdflib test harness in tests/test_delegation*.py cannot see this; it's a Jena ARQ optimizer bug. Three independent SPARQL-level rewrites (merging two FILTER NOT EXISTS into one via UNION, merging delegatedGraph into the main predicate chain, VALUES instead of a FILTER equality on ?delegator) each fixed their own isolated case live against Fuseki but still crashed combined -- none composed. Fixed by replacing the one correlated query with the simplest possible candidate query (delegate + permission only) plus validity-window, revocation, delegator-status and target/delegator narrowing in Python instead of SPARQL. Verified against the real running service: an unprivileged write now returns a clean 403, not a 500; a real delegation lets the write through with correct attribution. Full test suite unchanged." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pull/43> .
```

hb:release-2026-10-27-a a hb:ReleaseNote ;
    dcterms:date "2026-10-27"^^xsd:date ;
    rdfs:label "Datasets can be created through the bridge (create-only), behind DATASET_CREATORS" ;
    dcterms:description "POST /datasets and the create_dataset MCP tool create an empty persistent (TDB2) or in-memory dataset through Fuseki's /$/datasets admin API, uploading an assembler the bridge generates itself (never one from a caller), which includes a shacl endpoint, and seeding the bundled shapes (shapes=false to skip). Persistent datasets go under FUSEKI_DATABASES_DIR. Gated on an established identity listed in the new DATASET_CREATORS setting (empty by default, separate from DATASET_CLAIMANTS); the legacy shared token and an open deployment never qualify. The creator becomes the owner unless claim is false. Strict lower-case names, reserved names refused, case-insensitive existence check, MAX_DATASETS ceiling, successful creations always audited. There is deliberately no drop: a test asserts no DELETE route and no drop method exist. Tested against rdflib and a mocked Fuseki, not a live server." ;
    rdfs:seeAlso <https://github.com/kurtcagle/holon-bridge-python/pulls> .
