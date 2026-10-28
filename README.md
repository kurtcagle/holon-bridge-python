---
title: HolonBridge (Python)
version: 0.1.0
status: working core, not yet at parity with the Node bridge
--- 

# HolonBridge — Python

A Python implementation of the HGA Server: the bridge between the Holon
Graph Architecture pipeline and an Apache Jena Fuseki backend. FastAPI for
the REST surface, `httpx` for the backend, `rdflib` where triples genuinely
need to be handled in-process, and a stdio MCP layer on top.

The architectural contract is unchanged from the Node bridge. External
clients never reach Fuseki. The bridge owns authentication, the SHACL gate,
graph-name resolution, and DataBook-level projection.

```
Claude Code (stdio) ──┐
                      │   ┌──────────────────────┐        ┌──────────────┐
Browser / artefact ───┼── │  holonbridge (:3031) │ ────── │ Fuseki :3030 │
                      │   │  FastAPI + httpx     │        │ dataset /ds  │
PowerShell / CLI ─────┘   └──────────────────────┘        └──────────────┘
                                     ▲
                          holonbridge_mcp (stdio) — 49 tools
```

---

## Scope

This is the core, running and tested. It is not a line-for-line port of
v2.10.0.

| Ported | Not yet ported |
|---|---|
| Bearer auth, per-request `Conn`, `X-Dataset-Override` | Per-dataset ACLs |
| GitHub OAuth + PKCE (remote MCP transport) | GitHub push / delete endpoints |
| SPARQL select / construct / update, with endpoint guards | Federated network registry |
| GSP push (merge and replace), get, drop, list with counts | Admin console, file-watching restart |
| SHACL validation, including delta mode | Dataset admin — create, drop |
| `get_holon` → DataBook, with `subPropertyOf*` role discovery | LLM proposer for `LLMInvocation` |
| Named-query registry, dual `hb:` / `hquery:` vocabularies | |
| Named rules — Append / Replace / Sync, `/graph-op` | |
| Pipelines, ingest, and `hb:Message` status polling | |
| Scheduler — gates, ODRL caps, provenance, quarantine | |
| Projection hooks — watermarked deltas, pull and webhook | |
| `LLMInvocation` proposer, delivery sweeper, recursion guards | |
| Sequence minting with compare-and-set | |
| DataBook parse and render | |
| Dataset listing and switching — `/datasets`, `switch_dataset` | |
| MCP stdio server, 49 tools | |

The omitted pieces are all additive: they are separate route modules over
the same `Conn` and the same client, so they slot in without touching what
is here.

---

## Install and run

```powershell
git clone <your-fork> holon-bridge-py
cd holon-bridge-py
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[mcp,dev]"

Copy-Item .env.example .env    # then set BEARER_TOKEN — loaded automatically,
                                # see "One shared .env" below

# Fuseki, if it is not already up
fuseki-server --update --loc C:\jena\data /ds

holonbridge                     # listening on 127.0.0.1:3031
```

Generate a token:

```powershell
[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
```

Smoke test:

```powershell
$env:HB = "http://localhost:3031"
$env:HBTOK = "<your-token>"
$h = @{ Authorization = "Bearer $env:HBTOK" }

Invoke-RestMethod "$env:HB/health"
Invoke-RestMethod "$env:HB/endpoint" -Headers $h
Invoke-RestMethod "$env:HB/graphs"   -Headers $h | Select-Object -Expand graphs
```

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. No token required. |
| `GET` | `/endpoint` | Active profile, dataset, resolved graph IRIs, backend reachability |
| `GET` | `/endpoints` | All named profiles |
| `POST` | `/endpoint` | Switch active profile |
| `POST` | `/endpoints/reload` | Re-read `~/.holonbridge/config.json` |
| `GET` | `/datasets` | Datasets the backend actually hosts, per Fuseki's own admin API |
| `GET` | `/datasets/{name}` | Confirm one dataset exists; its graph-role IRIs |
| `GET` | `/whoami` | The resolved Person, persona, and act_as state for the caller's credential |
| `GET` | `/persona/list` | Personas in this dataset, and whether the caller holds a Home under each |
| `POST` | `/persona/switch` | Set the caller's own active persona override (self-service) |
| `POST` | `/admin/person` | Create a Person, external identity, and optional Role |
| `GET` | `/admin/persons` | Every Person, with identities, roles, status, write/replace grants |
| `POST` | `/admin/role/grant` | Grant an existing Person a Role, standalone |
| `POST` | `/admin/role/revoke` | Retract a Role grant; refuses to strip the last admin |
| `POST` | `/admin/write/grant` | Grant `grantsWrite`/`grantsReplace` on one target |
| `POST` | `/admin/write/revoke` | Retract a write/replace grant |
| `POST` | `/admin/person/deactivate` | Revoke all access, mark inactive; identity stops resolving |
| `POST` | `/admin/person/reactivate` | Clear the inactive marker; access is not reimplied |
| `POST` | `/admin/persona/assign` | Mint a Home for an existing Person under a Persona |
| `POST` | `/admin/act-as` | Admin-only impersonation for exercising real grant checks |
| `POST` | `/admin/cease-acting-as` | Clear an active act_as override |
| `POST` | `/sparql/select` | SELECT / ASK → SPARQL JSON |
| `POST` | `/sparql/construct` | CONSTRUCT / DESCRIBE → Turtle |
| `POST` | `/sparql/update` | UPDATE against the update endpoint |
| `GET` | `/graphs` | Named graphs with triple counts, `?filter=` substring |
| `GET` | `/graph?iri=` | Fetch a named graph as Turtle |
| `POST` | `/graph/push` | Ingest Turtle; `mode=merge\|replace`; optional SHACL gate |
| `DELETE` | `/graph?iri=` | Drop a named graph |
| `GET` | `/holon?iri=` | Holon projected as a DataBook |
| `POST` | `/validate` | SHACL report; `mode=auto\|full\|delta` |
| `GET` | `/named-queries` | Registered queries, `?vocabulary=hb\|hquery`, `?filter=` |
| `GET` | `/named-query/{id}` | Full definition including the SPARQL body |
| `POST` | `/named-query/{id}/run` | Bind parameters and execute; `dry_run` returns the bound query |
| `POST` | `/named-queries/reload` | Discard the cached registry |
| `GET` | `/named-rules` | Registered rules, `?rule_status=` |
| `GET` | `/named-rule/{id}` | Full definition including the CONSTRUCT body |
| `POST` | `/named-rule/{id}/run` | Materialise; `write_mode` override, `dry_run` |
| `POST` | `/named-rules/run` | Fire every active rule once, in order |
| `POST` | `/named-rules/reload` | Discard the cached registry |
| `POST` | `/graph-op` | CLEAR, DROP, CREATE, COPY, MOVE, ADD |
| `GET` | `/pipelines` | Registered manifests |
| `GET` | `/pipeline/{id}` | Manifest nodes, resolved run order, warnings |
| `POST` | `/pipeline` | Register a manifest into its own graph |
| `DELETE` | `/pipeline/{id}` | Drop a manifest and its index entry |
| `POST` | `/pipeline-run` | Run in dependency order; `wait` for inline |
| `POST` | `/ingest` | Land a payload, then optionally run a pipeline |
| `GET` | `/message/{id}` | Status of an asynchronous run |
| `GET` | `/messages` | Recent runs |
| `GET` | `/scheduler/status` | Running state, counts, in-flight tasks |
| `GET` | `/scheduler/tasks` | Scheduled tasks, `?task_status=` |
| `GET` | `/scheduler/task/{id}` | Full task definition |
| `POST` | `/scheduler/task` | Create a task |
| `POST` | `/scheduler/task/{id}/status` | Suspend, resume, deprecate |
| `POST` | `/scheduler/task/{id}/fire` | Fire now, tagged `manual` |
| `GET` | `/scheduler/activity` | Firing records; `since` needs a timezone |
| `GET` | `/scheduler/quarantine` | Proposals held back by validation |
| `POST` | `/scheduler/reload` | Re-read tasks and personas |
| `POST` | `/scheduler/tick` | Run one pass immediately |
| `GET` | `/projection/hooks` | Registered hooks, with problems flagged |
| `GET` | `/projection/hook/{id}` | Hook detail and watermark size |
| `POST` | `/projection/hook` | Register a hook |
| `POST` | `/projection/hook/{id}/status` | Suspend, resume, deprecate |
| `DELETE` | `/projection/hook/{id}` | Drop a hook and its watermark |
| `POST` | `/projection/hook/{id}/run` | Compute the delta and deliver it |
| `POST` | `/projection/hook/{id}/reset` | Clear the watermark |
| `GET` | `/projection/deliveries` | Delivery log, filterable |
| `GET` | `/projection/delivery/{id}` | One delivery; pending ones carry the envelope |
| `POST` | `/projection/delivery/{id}/ack` | Applied — advance the watermark |
| `POST` | `/projection/delivery/{id}/reject` | Not applied — keep the watermark |
| `POST` | `/projection/sweep` | Reclaim deliveries never acknowledged |
| `POST` | `/sequence/mint` | Mint the next identifier |
| `GET` | `/sequence/{name}` | Current counter value without advancing |

