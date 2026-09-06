---
id: https://w3id.org/databook/causalspark/named-function-vs-extension-function-status-v1
title: "Named Functions vs. Jena/ARQ Extension Functions — Status Confirmation and Governance Hold, 6 September 2026"
type: databook
version: 1.0.0
created: 2026-09-06
author:
  - name: Kurt Cagle
    iri: https://holongraph.com/people/kurt-cagle
    role: orchestrator
license: CC-BY-4.0
domain: https://w3id.org/holonbridge/
subject:
  - holon-bridge-python
  - CausalSpark / Constellation
  - model tiering
  - Named Functions
  - SPARQL SERVICE federation
  - Jena ARQ extension functions
  - founders governance
description: >
  Status note, not a new design. Records where a live implementation
  question landed after reviewing Carlo's 6 September 2026 "Model Tiering
  for the Constellation Personas" note: whether the occasional
  computations needed by the note's proposed escalation gate should be
  implemented as Jena/ARQ extension functions (Java, or ARQ's built-in
  JavaScript scripting mechanism) or as HolonBridge-hosted Named
  Functions per the existing hb:NamedFunction / SERVICE-federation
  design. Confirms the latter, on three independent grounds, without
  revising anything in the 2026-09-05 named-function-architecture
  record. Also records the founders' governance decision to hold all of
  Carlo's proposed decision records for formal ratification before any
  implementation begins.
