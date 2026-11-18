---
id: https://w3id.org/databook/holon-focus
title: "Holon Focus: Avatars, Focus Properties and Resolving a Point of View"
type: databook
version: 0.2.0
created: 2026-11-17
description: >
  How a holon system says what can take a focus, how a focus is resolved to a place and a
  time, and what list_avatars, get_focus and set_focus do in holon-bridge-python. Written for
  the HCG Plenary. Vocabulary is in the holon: namespace and is not ratified.
subject:
  - holon focus
  - avatar
  - point of view
  - holon-bridge-python
graph:
  namespace: https://w3id.org/holon/
  named_graph: https://w3id.org/databook/holon-focus#graph
  triple_count: 17
  subjects: 4
  rdf_version: "1.1"
process:
  transformer: "Claude Sonnet 5.5"
  transformer_type: llm
  transformer_iri: https://api.anthropic.com/v1/models/claude-sonnet-5-5
  inputs:
    - iri: https://claude.ai/chat/3c09f48f-7d63-4b71-a569-ed5111dfe0b4
      role: evidence
      description: "The Murder on the Elizabeth Line model: Jane Doe as default avatar, agent:presentIn and agent:location fluents"
    - iri: urn:input:focus-design-discussion-2026-11-17
      role: constraint
      description: "Kurt Cagle's positions on focus, avatar and ledgers, 2026-11-17, recorded below"
  timestamp: 2026-11-18T03:30:00Z
  agent:
    name: Chloe Shannon
    iri: https://holongraph.com/people/chloe-shannon
    role: orchestrator
---

## Status

The positions below are the chair's working positions as of 17 November 2026, not HCG decisions. The vocabulary is in the `holon:` namespace and is not ratified. **Implemented in holon-bridge-python** (`holonbridge/focus.py`, `holonbridge/routes/focus.py`, `holonbridge_mcp/tools/focus.py`), with tests against rdflib's SPARQL engine, not a live Fuseki. The resolver reads the bridge's own encoding, described in "How the bridge encodes it" below, which differs from the spec-level model that the resolution query in this document was written against.

## Summary

A focus is an **avatar** and, optionally, a **time**. The place is not stored; it is read from the avatar's own fluents as of that time. An avatar is any entity that represents a point of view inside the system: a person, a persona, an account, a company or a character. What can take a focus is declared in the graph by a class (`holon:Avatar`, asserted, never inferred), what locates an avatar is declared on properties (`holon:FocusProperty`, with `holon:focusWithin` giving the order of scale), and shapes validate both. Changing focus changes which avatar's view you read. It creates no holon and writes nothing to a ledger.

## Positions taken

| Question | Position | Reason |
|---|---|---|
| Does a holon need a ledger? | No, unless it has a fluent. An event layer MAY be present and MUST be present if any fluent is. | A fluent's current value is a cache of its latest ledger entry. A holon with no fluents has no state to record, so a static holon is legitimate. |
| Does changing focus change holon? | No. The holon is the system; focus is the viewpoint it is read from. | If focus were the holon, every focus would be a distinct holon with no stable identity, and nothing could hold state once you looked away. |
| Is the place part of the focus? | No. Place is derived from the avatar's fluents at the focus time. | Storing it would create a second fact that can contradict the avatar's own fluents. The Elizabeth model already derives a station from the train it is on, for the same reason. |
| What is an avatar? | A role: any entity that represents the point of view of an observer or actor within the system. | A person, persona, account or company can each play it. It need not be a holon, and may be one. |
| How is "can take a focus" declared? | A class on the avatar, a class on the properties that locate it, and shapes to validate. | See Vocabulary. The declaration has to live in the ontology, because the shapes graph is optional and per dataset. |
| Default and switching | A holon names a default avatar. A caller may switch to any other avatar. | The story is told from the default avatar's point of view, as Jane Doe's is. |

A note on the ledger answer: in holon-bridge-python the lifecycle state of a holon is a fluent, so deactivating a ledgerless holon gives it a ledger. That is an implementation fact, and the spec need not copy it. A ledgerless holon could carry its state as a plain triple.

## Vocabulary

Four terms. `holon:FluentProperty` and `holon:StateAssertion` are used as they appear in the Elizabeth Line model.