Example push:

```powershell
$body = @{
  turtle    = Get-Content .\sensors.ttl -Raw
  graph_iri = "urn:bridgerton:holons"
  mode      = "merge"
} | ConvertTo-Json

Invoke-RestMethod "$env:HB/graph/push" -Method Post -Headers $h `
  -ContentType "application/json" -Body $body
```

---

## Design notes

### Turtle 1.2 is passed through, not parsed

`rdflib` reads Turtle 1.1. Jena 6.0 reads Turtle 1.2, triple terms
included. So `PARSE_MODE=passthrough` is the default and payloads reach Jena
byte-for-byte; Jena is the syntax authority. `PARSE_MODE=local` buys a
faster syntax error and will reject perfectly valid RDF 1.2 — use it only on
a pipeline you know is 1.1.

Where the bridge genuinely needs triples in-process, it only ever handles
SHACL reports, which are plain 1.1.

### One `Conn` per request

`holonbridge/conn.py` resolves the profile, the dataset, and the override in
one place; `require_conn` is the only way a handler obtains it. A route that
forgets the override cannot exist, because a route that forgets the
dependency has no connection at all.

`X-Dataset-Override` retargets the dataset, never the server, and the name
is validated against a path-safe character set. Anything that would refresh
process-global state must check `conn.overridden` first.

### Graph naming lives in one function

`conn.graph(role)` returns `urn:{dataset}:{role}` for the eight known roles.
This is the convention that `urn:data:*` datasets break, and breaking it is
what makes the SHACL gate unarmable — a shapes graph the resolver cannot
find is an empty shapes graph, and an empty shapes graph either rejects
everything or validates nothing. Unknown roles raise rather than silently
producing an IRI nobody has ever written to.

### Dataset listing is separate from the configured profiles

`/endpoints` (and `list_endpoints` in MCP) reports the bridge's own
configured profiles — an *intention*: "connect to this server, use this
dataset." `/datasets` (`list_datasets`) asks Fuseki's `/$/datasets` admin API
what it actually hosts — the *observation*. The two disagreeing is normal
and worth being able to see: a profile naming a dataset that was renamed or
never created looks identical to a working one until something reads from
it and comes back empty, which is exactly the failure `GET /datasets/{name}`
exists to catch before it happens — `switch_dataset` calls it first, so a
typo returns what's actually available rather than silently pointing every
subsequent call at an empty dataset.

`switch_dataset` sets `X-Dataset-Override` on the MCP layer for the rest of
the process, rather than taking a `dataset` argument on all forty-nine
tools. Switching is a session gesture — "I'm working on `bridgerton` now" —
not something to repeat on every call. The cost is that it is genuinely
global: a second client against the same MCP process sees the switch too.
Fine for one operator on one tunnel, wrong for shared hosting.

Listing only. Creating and dropping a dataset are deliberately not exposed:
those are destructive server-level operations, and the bridge still holds
one credential shared by every caller, so "who asked for this" has no
answer yet. The GitHub OAuth layer now carries an authenticated `sub` on
every request — an allowlisted login, not just a valid token — but nothing
downstream of the gate consumes it. A `DROP` behind a shared secret with no
caller identity attached is not something to add casually; this waits on
that identity actually reaching the route layer.

### User administration -- grant, revoke, deactivate

The ACL model (`acl.py`) has always been: a Person resolves from an
external identity (`resolve_person`, driving `require_animus`); a Person
holds zero or more Roles; a Role grants ReadGrant/InvokeGrant scoped to a
Persona or a named query/rule; write and replace access are never
role-based -- only an explicit `holon:grantsWrite`/`holon:grantsReplace`
naming that exact (person, target) pair allows them; and a narrowly-held
admin Role bypasses every check unconditionally, as a break-glass
capability rather than a role competing for access the ordinary way.

Through 2026-09-01 the only way to *create* any of this was
`POST /admin/person` (Person + identity + one optional Role, at creation
time only) and `POST /admin/persona/assign` (a Home under an existing
Persona). There was no way to grant a Role after the fact, no way to
create or remove a `grantsWrite`/`grantsReplace` edge at all outside
hand-written SPARQL, no way to revoke anything, and no way to see, in one
call, who currently holds what -- auditing "who can do what" meant
writing the same queries `acl.py` runs internally, by hand, against a
graph a caller may not even be able to name.

`POST /admin/role/grant` and `POST /admin/role/revoke` close the first
gap: standalone Role assignment/retraction for a Person who already
exists, using the identical Turtle shape `create_person`'s own `role`
field already used, so a Role granted here is indistinguishable from one
granted at creation time. `POST /admin/write/grant` and
`POST /admin/write/revoke` do the same for the `grantsWrite`/
`grantsReplace` axis specifically -- this was the one thing `create_person`
*never* touched at all, at any point, since writes are deliberately kept
out of the Role/grants machinery. `GET /admin/persons` closes the read
gap: Person, external identities, Roles, and write/replace grants, walked
in one call.

Both revoke routes return `{"ok": false, "note": "not_found: ..."}`
rather than a 4xx when the thing being revoked was never held -- the same
convention `assign_persona` already used for "no such Persona"/"no Home
here": an ordinary, expected outcome, not an error worth a status code
for.

**Revoking a Person's last admin Role is refused**, by both
`revoke_role` and `deactivate_person` below. The admin bypass has no
ordinary-Role fallback underneath it, so a dataset with zero admins left
has no route back in through the REST API at all -- only
`holonbridge-bootstrap-admin`, run by hand directly against Fuseki, the
same chicken-and-egg bypass `bootstrap.py` exists for in the first place.
Refusing here is cheaper than ever needing that. The guard is a
check-then-act read followed by a write, not a transactional
compare-and-set -- two concurrent admins simultaneously revoking *each
other's* admin Role would each see "another admin still exists" and both
proceed, the same TOCTOU shape `write_turtle_to_graph` and
`sequence.py`'s mint already had to close elsewhere in this codebase.
Worth knowing about; not fixed here, since it needs the same kind of
compare-and-set the sequence minter uses, which is more machinery than a
first pass at admin routes warranted.

**Removing a user is `POST /admin/person/deactivate`, not deletion.** A
Person is referenced from provenance -- a past `act_as`'s `real_person`,
a scheduler firing, an authored holon -- that should never be left
dangling just because someone's access ended. Deactivation instead: (1)
retracts every `holon:hasRole` edge the Person holds, (2) retracts every
`holon:grantsWrite`/`holon:grantsReplace` edge naming them, and (3)
asserts `holon:status "inactive"` -- all three in one semicolon-joined
SPARQL Update request, never several separate `client.update()` calls,
for the identical reason the named-rule Replace-mode fix (`#12`/`#14`,
above) landed on one atomic request: splitting a logical multi-step write
into separate HTTP calls is exactly what let a concurrent execution
interleave and lose data there, and nothing about that risk is specific
to named rules.

