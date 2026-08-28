# create_holon, create_message, named_query_schema

Added 2026-08-28. Three additions to the REST API, all built on the existing
auth/ACL/SHACL machinery rather than beside it. This document covers what
each one does, how to call it, and — because two of the three collide in
name with something that already exists — what each one is *not*.

All three require the same headers as every other identity-gated route:

```
Authorization: Bearer <BEARER_TOKEN>
X-Holon-Animus-Id: <your external identity, e.g. GitHub login>
X-Holon-Animus-Type: GitHubIdentity   # default; omit unless you use a different scheme
```

---

## `POST /holon` — create_holon

Creates or merges into a holon from a **DataBook message** rather than raw
Turtle. This is `databook push` (extract the RDF block, resolve a target
graph, validate, write) exposed as a bridge-native operation, so a client
doesn't need the DataBook CLI installed to do it — send the whole
`.databook.md` text and the bridge does the extraction itself.

It is a thin wrapper around the exact same path `POST /graph/push` uses
(`holonbridge.ingest.write_turtle_to_graph`): same ACL check
(`grantsWrite`/`grantsReplace`), same SHACL gate, same GSP write. It does
not mint identity — the DataBook's own Turtle/JSON-LD block has to declare
whatever subject IRIs the holon needs, same as any other push. Mint a
fresh IRI first via `POST /sequence/mint` if you need one.

### Request

```jsonc
POST /holon
{
  "databook": "<full DataBook markdown text>",
  "block_id": null,              // optional: select a specific databook:id block
  "graph_iri": null,             // optional: overrides the DataBook's graph.named_graph
  "shapes_graph": null,          // optional: defaults to conn.shapes_graph if SHACL_REQUIRED
  "mode": "merge",               // "merge" | "replace"
  "reduction_rule_id": null      // optional: see holonbridge.shacl._apply_reduction
}
```

The block matched is the DataBook's first `turtle`, `turtle12`, or
`json-ld` fence (or the one named by `block_id`). A `json-ld` block is
converted to Turtle in-process before it reaches Fuseki — the write path
is always `text/turtle` regardless of which serialisation you sent.

**Graph resolution order:** `graph_iri` in the request body, then the
DataBook's own `graph.named_graph` frontmatter, then a `400` if neither is
present. There is no default graph for `create_holon` — unlike
`create_message`, a holon with no declared home is much more likely to be
a mistake than an intentional choice.

### Example