process:
  transformer: "Claude (chat session)"
  transformer_type: llm
  inputs:
    - iri: urn:causalspark:note:2026-09-06-model-tiering
      role: primary
      description: "Model Tiering for the Constellation Personas — Carlo's pricing/tiering note, sent to Kurt via Caroline, 6 September 2026"
    - iri: https://w3id.org/databook/causalspark/named-function-architecture-v1
      role: context
      description: "Named Function architecture design capture, 2026-09-05 — the design this note confirms rather than revises"
  timestamp: 2026-09-06T20:50:00Z
  agent:
    name: Chloe Shannon
    iri: https://holongraph.com/people/chloe-shannon
    role: transformer
  note: >
    This document closes out a live implementation question the
    2026-09-05 record had left open by construction (it deliberately
    committed to SERVICE federation over any triple-store-native
    mechanism, but hadn't yet been tested against a live alternative).
    Reviewing Carlo's tiering note supplied that test: its proposed
    escalation gate cites "Kurt's named-function architecture" as its
    natural home, which raised the question of whether Jena's own
    extension-function surface — including a JavaScript mechanism that
    avoids Kurt's stated objection to a Java recompile cycle — might be
    a simpler alternative. It was considered and set aside. Nothing here
    changes hb:NamedFunction, hb:implementationRef, or any open item
    from the prior record.
graph:
  namespace: https://w3id.org/holonbridge/
  named_graph: https://w3id.org/databook/causalspark/named-function-vs-extension-function-status-v1#graph
  triple_count: 27
  subjects: 9
  rdf_version: "1.1"
  turtle_version: "1.1"
  reification: false
---

## What this document is

A status note, not a design document. Two things happened in conversation on 6 September that are worth recording for the founders and for whoever next touches `holon-bridge-python`'s Named Function surface: a specific implementation question got asked and answered, and a governance decision was made about timing. Neither changes the 2026-09-05 `named-function-architecture` record — this document confirms it from a second angle and adds a scope note about when any of it may actually be built.

## Context: what Carlo's note surfaced

Carlo's "Model Tiering for the Constellation Personas" note (6 September, sent via Caroline) proposes a resource-rational policy for routing persona work across Haiku 4.5, Sonnet 5, Opus 5, and Fable 5.1 by task structure, with a five-trigger symbolic escalation gate (stakes, prediction error, credence band, tier disagreement, and novelty relative to the graph) deciding when work escalates to a stronger model. The note describes the gate as "a named query over the graph — inspectable and tunable without touching code" and separately names "Kurt's named-function architecture" as its natural home.

That second phrase raised a real, previously untested question: the 2026-09-05 record had already decided *that* Named Functions live in HolonBridge and are reached via SPARQL `SERVICE` federation rather than a triple-store-native extension mechanism — but that decision had not yet been checked against a concrete alternative someone might reasonably propose instead. Carlo's gate is exactly that concrete case.

## Clarification carried forward from 2026-09-05

Kurt's own framing of the relationship stands unchanged and matters here: Named Functions are persona utility functions, not directly invocable on their own. Everything invocable runs through a named query or a SHACL `sh:sparql` constraint; a Named Function is only ever reached *from within* one of those, never called on its own. Applied to Carlo's gate, this means most of what the gate actually needs — the credence-band check, a SHACL-validation-status check, the graph-novelty/conflict check — is plain SPARQL inside a named query and needs no Named Function at all. Only a narrower residual (a calibration or discard-recall lookup, or anything needing a real numerical library) would ever reach one.

## The alternative considered: Jena/ARQ's own extension-function surface

Apache Jena 6 ships a real extension-function surface out of the box: the `afn:` function library and XPath/XQuery-derived `fn:`/`math:` functions for FILTER/BIND expressions; `agg:` custom statistical aggregates; `apf:`/`list:` property functions (list operations, container membership, Lucene free-text search via the separate `jena-text` module); and, most relevantly, a built-in JavaScript scripting mechanism (`arq:js-library`, `jena:scripting=true`, backed by GraalJS) that registers new SPARQL-callable functions from an external `.js` file at runtime — specifically without a Java recompile, which is the exact friction the 2026-09-05 record's `hb:NamedFunction` design was already built to avoid.

That JavaScript mechanism was taken seriously as a candidate precisely because it answers the recompile objection directly. It was set aside anyway, on three independent grounds:

1. **Access surface.** Raw SPARQL access to Fuseki (`sparql_select`/`sparql_update`/`push_turtle`) still coexists with the named-query layer as ACL gating has been extended endpoint-by-endpoint rather than closed off wholesale. Anything registered as a Jena/ARQ-native extension — Java or JavaScript — is reachable by any caller with raw access, not gated by Toolset-reachability the way named queries are. A HolonBridge-hosted function only exists where invocation is already controlled.
2. **Workload fit.** The gate's actual computations are small, occasional, symbolic lookups over already-bound values, not per-row or high-throughput work — nothing here needs in-engine execution for performance. Some of them (a discard-recall or calibration lookup) may need data or logic that doesn't live in the graph being queried at all, which an in-engine function can't reach cleanly regardless of language.
3. **Deployment portability.** HolonBridge is the one constant across all three of the tiering note's own deployment modes — hosted SaaS, client private-cloud (e.g. Azure OpenAI/Foundry), and on-premise — and across the swappable-triple-store-backend roadmap already underway (Stardog, GraphDB, Neptune, and others under consideration). A Jena/ARQ-specific mechanism, GraalJS scripting included, would need reinstalling or reconfiguring per store and per deployment. A standard SPARQL 1.1 `SERVICE` call needs nothing store-specific at all. This is the identical reasoning that led the 2026-09-05 record to choose `SERVICE` federation over any store-native mechanism in the first place — today's review arrived at the same conclusion independently, which is a useful confirmation that the design holds up under a second line of argument, not just the one that produced it.

Worth naming explicitly: a `SERVICE` call sitting inside a named query or SHACL shape is just as visible in the query text as a Jena-side function call would be, so nothing is traded away on inspectability to get the other two properties.

## Net effect

No change to `hb:NamedFunction`, `hb:implementationRef`, the `SERVICE`-federation mechanism, or any item left open in the 2026-09-05 record — all of it stands as written. Jena 6's own extension-function surface remains worth knowing about (appendix below) purely as a future in-engine performance option, should a genuinely high-throughput, per-row computation ever arise that the network hop to a HolonBridge `SERVICE` endpoint would meaningfully bottleneck — not as a plan, and not as the default.

## Governance: implementation held for formal ratification

Caroline has agreed not to implement any of Carlo's model-tiering proposals — DR-A (tiering policy), DR-B (model provenance), DR-C (calibration protocol), or the pricing basis — until a formal founders meeting can ratify them. That hold applies equally to anything in this document: nothing here is authorization to build the `SERVICE` endpoint, register a `hb:NamedFunction`, or otherwise begin implementation. This is a technical record for that meeting, not a green light ahead of it.

## Appendix: Jena 6's extension-function surface, for reference

Not the chosen mechanism, but real and worth having on file.

| Category | Prefix / mechanism | Examples |
|---|---|---|
| XPath/XQuery F&O 3.1 scalar functions | `fn:`, `math:` | String, date, and numeric functions beyond SPARQL 1.1's mandated subset (sequence-taking functions unsupported) |
| ARQ's own value-function library | `afn:` | `afn:localname`, `afn:namespace`, `afn:bnode`, `afn:sprintf`, `afn:substr`/`afn:strjoin`, `afn:sha1sum`, `afn:min`/`afn:max`/`afn:sqrt`/`afn:pi`/`afn:e`, `afn:now()` — mostly thin conveniences with preferred SPARQL-native equivalents |
| Custom aggregates | `agg:` | `agg:stdev`, `agg:stdev_samp`, `agg:stdev_pop`, `agg:variance`, `agg:var_samp`, `agg:var_pop` (ARQ syntax mode) |
| Property functions ("magic properties") | `apf:`, `list:` | `list:member`/`list:index`/`list:length`, `rdfs:member` (container membership), `apf:textMatch` (Lucene, needs the `jena-text` module), `apf:bag`; also `concat`, `splitIRI`/`splitURI`, `str`, `strSplit`, `assign`, `version` — present in the codebase but not written up on Jena's current docs page; confirm exact signatures against `StandardFunctions.java` / the `pfunction.library` package source before relying on one |
| JavaScript scripting (the recompile-avoiding option) | `js:` under `<http://jena.apache.org/ARQ/jsFunction#>` | Requires GraalJS on the classpath and `jena:scripting=true`; functions loaded from a file via the `arq:js-library` context setting (or inline via `arq:js-functions`), restricted to an explicit `arq:scriptAllowList`. Avoids a Java recompile, but still couples the function to one Jena/Fuseki instance and, while raw SPARQL access remains open, is reachable outside HolonBridge's own Toolset-reachability gate |

## Technical appendix — decision and governance status (Turtle)

<!-- databook:id: decision-status -->
```turtle
@prefix hb:   <https://w3id.org/holonbridge/> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

hb:DesignDecision a rdfs:Class ;
    rdfs:label "Design Decision"@en .

hb:decision-named-function-vs-extension-function a hb:DesignDecision ;
    dct:title "Named Functions remain HolonBridge-hosted, not Jena/ARQ extension functions"@en ;
    dct:description "Confirms the 2026-09-05 hb:NamedFunction / SERVICE-federation design over both Java and JavaScript ARQ extension-function mechanisms, on three grounds: raw-SPARQL-access exposure, workload fit, and deployment portability across hosted SaaS, private-cloud, and on-premise modes."@en ;
    dct:date "2026-09-06"^^xsd:date ;
    dct:relation <https://w3id.org/databook/causalspark/named-function-architecture-v1> .
```

<!-- databook:id: governance-status -->
```turtle
@prefix hb:   <https://w3id.org/holonbridge/> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

hb:GovernanceStatus a rdfs:Class ;
    rdfs:label "Governance Status"@en .

hb:DecisionRecord a rdfs:Class ;
    rdfs:label "Decision Record"@en .

hb:dr-a-tiering-policy a hb:DecisionRecord ;
    dct:title "DR-A — Tiering policy"@en .

hb:dr-b-model-provenance a hb:DecisionRecord ;
    dct:title "DR-B — Model provenance"@en .

hb:dr-c-calibration-protocol a hb:DecisionRecord ;
    dct:title "DR-C — Calibration protocol"@en .

hb:blocks a rdf:Property ;
    rdfs:domain hb:GovernanceStatus ;
    rdfs:range hb:DecisionRecord ;
    rdfs:label "blocks (implementation of)"@en .

hb:governance-2026-09-06-tiering-hold a hb:GovernanceStatus ;
    dct:description "Caroline agreed not to implement Carlo's model-tiering/named-function proposals -- DR-A, DR-B, DR-C, and the pricing basis -- until a formal founders meeting ratifies them."@en ;
    dct:date "2026-09-06"^^xsd:date ;
    hb:blocks hb:dr-a-tiering-policy, hb:dr-b-model-provenance, hb:dr-c-calibration-protocol .
```

> **Note:** This document's `architecture/README.md` index entry (in the Constellation repo) has not been added yet — the 2026-09-05 `named-function-architecture` record is also missing from that index. Both can be added together in a single pass rather than one at a time.
