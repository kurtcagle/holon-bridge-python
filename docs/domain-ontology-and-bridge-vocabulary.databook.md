---
id: https://w3id.org/databook/holon-bridge-python-domain-ontology-and-bridge-vocabulary
title: "Mapping a domain ontology onto the bridge: built in, convention, or yours"
type: databook
version: 1.0.0
created: 2026-11-13
updated: 2026-11-13
author:
  - name: Kurt Cagle
    iri: https://holongraph.com/people/kurt-cagle
    role: orchestrator
  - name: Claude Sonnet 5.5
    role: transformer
description: >
  When you model a domain (a rail network, a ward, a supply chain) on
  holon-bridge-python, which of your ideas does the bridge already have a term
  and a mechanism for, which are only conventions you must keep yourself, and
  which the bridge leaves to the modeller. Written after a worked example first
  invented vocabulary the bridge does not have, then was rebuilt on the
  bridge's own terms.
---

# Mapping a domain ontology onto the bridge

A domain model usually arrives with its own words for things: *event*, *state*,
*observation*, *door*, *clock*. Before inventing vocabulary for any of them,
check this table. There are three kinds of answer, and they are not the same:

- **Built in**: the bridge has a term *and* code that enforces or uses it.
- **Convention**: nothing in the bridge knows the term. It works only if every
  writer and every query agrees to use it. Nothing will notice if they do not.
- **Modeller's job / external**: the bridge has no construct at all.

| Idea | Status | What the bridge has |
|---|---|---|
| A changing property of a thing (a fluent) | Built in | `holon:Fluent`, with the scene holding `holon:currentValue` and `holon:currentAssertion`; changed with `update_fluent` |
| History of every change, never edited | Built in | an append-only `hevt:StateAssertion` per transition in the events graph, chained with `hevt:supersedes`, carrying `hevt:oldValue`, `hevt:hasValue`, `hevt:operation`, `hevt:assertedBy`, `hevt:sequenceId` |
| Two writers racing on the same fluent | Built in | `update_fluent` is a guarded compare-and-set; the loser writes neither the scene change nor a ledger entry, and retries |
| Check a change against the schema before it lands | Built in | a SHACL gate on a scratch copy of events and scene, **scoped to the fluent's live assertion only** (see below) |
| When a fact was true (valid time) | Built in | `hevt:assertedDateTime`; a caller may backdate it with `asserted_datetime` on `update_fluent` (REST and MCP) |
| When the record learnt it (transaction time) | Built in, since [PR #85](https://github.com/kurtcagle/holon-bridge-python/pull/85) | `hevt:insertedAt`, stamped by the bridge's wall clock; the caller cannot set it. `get_holon` accepts `observed_at` (valid time) and `inserted_at` (transaction time). Entries from before the change have no `insertedAt` and count as known from the start |
| Ask "what did the record say at T" | Built in | `get_holon` with `observed_at`, optionally `inserted_at`; refuses holons with list-mode fluents |
| An event that something happened | Built in, loosely | `hev:AssertionEvent` content written to the events graph by `create_message` (`POST /message/create`): append-only, ACL and SHACL gated. The bridge does **not** stamp `assertedAt` or `receivedAt` on it; they are whatever the DataBook says. (`hb:Message` and its `receivedAt` are something else: a pipeline run's status record.) |
| Which ledger entries an event brought about | Convention only | no bridge term links an event to the assertions written because of it |
| Something that fires when a condition becomes true | Built in | `hev:NamedTrigger`, state or temporal, edge-triggered and re-armed on the falling edge |
| An external agent giving an answer | Built in, gated | the agent gateway; answers become candidates. See [the projection-to-reviewed-answer page](projection-to-reviewed-answer.databook.md) |
| A door between holons, with a lock | Modeller's job | the bridge has no portal construct. ACLs govern who may write a graph; a SHACL shape governs what a graph may contain; neither is a rule for moving between holons |
| A clock that advances the simulated world | External driver | the scheduler runs on the wall clock. A simulated or replayed clock is something your driver supplies, through `asserted_datetime` and your own events |
| Claims an agent makes, and their citations | Convention only | see the claim-check example in the projection page |

## Three things in that table that bite

**The SHACL gate sees one assertion, not the whole holon.** Before a fluent
update commits, the bridge validates a scratch copy, but only the fluent's live
assertion is in scope. A shape that needs the holder's other triples, or a
sibling fluent's current value, will not fire at the gate, and a constraint
that spans two fluents is not enforced by it. Put such a rule in a rule or
trigger, or validate the whole holon afterwards. (A wider, opt-in gate scope is
tracked in [#88](https://github.com/kurtcagle/holon-bridge-python/issues/88).)

**Valid time and transaction time are different questions.** `assertedDateTime`
answers "when was it true", and you can backdate it to record something learnt
late. `insertedAt` answers "when did the record know it", and you cannot. A
report of "what did the investigation know on Tuesday" must use
`inserted_at`; `observed_at` alone would include a fact first recorded on
Wednesday and backdated to Monday.

**The event-to-assertion link is a convention you must keep.** If "what did the
record know at T" has to include which event prompted a change, you need one
predicate (the worked example used `lts:concerns`, from its own namespace) on
the event or the assertion, written every time. Nothing will fail if a writer
forgets. If you adopt it, put it in a SHACL shape on your event class so a
missing link is a 422 and not a gap found later. Whether the bridge should
define the term itself is an open design question, not decided here.

## Using the table

1. List the nouns and verbs of the domain.
2. Mark each *built in*, *convention* or *yours* from the table.
3. For each *convention*, write the one SHACL shape that enforces it, and say
   in the ontology's own documentation that the bridge does not.
4. For each *yours*, decide whether it belongs in the ontology, in a driver
   outside the bridge, or does not need to exist.

Terms that appear in neither the bridge nor this table are yours to define in
your own namespace. Do not give them a bridge prefix.