```bash
curl -X POST https://your-bridge/holon \
  -H "Authorization: Bearer $BEARER_TOKEN" \
  -H "X-Holon-Animus-Id: kurtcagle" \
  -H "Content-Type: application/json" \
  -d @- <<'EOF'
{
  "databook": "---\nid: urn:databook:example:observatory-a\ntitle: \"Observatory A\"\ntype: databook\nversion: 1.0.0\ncreated: 2026-08-28\ngraph:\n  named_graph: https://example.org/graphs/observatories\n---\n\n<!-- databook:id: primary-graph -->\n```turtle\n@prefix ex: <https://example.org/> .\nex:ObservatoryA a ex:Observatory ;\n    ex:name \"Observatory A\" .\n```\n"
}
EOF
```

### Response

```jsonc
{
  "ok": true,
  "graph": "https://example.org/graphs/observatories",
  "dataset": "causalspark",
  "mode": "merge",
  "validated": true,
  "validation": { "conforms": true, "mode": "delta", "violations": 0, "warnings": 0, "results": [], "report": "..." },
  "sourceBlock": "primary-graph",
  "sourceLang": "turtle"
}
```

Errors: `400` (unparseable DataBook, no RDF block found, no graph
resolvable), `401` (unresolved identity), `403` (no `grantsWrite`/
`grantsReplace` for the target graph), `422` (blocking SHACL violation —
`sh:Warning`/`sh:Info` never block, only `sh:Violation` does), `502`
(Fuseki failure).

---

## `POST /message/create` — create_message

**Read this before assuming it does what the name suggests.** `create_message`
is unrelated to `holonbridge.messages` / `hb:Message` — the existing
status record for an async pipeline run (`Received → Running →
Completed/Failed`), stored in a graph that happens to also be called
`messages`. That's a coincidence of an earlier, separate naming decision.
`create_message` writes domain-level `hev:AssertionEvent` content into the
dataset's **`events`** graph. Different graph, different vocabulary,
different concept. Nothing here touches `MessageStore`, and nothing in
`messages.py` changed for this to exist. If you're looking for pipeline-run
status, that's still `holonbridge.messages.MessageStore` (no route exposes
it directly today — it's consumed internally by pipeline runs).

### Scope: AssertionEvent submission only

This is the *passive* half of the CommandEvent pipeline sketched in the
`sce` architecture skill (validate → authorise → execute → assert → log →
update → project): a human or persona explicitly records that something
happened. It does not trigger anything. It is also distinct from
`holonbridge/triggers.py`'s condition-driven CommandEvents, which already
exist and fire on their own schedule — `create_message` never invokes a
trigger, a rule, or the scheduler.

Triggering system *action* from a submitted event — the CommandEvent
execution half — is a separate, materially larger piece of design work
(a request that causes a mutation needs a different authorisation posture
than one that only records a fact) and is out of scope here by agreement,
not oversight.

### Request

```jsonc
POST /message/create
{
  "databook": "<full DataBook markdown text carrying hev:AssertionEvent content>",
  "block_id": null,
  "graph_iri": null,        // overrides both frontmatter and the events-graph default
  "shapes_graph": null,     // pass an EventShape graph here to enforce hev:AssertionEvent typing
  "reduction_rule_id": null
}
```

No `mode` field — the write is always a merge. An event ledger is
append-only by nature; the event that already happened doesn't get
overwritten by the next one.

**Graph resolution order:** `graph_iri` in the request, then the
DataBook's `graph.named_graph` frontmatter, then `conn.graph("events")` —
i.e. `urn:{dataset}:events` (or the bank-scoped equivalent). Omitting both
is the common case, not an error.

This route does not itself check that the submitted content types as
`hev:AssertionEvent` — only that it's well-formed RDF. If you want that
enforced, register an EventShape and pass its graph IRI as `shapes_graph`
(or point `SHACL_REQUIRED` at a shapes graph that includes it).

### Example

```bash
curl -X POST https://your-bridge/message/create \
  -H "Authorization: Bearer $BEARER_TOKEN" \
  -H "X-Holon-Animus-Id: kurtcagle" \
  -H "Content-Type: application/json" \
  -d @- <<'EOF'
{
  "databook": "---\nid: urn:databook:example:sensor-fault-event\ntitle: \"Sensor Fault\"\ntype: databook\nversion: 1.0.0\ncreated: 2026-08-28\n---\n\n<!-- databook:id: primary-graph -->\n```turtle\n@prefix hev: <https://w3id.org/holon/event/> .\n@prefix ex: <https://example.org/> .\n@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\nex:event-2026-08-28-0001 a hev:AssertionEvent ;\n    hev:concerns ex:ObservatoryA ;\n    hev:assertedAt \"2026-08-28T15:00:00Z\"^^xsd:dateTime ;\n    hev:summary \"Temperature sensor offline\" .\n```\n"
}
EOF
```

### Response

Same shape as `create_holon`'s, minus `mode` variability (always
`"merge"`):

```jsonc
{
  "ok": true,
  "graph": "urn:causalspark:events",
  "dataset": "causalspark",
  "mode": "merge",
  "validated": false,
  "validation": null,
  "sourceBlock": "primary-graph",
  "sourceLang": "turtle"
}
```

Error shapes are identical to `create_holon`'s.

---

## `GET /named-query/{query_id}/schema` — named_query_schema

Returns a named query's declared parameters as a SHACL `sh:NodeShape`,
for a client that wants to introspect or auto-generate a form for a
query's parameter contract without parsing the query body or any
`databook:param`-style comments.

The shape is generated fresh, on every call, from the same `Parameter`
declarations `apply_query_params` already binds requests against — there
is one source of truth for a query's parameter contract, not a
hand-authored shape that can drift from the real `{{placeholder}}`/`VALUES`
bindings.

Gated exactly like `GET /named-query/{id}` and `POST
/named-query/{id}/run`: it goes through the same toolset-reachability
check, and a query outside the caller's reachable set gets the same `404`
shape as an unknown id. A restricted query's parameter contract is exactly
the kind of thing that would differentially confirm its existence to
someone who can't reach it, so it isn't treated as an exception to that
rule.

### Request

```
GET /named-query/select-by-type/schema
```

No body.

### Response

```jsonc
{
  "id": "select-by-type",
  "iri": "https://example.org/nq/select-by-type",
  "parameters": [
    {
      "name": "entityType",
      "datatype": "IRI",
      "required": true,
      "description": "the class to filter by",
      "default": null
    },
    {
      "name": "limit",
      "datatype": "xsd:integer",
      "required": false,
      "description": "max rows",
      "default": "10"
    }
  ],
  "shapesTurtle": "@prefix sh: <http://www.w3.org/ns/shacl#> .\n\n<https://example.org/nq/select-by-type#ParameterShape>\n    a sh:NodeShape ;\n    sh:targetNode <https://example.org/nq/select-by-type> ;\n    sh:name \"select-by-type parameters\" ;\n    sh:property [\n          sh:path <https://w3id.org/holonbridge/param/entityType> ;\n          sh:name \"entityType\" ;\n          sh:nodeKind sh:IRI ;\n          sh:minCount 1 ;\n          sh:maxCount 1 ;\n          sh:description \"the class to filter by\" ;\n          sh:order 0\n        ] ;\n    sh:property [\n          sh:path <https://w3id.org/holonbridge/param/limit> ;\n          sh:name \"limit\" ;\n          sh:datatype <http://www.w3.org/2001/XMLSchema#integer> ;\n          sh:minCount 0 ;\n          sh:maxCount 1 ;\n          sh:defaultValue \"10\" ;\n          sh:description \"max rows\" ;\n          sh:order 1\n        ] .\n"
}
```

`sh:path` on each property is a synthetic IRI under
`https://w3id.org/holonbridge/param/{name}` — not a real predicate
asserted on any data, just a stable, distinct key per parameter name so a
form-generating client can rely on it across calls.

**Not yet built:** an author-supplied override shape for a query that
needs constraints this flat declaration set can't express (value ranges,
`sh:in` enumerations, cross-parameter rules). The intended extension point
is a shapes block registered alongside the query, preferred over the
derived one when present — flagged here as a known gap, not implemented.

Error shapes: `404` (unknown id, or known but unreachable for the current
persona — same shape either way, see above).

---

## What's deliberately not done here

- **MCP tool parity.** `holonbridge_mcp/server.py` is a single ~55KB file
  and wasn't read in full before this change — wiring these three up as
  MCP tools (`create_holon`, `create_message`, `named_query_schema`)
  alongside the existing 11+ REST-backed tools is the natural next step,
  but doing it without first reading that file end-to-end risks a bad
  edit to a file already carrying real production behaviour. Flagged as a
  follow-up, not silently skipped.
- **CommandEvent execution.** As above — `create_message` is the passive
  half only. The active half (an event triggering a mutation, with its
  own authorisation posture) is a separate design task.
- **AssertionEvent shape enforcement.** `create_message` doesn't require
  or check `hev:AssertionEvent` typing on its own; that's `shapes_graph`'s
  job if you want it enforced. Worth registering an EventShape before
  this sees real traffic from more than one caller.