Step 3 is what actually locks the door, and it required one change to
`acl.py`: `resolve_person` now treats a Person carrying
`holon:status "inactive"` exactly like an external id that never
resolved -- `None`, not a Person with no Roles. Since `require_animus`
builds every caller's `Animus` through `resolve_person`, a deactivated
caller gets a 401 on their very next request, on *any* route, not only
admin-gated ones. Steps 1 and 2 are belt-and-suspenders for anything that
still consults Roles or grants directly instead of going through identity
resolution -- `act_as` is the one route that does: `build_animus_as`
never itself checks `holon:status`, so `POST /admin/act-as` now carries
its own `FILTER NOT EXISTS { ... holon:status "inactive" }` in its
target-existence check, refusing to let an admin impersonate a
deactivated Person even though that Person's own real login no longer
could.

`deactivate_person` also refuses to deactivate the caller's own real
identity (`animus.real_person`) -- self-locking an admin out mid-session,
with no route back in but the direct-Fuseki bootstrap bypass, is not a
mistake this should let happen silently.

`POST /admin/person/reactivate` clears the `inactive` marker and nothing
else. Roles and write/replace grants that deactivation stripped are not
reimplied -- a reactivated Person has no access again, exactly like a
fresh `create_person` call with no `role`, and getting any back is a
separate, explicit `grant_role`/`grant_write` call. Silent automatic
restoration of "whatever they had before" is the kind of thing that reads
fine right up until it's the wrong call.

The identity shape (`shapes/identity.ttl`) grew one more optional
property to match: `holon:status`, `sh:in ("active" "inactive")`,
`maxCount 1`. Deliberately not `sh:minCount 1` -- absence means active,
so every Person created before this shipped, and every dataset that
never adopts deactivation at all, stays conformant with no backfill
needed.

### Delta-mode SHACL

Full validation of a merged graph makes every write answerable for the whole
target: one pre-existing violation blocks all subsequent writes, which is
why gates get switched off. Delta mode validates the target, validates the
target merged with the payload, and reports only the difference.

The merge happens in a per-request scratch graph via server-side `COPY` and
GSP `POST` — no Turtle round trip through the bridge — and the scratch graph
is dropped in a `finally`.

Consequence worth knowing: delta mode needs a `target_graph`. Validating a
payload in isolation is a different question and will flag cross-references
the target graph would have satisfied. `/validate` with `mode=auto` picks
delta whenever a target is supplied.

**Writing a constraint that is "one per graph".** Delta mode validates the
target graph as one flat graph, so a SPARQLConstraint should be written
against that graph and not reach for `GRAPH ?g`:

```sparql
SELECT $this ?other
WHERE { ?other a holon:Home . FILTER (?other != $this) }
```