<!-- databook:id: focus-vocabulary -->
```turtle
@prefix holon: <https://w3id.org/holon/> .
@prefix rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:  <http://www.w3.org/2000/01/rdf-schema#> .
@prefix sh:    <http://www.w3.org/ns/shacl#> .
@prefix xsd:   <http://www.w3.org/2001/XMLSchema#> .

holon:Avatar a rdfs:Class ;
    rdfs:label "Avatar" ;
    rdfs:comment "An entity that represents a point of view within a holon system: the observer or actor. A person, persona, account, company or character may each be one. It is a role, asserted of the entity and never inferred, and it does not make the entity a holon or stop it being one." .

holon:FocusProperty a rdfs:Class ;
    rdfs:subClassOf holon:FluentProperty ;
    rdfs:label "Focus property" ;
    rdfs:comment "A fluent property whose value, held by an Avatar, locates it: which holon it is in, or where within that holon." .

holon:focusWithin a rdf:Property ;
    rdfs:label "focus within" ;
    rdfs:domain holon:FocusProperty ;
    rdfs:range holon:FocusProperty ;
    rdfs:comment "This focus property is read within the holon named by the value of the other one. A focus property with no focusWithin is a root." .

holon:defaultAvatar a rdf:Property ;
    rdfs:label "default avatar" ;
    rdfs:domain holon:Holon ;
    rdfs:range holon:Avatar ;
    rdfs:comment "The avatar a read follows when the caller names none. At most one per holon." .
```

Two properties of an Elizabeth-style model are then marked as focus properties. `agent:location` is read within the holon that `agent:presentIn` names, so `presentIn` is the root and `location` is one scale down. A model with more scales adds more links in the chain.

```turtle
agent:presentIn a holon:FocusProperty .
agent:location  a holon:FocusProperty ; holon:focusWithin agent:presentIn .
```

**Why this shape.** The class marks the avatar because avatar-ness is a role that someone asserts. It is not inferred, and it does not need a reasoner. The property marks what locates it because the bridge already finds fluent behaviour by looking at the property in the ontology (`fluentOperationMode` is the existing example). `focusWithin` makes the order of scale a statement in the graph, so a tool does not need to know that presence comes before location. If the HCG classifies by Holonitype, "avatar" could move there. That waits on the open/closed terminology ruling.

## Validation

<!-- databook:id: focus-shapes -->
```turtle
@prefix holon: <https://w3id.org/holon/> .
@prefix rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix sh:    <http://www.w3.org/ns/shacl#> .
@prefix xsd:   <http://www.w3.org/2001/XMLSchema#> .

<https://w3id.org/databook/holon-focus#prefixes>
    sh:declare [ sh:prefix "holon" ; sh:namespace "https://w3id.org/holon/"^^xsd:anyURI ] .

# A focus property is only ever asserted of an Avatar.
holon:FocusAssertionShape a sh:NodeShape ;
    sh:targetClass holon:StateAssertion ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "A focus property may only be asserted of an Avatar." ;
        sh:prefixes <https://w3id.org/databook/holon-focus#prefixes> ;
        sh:select """
            SELECT $this ?subject WHERE {
              $this holon:fluent ?p ; holon:subject ?subject .
              ?p a holon:FocusProperty .
              FILTER NOT EXISTS { ?subject a holon:Avatar }
            }"""
    ] .

# A holon has at most one default avatar, and it is an Avatar.
holon:DefaultAvatarShape a sh:NodeShape ;
    sh:targetSubjectsOf holon:defaultAvatar ;
    sh:property [ sh:path holon:defaultAvatar ; sh:maxCount 1 ; sh:class holon:Avatar ] .

# A focus property is read within at most one other focus property, and not within itself.
holon:FocusPropertyShape a sh:NodeShape ;
    sh:targetClass holon:FocusProperty ;
    sh:property [ sh:path holon:focusWithin ; sh:maxCount 1 ; sh:class holon:FocusProperty ] ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "focusWithin must not lead back to the property itself." ;
        sh:prefixes <https://w3id.org/databook/holon-focus#prefixes> ;
        sh:select """SELECT $this WHERE { $this holon:focusWithin+ $this }"""
    ] .
```

These shapes need the registry (which says who is an Avatar) and the assertions in the same data graph when they run. Where a dataset keeps them in different named graphs, this is the cross-graph limitation recorded as issue #60 in holon-bridge-python. Tested cases: a conforming model passes; a focus property asserted of a non-avatar fails; two default avatars on one holon fail; a `focusWithin` cycle fails. A focus property having at most one value at a time is a property of the ledger (a set-value fluent), not something SHACL can say about a history, so no shape claims it.

## Resolving a focus

To resolve a focus for an avatar at a time:

1. Take the avatar from the caller's explicit choice, then the caller's saved choice, then the default avatar of the holon that scopes the dataset. If there is none, refuse. Do not guess.
2. Take the time from the caller. If none is given, it means now, and now is read at each resolution, not frozen when the focus was set.
3. Read the avatar's value for each root focus property (one with no `focusWithin`) as of that time. Where a value exists, read the focus properties that are `focusWithin` it, and so on down the chain.
4. Return the path: each focus property with its value.

Anything more derived than that belongs to the projection, not the focus. In the Elizabeth model, Jane's location is Train E and her station is whatever Train E's station is; that second hop is the projection's.

