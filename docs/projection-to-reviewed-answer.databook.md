---
id: https://w3id.org/databook/holon-bridge-python-projection-to-reviewed-answer
title: "From a projection to a reviewed answer: prompt, agent call, candidate, claim check"
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
  How a question about the graph travels to an outside agent and back
  without anything being written unreviewed. Joins four features that are
  documented separately: a named projection (builds the prompt), an agent
  call (sends it, gated), a candidate (the review queue) and a claim-check
  query (verifies what came back). The bridge does not run a model; this page
  says exactly where its responsibility stops.
---

# From a projection to a reviewed answer

The bridge **does not run a model or any agent**. It has an *agent gateway*
(`holonbridge/agents`): a gated, ledgered way to send a task to an endpoint you
have registered, and a route for the answer to come back as a *proposal*. Four
existing features make up the route, and nothing joins them in one place.

```
named projection ──► agent call ──► candidate ──► claim-check query
 (build the prompt)   (send it,       (a person     (compare each claim
                       gated)          reviews)      with the ledger)
```

Everything left of the agent is the bridge's work. The agent's reasoning is not.
Everything the agent returns is untrusted until the last two steps.

## 1. A named projection builds the prompt

`hb:NamedProjection` wraps a named query and a prompt template.
`POST /named-projection/{id}/run` runs the query and renders the template, with
every `{{result}}` replaced by the rendered result (a template with no
`{{result}}` gets the result appended rather than silently losing it). It is
**deferred-only**: it returns the prompt and sends nothing anywhere.

So a projection is how you say *what the agent will be shown*, and it is
reproducible: the same graph state gives the same prompt.

## 2. An agent call sends it, behind a gate

`POST /agent-call` (MCP and trigger equivalents below) records the call and
**sends nothing**. The call is stored as `PendingApproval` with the exact body
that would leave the bridge, and `GET /agent-call/{id}` shows that body. Fields
of the request:

| Field | Meaning |
|---|---|
| `endpoint` | id of a registered `hb:AgentEndpoint` (credentials are held by reference, never in the registry) |
| `task` | the text the agent is asked, for example the rendered projection |
| `focus` | optional IRI the task is about |
| `input` | optional extra JSON, passed to the agent as is |
| `context_query` | optional id of a named query whose result is sent as context |
| `target_graph` | the graph the answer is *proposed for*; it is never written there directly |

A person then calls `POST /agent-call/{id}/approve` (or `/reject`). Approval
sends the stored body **at most once**: the status moves to `Sending` before any
bytes leave, so a crash leaves a visible `Sending` call, not one that could go
twice. `/retry` resends a failed call with the call id as the idempotency key.
`/agent-calls/dispatch`, which the `agent-dispatch` scheduler job runs on a
timer, sends calls that a policy has already approved.

A call can start in three places, all landing in the same ledger:

- a route or MCP request, as above;
- a pipeline stage with transformer `agent`, whose `promptText` is the task.
  The stage ends `Deferred` and the call waits for approval; a pipeline run never
  queues a call for policy dispatch, and a run with no identified person cannot
  place one;
- a trigger with `onFireAgent` (state or temporal), which places the call as the
  definer. See the trigger documentation for the definer re-check.

Placing a call needs write access to the calls ledger graph, and the request is
attributed to the person who placed it.

## 3. The answer is a candidate, not a write

If the agent's reply includes Turtle and the call named a `target_graph`, the
bridge stages it with `propose_agent_candidate`: a `holon:CandidateStatus`
resource in the dataset's candidates graph, status `Pending`, carrying
`hev:proposedByAgentCall` (so a reviewer can go from the proposal to exactly
what was asked and by whom), the agent id, the target graph and the Turtle.
**Nothing reaches the target graph.**

Review it with `GET /candidates`, `GET /candidate/{id}`, then
`POST /candidate/{id}/approve` or `/reject`. Approval goes through the ordinary
ACL-checked write path, so the approver needs write access to the target graph.

**Read this before relying on approval as a check.** On this version the REST
and MCP approval routes pass no shapes graph, so an approved candidate is
written with no SHACL check at all. Approval is a person's judgement, not a
schema check. To check the Turtle first, read it with `GET /candidate/{id}` and
run it through `validate_turtle` before you approve. (A change that makes
approval follow `SHACL_REQUIRED` and accept a `shapes_graph` is open as
[PR #96](https://github.com/kurtcagle/holon-bridge-python/pull/96); once it is
merged, this paragraph should be updated.)

## 4. A claim-check query verifies what came back

A reviewer reading a plausible report is the weakest link. The bridge has no
built-in notion of a "claim", so this step is a convention you model, and the
check is an ordinary named query. The idea: have the agent cite, for each
statement, the ledger assertion it relies on and the value it says that
assertion holds. A query then compares the citation with the ledger.

The vocabulary below (`ex:`) is **an example, not bridge terms**. The ledger
terms (`hevt:`) are the bridge's.

```turtle
# What the agent returns, as a candidate. One resource per claim.
@prefix ex:   <https://example.org/report#> .
@prefix hevt: <https://w3id.org/holon/event/> .

ex:claim-1 a ex:Claim ;
    ex:statement "The 18:02 service was at Paddington when the alarm was raised." ;
    ex:cites     <urn:example:assertion:7f3c> ;     # an hevt:StateAssertion
    ex:claimsValue "Paddington" .
```

```sparql
# Named query: claims whose citation does not hold. Empty result = all claims check out.
PREFIX ex:   <https://example.org/report#>
PREFIX hevt: <https://w3id.org/holon/event/>

SELECT ?claim ?statement ?cited ?claimed ?actual ?problem WHERE {
  ?claim a ex:Claim ;
         ex:statement ?statement ;
         ex:cites ?cited ;
         ex:claimsValue ?claimed .
  OPTIONAL { ?cited a hevt:StateAssertion ; hevt:hasValue ?actual . }
  BIND(
    IF(!BOUND(?actual),            "cited assertion does not exist",
    IF(STR(?actual) != STR(?claimed), "value differs from the ledger", ""))
    AS ?problem)
  FILTER(?problem != "")
}
```

Run it against the dataset where the claims were staged and the events graph is
reachable. This checks that a claim *matches what the ledger says*. It does not
check that the claim is relevant, complete or well reasoned, and it does not
check a claim that cites nothing. Require a citation with a SHACL shape on
`ex:Claim` (`sh:minCount 1` on `ex:cites`) and run the candidate through
`validate_turtle` against it before approving.

In the worked example this was built from (a two-line train network with a
detective, a victim, a witness and a clock), six claims were written, two
deliberately false, and this query flagged both. That example ran on a local
rdflib and pyshacl harness; the agent and candidate routes in sections 2 and 3
were read from the code, not run end to end.

## Where responsibility sits

| Step | Who does it |
|---|---|
| What the agent sees | the bridge (projection, `context_query`) |
| Whether it is sent | a person, or a policy someone defined |
| What the agent does with it | the agent, outside the bridge |
| Whether the answer is written | a person, through the ordinary ACL-checked write path (SHACL on approval depends on PR #96) |
| Whether the answer is true | your claim-check query and your shapes; nothing automatic |