This enforces "at most one `holon:Home` in the graph being written to" — the
usual per-persona or per-user singleton — and it is what a write is checked
against. Homes in other graphs do not conflict, which is deliberate. A
constraint that genuinely spans graphs (uniqueness across the whole dataset,
say) cannot be enforced by Jena's SHACL service, so a shapes graph containing
a `GRAPH` inside any `sh:select` or `sh:ask` is refused outright, with the
offending shape named, on every gated path: `push_turtle`, `/validate`,
pipeline and scheduler validation, and fluent transitions. The check is a
keyword scan, so it also fires on a deactivated shape, or one that only
mentions `GRAPH` in a comment. See
[#2](https://github.com/kurtcagle/holon-bridge-python/issues/2).

**Three bugs found live against real shapes, none reachable from the unit
suite, all fixed.** Worth stating plainly rather than folding quietly into
the design description above, because each one changed what "the gate is
armed" actually meant.

*A write to a graph that doesn't exist yet used to 404.* `COPY SILENT` from
a nonexistent target copies nothing, so the scratch graph was never created,
and asking Jena to validate a graph it had never seen returned "No data
graph" instead of an empty report — on exactly the write where validation
matters most, the first one. Fixed: the baseline now checks for content with
an `ASK` before attempting the copy, and validates the target in place
rather than through a scratch copy at all.

*`sh:Warning` used to block writes.* Only `sh:Violation` does now, with
absent severity still defaulting to Violation per spec. A shape author
reaches for Warning specifically when a constraint cannot be fully checked
at write time — a range check needing another graph, say — and treating it
as blocking discarded that intent while also rejecting the write. The
response now reports `violations` and `warnings` as separate counts.

*Replace mode was validated as if it were a merge — the more serious of the
three.* The gate copied the target into a scratch graph and merged the
payload in, which is correct for `merge` (the post-write state genuinely is
target plus payload) and wrong for `replace` (the post-write state is the
payload alone). Validating the union hid every violation caused by
*removal*: a required property deleted by the replace was still sitting in
the union, satisfying its own `minCount` on behalf of a payload that had
dropped it. Demonstrated live — replaced a conforming `Book` with one
missing its required `rdfs:label`, the gate reported `conforms: true,
violations: 0`, and the write landed. The graph then violated its own shapes
with a clean bill of health, which is a worse failure than refusing a good
write would have been. Fixed: both `validate_full` and `validate_delta` take
`write_mode`, and under `replace` the scratch graph holds the payload alone —
no `COPY`. Wired through `/graph/push` and `/ingest`, which had the
identical hole.

### Role discovery instead of hardcoded predicates

`get_holon` finds neighbours through `?predicate rdfs:subPropertyOf* holon:isPartOf`
and the same for `holon:isConnectedTo`. A domain that models
`geo:administrativePartOf` navigates without the bridge knowing the term
exists. The generated queries ship inside the returned DataBook, so a
projection is reproducible from the artefact alone.

Canonical namespace throughout is `https://w3id.org/holon/`.

### Two named-query vocabularies, one reconciliation point

`hb:` (`https://w3id.org/holonbridge/`) is the bridge's own original scheme.
`hquery:` is the HGA Named Query Specification. Both register a query, a
body, and parameters — but the difference between them survives loading, so
it cannot be normalised away at load time.

`hb:` bodies carry `{{placeholder}}` tokens and want string substitution.
`hquery:` bodies carry ordinary SPARQL variables and want them bound. Running
an `hquery:` query through `{{...}}` substitution matches nothing, so the
query executes unparameterised and returns every row — a wrong answer with no
error, which is worse than a failure. Every loaded query therefore carries
its vocabulary and binding dispatches on it.

Binding for `hquery:` appends a `VALUES` clause. SPARQL's grammar places
`ValuesClause` after the whole query form, past ORDER BY and LIMIT, so this
needs no parsing of the body and cannot corrupt a well-formed query.
Unsupplied parameters stay unbound, preserving the "omit for all" behaviour
HGA queries rely on. Undeclared parameters are rejected rather than ignored:
an undeclared VALUES variable does not error, it quietly constrains nothing,
so a typo would silently run a query nobody meant.

Two departures from the Node version worth your eye:

- **Class matching is by local name.** The loader takes anything typed
  `*NamedQuery` in the registry graph and derives the vocabulary from that
  type's namespace, rather than hardcoding predicate IRIs per scheme.
  Properties match by local name too. This tolerates the schemes differing in
  spelling and survives a third being added; the cost is that another
  vocabulary also called `NamedQuery` would be picked up. `QUERY_CLASS_SUFFIX`
  in `named_queries.py` is where to tighten it if that matters.
- **Duplicate ids are reported, not silently resolved.** When both schemes
  register the same id, the `hquery:` definition wins and the shadowed one is
  named in `warnings`. A duplicate id is a registry bug and should be visible.

### Parameters are a security boundary

Every caller-supplied value passes through `params.render_term`, which emits
a complete escaped SPARQL term or raises. Datatypes come from the registry's
declarations, never inferred from the Python type of the argument — a query's
behaviour should not depend on how a caller happened to type a value.

`xsd:dateTime` without a timezone is refused outright. Comparison against
timezone-qualified values is indeterminate within ±14 hours, so a
recent-window filter silently returns nothing while a distant one works.
Guessing UTC on the caller's behalf would hide that; the error explains it.

### The comment stripper is a scanner, not a regex

Classifying a query means finding the first keyword past the prologue, which
means stripping comments. Nearly every RDF namespace ends in `#`, so a regex
that cuts at the first `#` truncates `<...XMLSchema#>` and everything after
it on that line, turning a valid update into an unclassifiable fragment. The
scanner in `sparql_kind.strip_comments` tracks IRI references and literals,
and distinguishes `<` as an IRI opener from `<` as a less-than operator.

### Rule write modes, and why Sync exists

A rule is a stored CONSTRUCT plus a target graph and a write mode.

- **Append** adds the derived triples. Nothing is removed, so a triple the
  rule no longer derives stays behind for good.
- **Replace** makes the target exactly this rule's output. Anything else in
  that graph is destroyed — a target shared with another writer is the wrong
  target for this mode.
- **Sync** reconciles: insert what is newly derived, remove what the rule used
  to derive and no longer does, leave everything else alone. This is the mode
  that makes a rule safely re-runnable.

Sync deletes before it inserts, and both halves are `FILTER NOT EXISTS`
against the other graph, so a triple that is still derived is never removed
and re-added.

**`Replace` could silently drop a triple — diagnosed and fixed
([#12](https://github.com/kurtcagle/holon-bridge-python/pull/12) +
[#14](https://github.com/kurtcagle/holon-bridge-python/pull/14)).** A rule
whose CONSTRUCT yields three triples had been observed landing only two under
`write_mode=Replace`, reporting `triplesAdded: 3` with no error. **Root cause:**
the `COPY` was assembled as `DROP SILENT` + `ADD SILENT` sent as *two separate
HTTP update requests* (PR #12's instrumentation split it that way to make the
intermediate state observable), which gave up the single-transaction atomicity
a one-request `COPY` gets from Jena. Under **concurrent** `Replace` executions
against the same target graph, the two requests interleave: one execution's
`DROP` lands between another's `DROP` and `ADD`, discarding triples the second
execution had already (correctly, at the time) reported as added. Sequential
firing — how it was originally tested, 300× — can't race with itself, which is
why it went undiagnosed. **Fix (#14):** `DROP SILENT` and `ADD SILENT` are sent
as one `;`-separated SPARQL Update request, executed as one Jena transaction.
Verified against 20 and 60 concurrent executions: clean last-writer-wins, zero
loss. Fixed alongside, worth having independently: `added` used to echo the
scratch count rather than measuring what actually reached the target, which is
what let the discrepancy go unnoticed in the first place.

**Nothing is parsed in-process.** The CONSTRUCT result is fetched as Turtle
and pushed straight into a scratch graph — out of Jena and back into Jena,
never through rdflib — and every write mode is then a server-side graph
operation over that scratch graph. Turtle 1.2 output survives intact, and
triple counts come from `COUNT(*)` rather than from counting lines. The
scratch graph is dropped in a `finally`, including when the CONSTRUCT times
out.

`$this` is bound with a trailing `VALUES` clause rather than pasted in
textually. SPARQL admits a `ValuesClause` after a `ConstructQuery`, so the
binding cannot corrupt a well-formed rule, and an unbound rule keeps its "run
over every focus node" behaviour. Other parameters use `{{placeholder}}`
substitution through the same rendering path as everything else.

`/named-rules/run` is a single pass, not a fixpoint. Rules that state an
`order` run first, in it; rules that state none — or state one that will not
parse — run afterwards, so a misconfigured rule cannot preempt correctly
configured ones. A self-feeding rule such as a transitive closure needs
calling until `triplesAdded` reaches zero, and that loop is deliberately the
caller's: a non-terminating rule should not be able to hang the bridge.

Non-canonical, as in the Node bridge, pending WG IV alignment.

### Pipelines: one graph per manifest, and an order to run in

A manifest is RDF in the `build:` vocabulary — `Source`, `Stage`, `Target`
nodes joined by `build:dependsOn`. Because it is RDF rather than a build DSL,
change impact is already just `build:dependsOn+`. What a queryable manifest
still lacks is a total order and something that runs it, and that is what this
module adds.

Each manifest gets its own graph at `urn:{dataset}:pipeline:{id}`, indexed in
`urn:{dataset}:pipelines`. One manifest per graph makes it replaceable and
droppable on its own and removes any question about which triples belong to
which pipeline.

Ordering is Kahn's algorithm over `dependsOn`, with `build:order` breaking
ties among stages that are ready at once and stages declaring no order going
last — the same rule as named rules. **A cycle raises.** Kahn's algorithm
naturally just stops when it hits one, so the tempting implementation returns
a short list and runs a partial pipeline silently; the error names the stages
involved instead. Registration reports an unrunnable manifest immediately
rather than at first run.

**The bridge does not pretend to run what it cannot.** `sparql` stages
execute a named rule, `shacl` stages validate. `llm`, `human`, `external`,
`composite`, and `xslt` stages are recorded as `Deferred` with a reason. A
stage marked Completed because nothing happened would be worse than useless.
Where a stage and its rule disagree about the target graph, the manifest wins:
the stage says where output belongs *in this pipeline*, which may differ from
the rule's standalone default.

### Messages, and why they live in the graph

Ingest and pipeline runs return a message id immediately and the caller polls
`/message/{id}`. That only works if the record outlives the request, so
`hb:Message` records are written to `urn:{dataset}:messages` rather than kept
in memory. A restart mid-run leaves a message stuck in `Running`, which is
honest — better than a status that vanishes with the process.

Two Python-specific hazards are handled explicitly:

- **Background tasks are held in a set on app state.** `asyncio` keeps only a
  weak reference to a running task, so a task nobody holds can be collected
  mid-flight. The run would simply stop, leaving a `Running` message and no
  error to explain it.
- **`Conn` is frozen**, so capturing it for a background task is sound by
  construction. The Node bridge had to capture `req.conn` into a local before
  `setImmediate` because the request is not safe to touch once the response
  has gone out; here there is no request to go stale.

Ingest runs under the same validation gate as `/graph/push`, deliberately —
a second write path that skipped the gate would make arming it meaningless.

### The scheduler, and the bug that shaped it

A firing passes three gates — status, persona capability, ODRL daily cap —
and writes a provenance record **whichever way it goes**. Provenance is not a
success log: a rejected firing that leaves no trace is indistinguishable from
a scheduler that never ran.

Rate limits are counted from provenance rather than from memory, so a cap
survives a restart. The count only includes `committed` and `read-only`
outcomes: a firing refused by a gate must not consume the allowance that
refused it, or one rejection permanently costs a slot.

**Three things are structural rather than careful.**

*Every query names its graph.* A policy lookup written without a `GRAPH`
wrapper reads the default graph, matches nothing, reports "no limit", and
disables rate limiting entirely — with no error, and clean logs. Nothing about
that query is invalid; it just asks the wrong place. So every scheduler query
is built through `vocab.graph_query`, which refuses an empty graph, and there
is a test asserting no query escapes the store without a `GRAPH` clause. It is
a structural test because the failure had no behavioural signature.

*An unreadable policy fails closed.* "No policy declared" is unlimited; "a
policy is declared but will not resolve" raises `PolicyUnresolvable` and the
firing is rejected. Collapsing those two into a nullable return is what let a
broken query read as permission. A rate limiter that fails open is not a rate
limiter.

*Units come from the property name.* `sched:intervalMs` is milliseconds,
`sched:intervalSeconds` is seconds. Inferring the unit from magnitude would
silently reinterpret a legitimately long interval, and a scheduler firing a
thousand times more often than asked is a bad way to find out.

**Two connections, never one.** The scheduler reads its configuration through
the admin dataset and acts through the dataset a task names in
`sched:datasetScope`. Conflating them puts task output in the admin dataset.
Every `/scheduler/*` route is pinned to admin regardless of the caller's
`X-Dataset-Override`, and says so in the response — one scheduler per process
means one registry and one provenance trail, and a per-caller view of either
would be a fiction.

**Recursion has two guards.** A task already in flight is skipped rather than
recorded, which is the cross-tick half: a task whose action indirectly
triggers itself would otherwise queue firings faster than they retire until
the process dies. The within-firing half is that nothing in the execution path
calls `fire` again.

**Proposals are never written straight through.** An `LLMInvocation` returns
Turtle plus a one-line summary; the Turtle is validated, and anything that
fails is quarantined with its text intact so it can be inspected rather than
lost. With no proposer configured the outcome is `deferred`, not `committed` —
recording success for a firing that produced nothing would corrupt both the
provenance trail and the rate-limit count derived from it.

`since` on `/scheduler/activity` must carry a timezone. An unqualified
`xsd:dateTime` compares indeterminately against qualified stamps within ±14
hours, so a recent window returns nothing while a distant one works. The
bridge refuses the value rather than returning a misleading empty list.

**Live findings, not yet fixed.** Reading real provenance on a running
scheduler — rather than exercising it with test doubles — surfaced three
issues, none visible from the unit suite:

- Every quarantined `LLMInvocation` firing traced back to the proposal being
  *truncated mid-statement*, not to malformed RDF from the persona. The
  quarantine design is what made this diagnosable at all — the raw text is
  kept rather than discarded on parse failure — but the reported error
  (a Turtle syntax error) pointed at the wrong culprit entirely.
  [issue #2](https://github.com/kurtcagle/jena-bridge-python/issues/2).
- A task whose preconditions can never be met — a scheduled invocation
  asking for a session summary with no session to summarise — has no way to
  abstain. It can only quarantine or, once #2 is fixed, start committing
  placeholder writes. There's no first-class "nothing to do here" outcome.
  [issue #3](https://github.com/kurtcagle/jena-bridge-python/issues/3).
- Provenance records arrive in pairs roughly 0.4s apart on every firing,
  `lastFired` is never populated, and `triggerType` reports raw blank-node
  labels (`b0`..`b6`) instead of a resolved trigger type. The duplicate
  records matter beyond cosmetics: the ODRL daily cap is counted from
  provenance, so if one firing genuinely writes two records, every capped
  task is consuming its allowance twice as fast as configured.
  [issue #4](https://github.com/kurtcagle/jena-bridge-python/issues/4).

### Projection hooks — proposed, not ported

Everything above this section is a port. This is not: the Node bridge has
`HB_PROJECTION_HOOK_ARCH` logged as a design, not built. So the shape here is
a proposal against your stated constraints — graph authoritative, targets
subscribe, each configured separately, invoked through an output trigger
rather than built in natively — and the design decisions below are yours to
overrule. The `proj:` namespace in particular is my invention.

**The bridge never learns SQL.** A hook declares a scope (a CONSTRUCT, or a
registered named query), a target string it treats as opaque, and how the
target wants change expressed. It computes triples and hands them over. The
target does the transforming — which is what makes a second and third target
type tractable, and what keeps Postgres MCP's own maintained functionality
from being reinvented here.

**Retraction handling is the part people forget, so it is the design centre.**
Each hook keeps the last slice it successfully delivered in a watermark graph,
`urn:{dataset}:projection:{id}`. Additions are in the fresh slice and not the
watermark; retractions are the reverse. Both are computed server-side with
`FILTER NOT EXISTS`, so nothing passes through a local parser and Turtle 1.2
survives. It is the same delta machinery as the `Sync` rule write mode.

**The watermark only moves on a settled delivery.** That is the entire retry
story: a failed or unacknowledged delivery leaves it where it was, so the next
run re-derives exactly the same difference. Delivery is at-least-once, which
is why `keyPredicate` exists — a target that cannot tolerate a repeat should
key its writes.

For a `pull` hook the scratch graph survives until acknowledgement rather than
the delta being recomputed at ack time. The watermark then advances to exactly
what was handed over, even if the graph moved on in between. The cost is that
an abandoned pending delivery leaves a scratch graph behind, which is what
`/projection/sweep` reclaims — settling the delivery as failed and dropping
the graph. Sweeping is safe to run often and safe to run too eagerly: it never
touches the watermark, so a swept delivery's difference is simply offered
again on the next run. It also drops scratch graphs with no delivery behind
them at all, which is what a crash between creating the graph and recording
the delivery leaves. Schedule it with a `maintenance: projection-sweep` task.

**Change mode is per hook, never global**, because whether history is
preserved is a property of the target:

| Mode | Sends | For |
|---|---|---|
| `append` | additions only | append-only logs; retractions are meaningless |
| `upsert` | additions keyed, retractions as deletes | current-state tables |
| `soft-delete` | retractions as tombstones | targets that keep history |
| `replace` | the whole slice each time | targets that cannot do partial updates |

**Two delivery modes, and only one of them is code here.** `webhook` POSTs the
envelope. `pull` queues it for a target to collect and acknowledge — which is
how an MCP-based Postgres agent or an XSLT processor participates without the
bridge knowing anything about either.

This also closes the two loose ends from the previous slices. A pipeline stage
with an `external` transformer that names a hook now executes instead of
deferring, and a scheduler task can name a hook as its action. Both stop at
the same boundary and now cross it the same way. A stage or task that names
*no* hook still defers — the honest outcome when there is genuinely nothing to
hand over.

### Three guards against runaway firing

They catch different shapes of the same problem, which is why there are three
rather than one.

**In flight.** A task still running is skipped, not queued. Without this a slow
task on a fast tick accumulates overlapping firings.

**Depth.** A firing reached from inside another firing carries a depth, and
past `SCHEDULER_MAX_FIRING_DEPTH` it is refused and recorded. Nothing in the
current action set nests, so this is installed ahead of the feature that needs
it rather than after the incident — it is the stated precondition for
`StateTrigger` and subscriptions.

**Per pass.** Within one tick a task fires at most once at top level and cannot
be reached again through another task's action. This is the one the in-flight
check misses, and it was the gap worth closing: A triggering B triggering A
involves no task re-entering *itself*, so nothing is ever simultaneously in
flight and the cycle simply runs. Manual and top-level firings are never
blocked by it.

### Proposals: deferred, failed, and quarantined are three different things

An `LLMInvocation` task can end four ways, and collapsing any of them loses
information that matters:

| Outcome | Means |
|---|---|
| `deferred` | no proposer configured — nothing was attempted |
| `failed` | the proposer tried and errored — a rate limit, a bad key, a timeout |
| `quarantined` | something came back but did not validate, or no Turtle could be recovered from it |
| `committed` | validated and merged |

The first two were originally one branch, which meant a broken persona read as
a configuration choice. An unparseable reply is quarantined **with its raw
text**, because unreadable output is the most informative thing there is about
a misbehaving prompt.

The persona returns a one-line summary alongside its Turtle. The summary is
stripped before validation — left in the payload it is a parse error, and a
parse error there quarantines a proposal that was actually fine — and it ends
up in the provenance record, never in the graph.

Grounding does more for proposal quality than prompt wording: the proposer
shows the persona the SHACL shapes its output must satisfy and the predicates
already in use in the target graph. A persona shown nothing invents a
vocabulary and gets quarantined.

### Compare-and-set minting

The mint UPDATE fires only if the counter still holds the value that was
read, then reads back to confirm. Two concurrent minters cannot both claim
a number; the loser retries. Counters live in `urn:{dataset}:sequences`,
which is the same convention point as everything else.

### Literal escaping

Everything the bridge writes on a caller's behalf goes through
`turtle.escape_literal`. An unescaped quote or newline in generated Turtle
produces a write that fails quietly and keeps failing.

### One shared `.env`, loaded once, before anything reads it

`holonbridge` and `holonbridge_mcp` used to have the same silent gap:
neither actually called `load_dotenv()` anywhere. A `.env` sitting next to
either process did nothing at all — every setting came from whatever was
already in the real process environment, and `Copy-Item .env.example .env`
looked like a complete setup step while quietly being one.

`holonbridge/envfile.py` fixes this with one loader both processes call,
rather than two separate `.env` conventions to keep in sync. Discovery is
`HOLONBRIDGE_ENV_FILE` (an explicit path, for a launcher that doesn't run
from the project directory — Claude Desktop's config is exactly this case)
falling back to `.env` in the current working directory (what `cd` into the
project and just run either command gives you for free). A real environment
variable always wins over the file — `python-dotenv`'s own default — which
is what lets a one-off `$env:X = ...` override still work without editing
anything.

**It has to load before the first `os.getenv()` call, not merely before
`main()`.** `holonbridge_mcp/server.py` reads its constants — `BRIDGE_URL`,
`BEARER`, `ANTHROPIC_KEY` — at module level, which run the instant the
module is imported, including via a direct `python -m holonbridge_mcp.server`
that never goes through `__main__.py` at all. So the load call sits at the
top of that file, and at the top of `holonbridge/config.py`, rather than
inside `main()` where it would arrive one step too late for either entry
point's own settings. It's idempotent — a second call in the same process is
a no-op — so both files can call it unconditionally without coordinating who
goes first.

**An explicit path that doesn't resolve fails loudly.** Silently falling
back to CWD discovery when `HOLONBRIDGE_ENV_FILE` is set but wrong just
relocates a typo into a confusing "why isn't my token set" several steps
downstream. A missing `.env` in the *implicit* case is fine and says
nothing — plenty of setups supply real environment variables directly and
have no file at all — but an explicit path is a promise, and a broken one
should say so immediately.

### Credentials: one token, one role, and claiming a dataset

A token to the REST bridge is exactly one of three kinds, and the bridge refuses
to start if one string is configured as two of them:

| Kind | Configured as | What presenting it means |
|---|---|---|
| legacy | `BEARER_TOKEN` | Shared secret. The caller names any identity in the `x-holon-animus-id` header, so the header proves nothing. |
| identity | `IDENTITY_TOKENS=token=name,…` | The token *is* that identity; a header naming anyone else is a 403. |
| proxy | `PROXY_TOKENS=token,…` | A trusted proxy (the MCP remote, after OAuth) that has verified the caller and asserts the identity in the header. |

Startup also refuses `ALLOW_LEGACY_BEARER=false` when no identity or proxy token
is configured, since no credential could then authenticate. Identity and proxy
tokens must be at least 24 characters.

The MCP is a proxy, so it cannot present the `BEARER_TOKEN`. It reads its own
token from `HOLONBRIDGE_MCP_TOKEN`, and falls back to `BEARER_TOKEN` only when
that is unset or blank — an existing single-token setup is unchanged. A blank
`BEARER_TOKEN=` in the shared `.env` (the REST side, once it has moved to
proxy/identity tokens) does not hide the MCP's own token.
Remember that a real environment variable beats the file: a machine-level
`BEARER_TOKEN` outranks a blank one in `.env`, so delete it from the
environment instead. With no token at all the MCP sends no `Authorization`
header, which is correct for an open loopback bridge and otherwise a 401
`missing bearer token`; it logs which variable it used (never the value) at
startup, or a warning when there is none.

**Claiming a dataset.** A dataset that exists in Fuseki but has no owner or
admin answers every gated route with 401, because there is no Person who could
authorise anything. `holonbridge-bootstrap-admin` is the operator-side way in;
`DATASET_CLAIMANTS` is the in-band one. It is a comma-separated list of
identifiers; empty (the default) turns the in-band claim off.

- A caller may `POST /admin/dataset/initialise` (MCP tool `initialise_dataset`)
  only if its identity is *established* — an identity token, or a proxy
  asserting one — **and** is on the list. The legacy token and an open
  deployment never qualify. The caller becomes the owner; nothing names anyone
  else.
- Such a caller who calls a gated route on an open dataset gets a typed
  `401 dataset_unclaimed` with the remedy. It is a prompt to ask the person, not
  permission. Anyone else gets the plain 401, so this cannot be used to ask
  which datasets are open.
- Refusals: **403** for an ineligible caller (the same whether or not the
  dataset is open); **404 `unknown_dataset`** when the server does not host the
  dataset (checked after eligibility, so it does not reveal which datasets
  exist; create it first -- see below); **409 `dataset_already_claimed`** when it
  already has an owner or admin. The 409 is audited as a refusal, with its
  reason, so it survives the `denials` audit level. If Fuseki's
  `/$/datasets` admin API is unavailable the 404 check is skipped rather than
  guessed.

**Creating a dataset.** `POST /datasets` (MCP tool `create_dataset`) creates a
new, empty dataset through Fuseki's admin API. It is create-only: the bridge has
no way to drop a dataset, so removing one stays a deliberate step in Fuseki.

- Body: `name` (1-64 characters, lower-case letters, digits, `-`, `_`, starting
  with a letter), `kind` (**required**: `persistent` is TDB2 and survives a
  restart; `memory` loses its contents when Fuseki restarts), and `claim`
  (default true: the caller becomes the owner, so nobody can take the dataset in
  the gap; false creates it and writes nothing, for instance to set up bank
  scoping first), and `shapes` (default true: seed the bundled identity,
  delegation and fluent shapes into the new dataset's shapes graph).
- **SHACL needs a Fuseki restart.** Fuseki's admin API can only create a dataset
  from a fixed template (it no longer accepts an uploaded configuration, which
  could read any file on the server), and the template has no `/shacl` endpoint.
  A SHACL-gated bridge cannot write to such a dataset. So when
  `FUSEKI_CONFIG_DIR` is set, the bridge appends a `shacl` endpoint to the
  configuration file Fuseki wrote for the new dataset
  (`{FUSEKI_CONFIG_DIR}/{name}.ttl`), leaving everything else in it as Fuseki
  wrote it, and the response says `restartRequired: true`. **Restart Fuseki**
  and the endpoint exists. The restart cannot be avoided (Fuseki reads service
  configuration only at startup), so when creating several datasets, create them
  all and restart once. The response's `shaclEnabled` is read back from
  Fuseki, so it stays `false` until then. Without `FUSEKI_CONFIG_DIR` the
  dataset is still created, and `shaclConfigError` says why it has no endpoint.
  The bundled shapes are seeded at once, so they are in place when it does.
- **In-memory datasets after a Fuseki restart.** Checked live: the dataset comes
  back with its `shacl` endpoint but **empty** — the seeded shapes and the owner
  are gone, and a SHACL-gated bridge then rejects every write to it (400, "shapes
  graph is empty or absent"). `POST /admin/dataset/shapes` (MCP tool
  `seed_dataset_shapes`) puts the bundled shapes back into the active dataset,
  and with `claim: true` makes the caller its owner again, in one call. It only
  fills an **empty** shapes graph (existing shapes are never overwritten, and
  that is reported rather than treated as an error), and it has the same gate as
  creation, `DATASET_CREATORS`. The create response for a `memory` dataset says
  all of this.
- **Snapshot and restore.** `POST /admin/dataset/snapshot` (MCP `snapshot_dataset`)
  saves every graph of the active dataset to `SNAPSHOT_DIR/{dataset}/` — a
  `manifest.json` and one Turtle file per graph — and `POST /admin/dataset/restore`
  (`restore_dataset`) puts them back into an **empty** dataset, ownership and data
  included. Take the snapshot **before** restarting Fuseki: it is a checkpoint,
  not a backup system, and a persistent dataset is the right home for anything that
  must not be lost. Rules: snapshot needs **admin on the dataset** (it reads
  everything in it); restore uses the `DATASET_CREATORS` gate, because an empty
  dataset has no admin, and **never overwrites** (a dataset with content is refused);
  an empty dataset is never snapshotted, so a snapshot taken after a restart cannot
  replace a good one; the audit graph is not counted as content and is merged, not
  replaced, on restore; a snapshot over `SNAPSHOT_MAX_BYTES` (default 50 MB) is
  refused; graphs are stored and restored as text, never parsed, so RDF 1.2 passes
  through. Off unless `SNAPSHOT_DIR` is set.
- Gate: `DATASET_CREATORS`, a comma-separated list, empty by default (which
  turns creation off). Same standard as claiming — an identity token, or a
  proxy asserting an identity, that is on the list; never the legacy token or an
  open deployment — but a **separate list**, because creating a dataset
  allocates storage (and, for `memory`, heap) on a Fuseki every dataset shares,
  which admin on one dataset cannot imply. Being a claimant or an admin is not
  enough.
- Refusals: **403** for an ineligible caller; **400** `bad_dataset_name` /
  `reserved_dataset_name` (`admin` and the scheduler dataset); **409**
  `dataset_exists` (compared case-insensitively, since Windows folds case) or
  `dataset_limit_reached` (`MAX_DATASETS`, default 50, counted from what Fuseki
  reports); **502** `admin_api_unavailable` or `create_failed`. A successful
  creation is always audited, like initialising a dataset, whatever the audit
  level.
- Fuseki's default rule admits `/$/` admin requests only from the same machine,
  so this works as shipped when the bridge and Fuseki share a host. A remote
  Fuseki needs its admin API opened to the bridge, and note the bridge sends its
  bank token as a Bearer header, which Fuseki's default Shiro setup does not
  read.
- **Bank scoping order matters.** A new dataset uses `urn:{dataset}:{role}`
  unless it is already in the bank's `bankScopedDatasets`. To have
  `urn:{bank}:{dataset}:{role}`, add it to `config.json` and reload *before*
  creating it (or create with `claim=false` and do so before its first write);
  identity written under one convention is orphaned when the setting changes.
  The response reports `bankScoped` and the graph IRIs.

---

## MCP layer

`holonbridge_mcp` speaks to the REST API over HTTP like any other client, so
there is one authorisation path and one validation path rather than two that
drift.

Forty-nine tools, in nine groups: endpoints and profiles; raw SPARQL;
graphs, push, and validation; datasets; the named-query registry; the
named-rule registry with `graph_op`; pipelines, ingest, and messages; the
scheduler; and projection hooks. `get_holon` and `mint_sequence_id` sit
alongside.

The scheduler tools always target the admin dataset, whatever dataset the
session is otherwise using.

```json
{
  "mcpServers": {
    "holonbridge-py": {
      "command": "C:\\path\\to\\holon-bridge-py\\.venv\\Scripts\\python.exe",
      "args": ["-m", "holonbridge_mcp.server"],
      "env": {
        "HOLONBRIDGE_ENV_FILE": "C:\\path\\to\\holon-bridge-py\\.env"
      }
    }
  }
}
```

Claude Desktop does not launch from the project directory, so the implicit
`.env`-in-CWD discovery that works from a shell doesn't apply here —
`HOLONBRIDGE_ENV_FILE` is the one line that points it at the same shared
file `holonbridge` itself uses, instead of duplicating `BEARER_TOKEN`,
`HOLONBRIDGE_URL`, and everything else into this block by hand. The older
shape still works if you'd rather keep this config self-contained:

```json
      "env": {
        "HOLONBRIDGE_URL": "http://localhost:3031",
        "BEARER_TOKEN": "<your-token>",
        "HOLONBRIDGE_DATASET": "bridgerton",
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
```

Anything set directly in this block still wins over the shared file — a
launcher's own `env` is real environment, and real environment always beats
`.env`.

`HOLONBRIDGE_DATASET` sets `X-Dataset-Override` on every call, so a second
MCP entry pointed at a different dataset needs no second bridge process.

### Remote transport, for claude.ai

stdio only works where the client can launch a child process — Claude Desktop
and Claude Code. claude.ai connects from Anthropic's servers, not from your
machine, so it needs a URL:

```powershell
$env:MCP_INBOUND_TOKEN = [Convert]::ToBase64String(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32))

python -m holonbridge_mcp --transport sse --port 3032
ngrok http --url=your-subdomain.ngrok.io 3032
```

Then add the ngrok URL plus `/sse` as a custom connector, with the token as an
`Authorization: Bearer` request header.

**The remote transport refuses to start with no credential configured,** and
that is the point. stdio needs no authentication because only the process that
launched it can talk to it. A tunnelled endpoint is reachable by anyone who
finds the URL, and this server *holds* the bridge's bearer token — so an
unauthenticated caller inherits `sparql_update` and `push_turtle` without ever
seeing a credential. The bridge's own token protects `:3031`; it does nothing
for `:3032`.

The gate is raw ASGI middleware rather than Starlette's `BaseHTTPMiddleware`,
which buffers responses and would quietly turn the SSE stream into a single
blob.

**The MCP SDK's own DNS-rebinding protection has to be told about the
tunnel.** It defaults to on with an empty `allowed_hosts`, meaning localhost
only — correct for a server reached directly on 127.0.0.1, and exactly wrong
behind ngrok, which forwards with the public hostname in `Host`. The symptom
is a `421 Misdirected Request` and a `ValueError: Request validation failed`
raised from inside the SSE handler, *after* the whole OAuth flow has already
succeeded — which makes it read as an auth failure when authentication was
never the problem. The allowlist is derived from `MCP_PUBLIC_URL`, which the
OAuth layer already requires, so there's nothing extra to configure for the
ordinary case; `MCP_ALLOWED_HOSTS` (comma-separated) covers a second tunnel,
a reverse proxy, or a custom domain. Protection stays enabled either way —
this widens the allowlist, it does not switch the check off, and a `Host` not
on the list still gets 421.

Three ports now, and only one of them should ever be public:

| Port | What | Exposed |
|---|---|---|
| 3030 | Fuseki | never |
| 3031 | HolonBridge REST | only if you want direct HTTP clients |
| 3032 | MCP remote transport | via ngrok, for claude.ai |

### GitHub OAuth, as a second credential kind

`MCP_INBOUND_TOKEN` is one shared secret — anyone holding it has full access,
and every request looks the same in provenance. Setting
`GITHUB_OAUTH_CLIENT_ID` adds a second, additive credential kind: a
GitHub-identified session token, so a caller is an allowlisted login rather
than an anonymous holder of a string. Both can be configured at once; neither
is required to enable the other.

```powershell
$env:GITHUB_OAUTH_CLIENT_ID     = "<from a GitHub OAuth App>"
$env:GITHUB_OAUTH_CLIENT_SECRET = "<from the same App>"
$env:MCP_PUBLIC_URL             = "https://your-subdomain.ngrok.io"
$env:MCP_JWT_SECRET             = [Convert]::ToBase64String(
  [Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
$env:MCP_ALLOWED_GITHUB_LOGINS  = "kurtcagle"

python -m holonbridge_mcp --transport sse --port 3032
```

Register the OAuth App at github.com/settings/developers first — Homepage
URL is whatever you like, **Authorization callback URL must be
`<MCP_PUBLIC_URL>/callback/github`** exactly, since that is the only redirect
GitHub will accept back from this flow.

**Two OAuth flows are layered here, not one.** Claude's MCP client speaks
ordinary authorization-code-with-PKCE OAuth to this server, preceded by
Dynamic Client Registration (RFC 7591) — that is flow 1, and this server is
its authorization server. `/authorize` bridges into a second, ordinary OAuth
exchange with GitHub, used purely to answer "which human is this" — that is
flow 2, and it is invisible to the MCP client, which only ever sees flow 1
complete. Getting this distinction wrong is exactly where the earlier Node
implementation spent most of its debugging time (dynamic client registration
arriving unhandled, `code` vs `access_token` confusion in the redirect,
well-known routes landing behind the auth gate) — this port implements the
full shape from the start rather than the "static token dispenser that
speaks OAuth's vocabulary" the Node side started from, though live
connector-flow debugging against claude.ai's exact client behaviour is still
likely, the same way it was there.

**Scope is identity-only.** `read:user` is all that is requested, and
GitHub's own access token is discarded the moment `login` has been read — it
is never stored, logged, or held past that one call. This server does
nothing on a user's behalf on GitHub; it only asks who they are.

**`MCP_ALLOWED_GITHUB_LOGINS` is required, with no permissive default.** An
OAuth layer that establishes identity but authorizes anyone with a GitHub
account is a worse default than the single shared token it's meant to
improve on — so setting `GITHUB_OAUTH_CLIENT_ID` with an empty or absent
allowlist refuses to start, the same way an unset `MCP_INBOUND_TOKEN` always
has. Comma-separated, case-insensitive.

**Only S256 PKCE is accepted.** `plain` is legal per spec for constrained
clients but weaker, and every MCP client encountered so far uses S256, so
there is no compatibility reason to accept it. `/authorize` rejects anything
else before it ever reaches GitHub.

**Session tokens are stateless JWTs**, signed with `MCP_JWT_SECRET`,
`sub`=GitHub login, default 12h lifetime (`MCP_JWT_TTL_SECONDS`). Verification
is signature and expiry only — a login removed from the allowlist mid-session
is not re-checked until its token expires, which trades a short TTL for not
needing a lookup on every request. `/token` also issues a **refresh token**
(`MCP_REFRESH_TTL_SECONDS`, default 30 days, renewed at each use): opaque,
stored only as a SHA-256 hash, rotated on every use, and a replayed one revokes
its whole family. The allowlist is re-checked at every refresh, so a removed
login stops working within one `MCP_JWT_TTL_SECONDS`, which can therefore be
kept short. Set `MCP_DATA_DIR` so client registrations and refresh tokens
survive a restart (SQLite, single host); unset, they live in memory and a
restart forces re-registration and re-authorisation.

**The well-known/register/authorize/token endpoints are reachable with no
credential at all**, deliberately — an OAuth authorization server that
requires a token to reach `/authorize` cannot issue its first one. This is
the one place `BearerGate`'s open-path list is more than an afterthought:
during development, the first version of this made those paths bypass auth
by returning the *health-check's own canned response* instead of forwarding
to the real handler — passable-looking but wrong every time, since the
handlers never ran at all. Fixed, and there's a test
(`test_oauth_metadata_is_reachable_with_no_credential_at_all`) guarding
specifically against that shape of regression, not just against the 401.

**They also have to be open when GitHub OAuth is *not* configured at
all — not just when it is.** A real claude.ai connector, static-token-only,
no GitHub OAuth vars set, hit every one of these paths and got 401 across
the board, before it ever tried the working `MCP_INBOUND_TOKEN` it had
already been given. The client speaks the MCP Authorization spec and probes
OAuth discovery unconditionally, ahead of any credential it already holds;
a 401 on a discovery endpoint reads as "authenticate to find out how to
authenticate," which stalls a client that never needed OAuth in the first
place. Fixed: these paths are exempt from the gate regardless of whether
GitHub OAuth is configured. Unconfigured, there's genuinely no route behind
them, so exempting them just lets the request fall through to the transport
Mount's own 404 — the correct "not supported here" signal, and nothing
about that response is sensitive to reveal.

**`/.well-known/oauth-protected-resource` also has to be served at a
path suffixed with the resource's own — `/sse` here — not only the bare
path.** RFC 9728 §3.1 allows this; the same live connector trace showed it's
not optional in practice; it specifically requested
`/.well-known/oauth-protected-resource/sse` and nothing at the bare path
would have answered that even once GitHub OAuth was configured, since the
route for the suffixed variant didn't exist at all. Same handler, same
content, both paths — the suffix is only where the client looked, not a
different resource description. `/mcp` is registered too, for
`streamable-http`.

`nl_query` samples the store's classes and predicates first, so the model
writes against terms that actually exist, and always returns the generated
query alongside the results for inspection.

---

## Profiles

`~/.holonbridge/config.json`, or set `HOLONBRIDGE_CONFIG`. See
`examples/config.json`. A `local` profile is always synthesised from the
environment, so the bridge starts cleanly with no config file present.

---

## Tests

```powershell
python -m pytest -q
```

The backend is stubbed at the `FusekiClient` boundary, so the suite exercises
auth, override handling, endpoint guards, the delta gate, holon projection,
registry loading, both binding strategies, all three rule write modes,
topological ordering, ingest, message persistence, every scheduler gate, and
the projection watermark — without a live Fuseki. The GitHub OAuth layer adds
its own suite on top, with GitHub's two endpoints monkeypatched at the only
two points this codebase ever calls out to them — PKCE, the allowlist,
one-time codes, JWT verification, and the composed app's routing (open OAuth
paths, gated transport path, both credential kinds), and the shared `.env`
loader (both discovery paths, real-environment precedence, the loud failure
on a bad explicit path), and the transport-security allowlist (the tunnel
hostname derived from MCP_PUBLIC_URL, extra hosts declared explicitly, and
localhost-only when no public URL is set). Dataset listing has its own
suite against a stubbed Fuseki admin response, including the typo case — a
switch to a nonexistent dataset must fail loudly rather than silently
emptying. The write-mode-aware SHACL fix has structural tests asserting
*which graph gets copied*, since in a stubbed world the replace-as-merge bug
has no behavioural signature — the stub replays scripted reports rather
than running SHACL, so only the `COPY` call itself distinguishes correct
from incorrect. Two hundred and sixty tests total, no network — though it's
worth being direct about what that number does and doesn't mean: none of
the SHACL bugs above, nor the four issues in the scheduler notes, were ever
caught by this suite. Every one of them surfaced from running the bridge
against real data.

---

## Known issues

Originally four, all filed with full repro detail; **#1 is now fixed** (see
below). Linked inline above at the point each is most relevant; consolidated
here for anyone doing a fast pass over what's currently open rather than
reading the design notes end to end.

| # | What | Severity |
|---|---|---|
| ~~1~~ | Named rule `write_mode=Replace` could silently drop a triple while reporting success | **Fixed** — [#12](https://github.com/kurtcagle/holon-bridge-python/pull/12) (instrumentation) + [#14](https://github.com/kurtcagle/holon-bridge-python/pull/14) (COPY atomicity restored: the split DROP/ADD, run as two HTTP requests, interleaved under concurrent `Replace`; recombined into one SPARQL transaction) |
| [2](https://github.com/kurtcagle/jena-bridge-python/issues/2) | Scheduler LLM proposals truncate mid-statement, quarantining as a misleading Turtle syntax error | Medium — misdiagnosable, not data-lossy |
| [3](https://github.com/kurtcagle/jena-bridge-python/issues/3) | A task with unmeetable preconditions has no way to abstain; fixing #2 alone turns silent failure into silent noise | Design gap |
| [4](https://github.com/kurtcagle/jena-bridge-python/issues/4) | Scheduler: duplicate provenance per firing, `lastFired` never populated, `triggerType` leaks blank-node labels | Medium — affects ODRL cap accuracy |

`session-memory-delta-chloe-v2`, the one Active scheduled task in the
instance these were found on, is suspended pending #2/#3.

## Next slices

In the order that yields the most per unit of work:

1. ~~Diagnose issue #1.~~ **Done (#12 + #14).** The write path being fully
   trustworthy was the gate on everything below — in particular, rule-output
   SHACL validation against the scratch graph, which that bug showed did not
   always faithfully reach the target. That gate is now clear.
2. A first real target, to test the hook contract against something that
   pushes back. A Postgres subscriber on `pull` would exercise `upsert` and
   `keyPredicate` properly, and the RDF-to-SQL mapping question you logged —
   no standard direction, unlike R2RML the other way — is easier to answer
   with one concrete target in hand than in the abstract.
3. A live proposal, once issue #2 is fixed and #3 is resolved one way or
   the other. The parsing and quarantine paths are well exercised now — by
   a real, if unintended, two-day production failure — but the prompt
   itself has never successfully met a model on this codebase.
4. `routes/dataset_admin.py` — create and drop. Listing and switching
   shipped and are tested live (see "Dataset listing" above); create/drop
   were withheld deliberately, pending the authenticated `sub` claim the
   OAuth layer already carries actually reaching route-level authorization.
5. `sched:Subscription` over `StateTrigger`. The recursion guards it was
   waiting on are in place now, including the per-pass cycle check, so this
   is unblocked — but it wants the guards proven against a real subscription
   before being trusted, and probably wants issues #2–#4 settled first
   given they're in the same module.

Registry writes (registering and deleting a query through the API) are
deliberately not here: the registry is a DataBook artefact, so `push_turtle`
into `urn:{dataset}:named-queries` plus `reload_named_queries` is the honest
path until there is a reason for a dedicated write route.
#