The example below is the two-level case. A deeper chain repeats the optional block, and a tool would iterate over the chain instead.

<!-- databook:id: focus-resolution-query -->
```sparql
PREFIX holon: <https://w3id.org/holon/>
SELECT ?rootProperty ?holon ?withinProperty ?place WHERE {
  ?rootProperty a holon:FocusProperty .
  FILTER NOT EXISTS { ?rootProperty holon:focusWithin ?up }
  ?ra holon:subject ?avatar ; holon:fluent ?rootProperty ; holon:value ?holon ; holon:validFrom ?rf .
  OPTIONAL { ?ra holon:validTo ?rt }
  FILTER (?rf <= ?asOf && (!BOUND(?rt) || ?rt > ?asOf))
  OPTIONAL {
    ?withinProperty holon:focusWithin ?rootProperty .
    ?ca holon:subject ?avatar ; holon:fluent ?withinProperty ; holon:value ?place ; holon:validFrom ?cf .
    OPTIONAL { ?ca holon:validTo ?ct }
    FILTER (?cf <= ?asOf && (!BOUND(?ct) || ?ct > ?asOf))
  }
}
```

The query is run with `?avatar` and `?asOf` bound. This is the sample model it was tested on. The times are from the Elizabeth Line chronology.

<!-- databook:id: focus-sample-data -->
```turtle
@prefix holon: <https://w3id.org/holon/> .
@prefix agent: <https://example.org/agent#> .
@prefix lts:   <https://example.org/lts#> .
@prefix st:    <https://example.org/station#> .
@prefix train: <https://example.org/train#> .
@prefix rdfs:  <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:   <http://www.w3.org/2001/XMLSchema#> .

lts:ElizabethHolon a holon:Holon ; holon:defaultAvatar agent:JaneDoe .
lts:BakerlooHolon  a holon:Holon .
holon:FluentProperty a rdfs:Class .

agent:presentIn a holon:FocusProperty .
agent:location  a holon:FocusProperty ; holon:focusWithin agent:presentIn .

agent:JaneDoe a agent:Agent, holon:Avatar ; rdfs:label "Jane Doe" .
agent:MaxDamage a agent:Agent ; rdfs:label "Max Damage" .

# Jane's presence, from the Elizabeth Line chronology
agent:Jane_present_1 a holon:StateAssertion ; holon:subject agent:JaneDoe ; holon:fluent agent:presentIn ;
    holon:value lts:ElizabethHolon ; holon:validFrom "2026-11-12T22:50:00Z"^^xsd:dateTime ; holon:validTo "2026-11-12T23:27:00Z"^^xsd:dateTime .
agent:Jane_present_2 a holon:StateAssertion ; holon:subject agent:JaneDoe ; holon:fluent agent:presentIn ;
    holon:value lts:BakerlooHolon ; holon:validFrom "2026-11-12T23:27:00Z"^^xsd:dateTime .

# Her location within whichever holon she is in
agent:Jane_loc_1 a holon:StateAssertion ; holon:subject agent:JaneDoe ; holon:fluent agent:location ;
    holon:value train:TrainE ; holon:validFrom "2026-11-12T23:07:20Z"^^xsd:dateTime ; holon:validTo "2026-11-12T23:16:20Z"^^xsd:dateTime .
agent:Jane_loc_2 a holon:StateAssertion ; holon:subject agent:JaneDoe ; holon:fluent agent:location ;
    holon:value st:Whitechapel ; holon:validFrom "2026-11-12T23:16:20Z"^^xsd:dateTime ; holon:validTo "2026-11-12T23:17:30Z"^^xsd:dateTime .
agent:Jane_loc_3 a holon:StateAssertion ; holon:subject agent:JaneDoe ; holon:fluent agent:location ;
    holon:value st:PaddingtonPraed ; holon:validFrom "2026-11-12T23:27:00Z"^^xsd:dateTime ; holon:validTo "2026-11-12T23:28:20Z"^^xsd:dateTime .
agent:Jane_loc_4 a holon:StateAssertion ; holon:subject agent:JaneDoe ; holon:fluent agent:location ;
    holon:value train:TrainB ; holon:validFrom "2026-11-12T23:28:20Z"^^xsd:dateTime ; holon:validTo "2026-11-12T23:30:20Z"^^xsd:dateTime .
agent:Jane_loc_5 a holon:StateAssertion ; holon:subject agent:JaneDoe ; holon:fluent agent:location ;
    holon:value st:EdgwareRoadBakerloo ; holon:validFrom "2026-11-12T23:30:20Z"^^xsd:dateTime .
```

| As of (12 Nov 2026, UTC) | Presence | Location |
|---|---|---|
| 22:00:00 | nothing: no focus yet | |
| 22:55:00 | ElizabethHolon | not yet asserted |
| 23:16:30 | ElizabethHolon | Whitechapel |
| 23:29:00 | BakerlooHolon | Train B |
| 23:45:00 | BakerlooHolon | Edgware Road (Bakerloo) |

A missing location does not fail the resolution: the avatar's holon is returned and the place is left unresolved. A missing presence does fail it.

## How the bridge encodes it

The spec-level model records `holon:StateAssertion` with `holon:subject`, `holon:validFrom` and `holon:validTo`. The bridge keeps a fluent as a node, with the current value in the scene graph and the ledger in the events graph. The resolver therefore reads this shape:

- The avatar holds its focus fluents as any holder does: `<avatar> ?link <fluent>` in the holons graph, with `<fluent> holon:fluentProperty <prop>`.
- A property is a focus property when the ontology graph says `<prop> a holon:FocusProperty`. Its optional `holon:focusWithin <parent>` places it one scale below the parent. A property with none is a root.
- **Now** is `holon:currentValue` in the scene graph. **As of a time** is the ledger read `holon.as_of_query` uses: the latest `hevt:assertedDateTime` at or before that time, not invalidated by then, with no later assertion at or before it. An invalidated assertion drops out and does not revive an older one.
- `holon:Avatar` and `holon:defaultAvatar` are read from the holons graph. An avatar that is not a holon has to be described there to be found.
- Paths run root to leaf. A root with no value fails with `no_focus`; a child with no value leaves the place at its parent. A `focusWithin` cycle ends the walk rather than looping.

## Tool design for holon-bridge-python

`switch_persona` already keeps per-caller state on the REST side, because the MCP layer is process-global and cannot tell two callers apart. Focus is kept the same way: `FocusStore` in `focus.py`, per person and dataset, written through to `~/.holonbridge/focus-state.json` (or `HOLONBRIDGE_FOCUS_STATE_FILE`), held on `app.state.focus`.

| Call | REST | What it does |
|---|---|---|
| `list_avatars(include_tombstoned?)` | `GET /focus/avatars` | Entities typed `holon:Avatar`, with lifecycle state and the default flagged. This is how "what can take a focus" is discovered. |
| `get_focus(avatar?, as_of?)` | `GET /focus` | The avatar (explicit, else saved, else default; refuses if none), the time (explicit, else saved, else now, read at each call) and the resolved `paths`. An override here is for the call only. |
| `set_focus(avatar?, as_of?)` | `POST /focus` | Replaces the saved focus. Both empty clears it. Resolves before saving, so a focus that cannot work is never stored. |

- **Not a ledger event.** Focus is observer state. It writes nothing to any graph, so it needs authentication but no write grant, and it can only change the caller's own saved focus.
- **Refusals.** `not_an_avatar` (409), `avatar_tombstoned` (409), `no_focus` (409), `no_avatar` (409), `ambiguous_default` (409: two different default avatars are declared, and the bridge will not pick one), `invalid_as_of` (400).
- **Lifecycle.** An Inactive avatar can be a focus. A Tombstoned one is hidden from discovery and refused by `set_focus`. An avatar with a successor resolves to the end of its chain and the result reports `redirectedFrom`. These three were proposals, not HCG decisions.
- **Not done.** `get_holon` does not yet follow the focus. An optional `holon_iri` that defaults to the focus is the obvious second step; an explicit `holon_iri` would always win.

## Open questions

- Can one caller hold several avatars at once, such as a team's point of view?
- Is the focus time valid time only, or should it also carry a transaction time (the bridge's `inserted_at`) so a focus can say "what was known then"?
- The Elizabeth model records assertions as `holon:StateAssertion` with `holon:subject`, `holon:validFrom` and `holon:validTo`. The bridge's ledger uses `hevt:StateAssertion` and keeps the current value in the scene graph. A resolver in the bridge has to read the bridge's form, and the query above needs a bridge-side counterpart.
- If an avatar is itself a holon (a company), does it own its focus fluents, or does a container own them as the Elizabeth container owns `presentIn`?
- Does the chair want avatar-ness as a class or as a Holonitype once the terminology ruling is made?
- The resolver reads fluents the avatar holds itself. If a container holds them for it, as the Elizabeth container holds `presentIn`, they are not found.

## Where to look

- The Elizabeth Line model, in the chat "Murder on the Elizabeth Line article".
- `holonbridge/focus.py`: the resolver and `FocusStore`; `tests/test_focus.py`.
- `holonbridge/persona_state.py`: the per-caller state `FocusStore` copies.
- `holonbridge_mcp/tools/core.py`: `get_holon`, which a default focus could extend.
- `holonbridge/succession.py`: `resolve_successor`, used for a succeeded avatar.
