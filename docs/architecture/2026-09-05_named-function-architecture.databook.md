---
id: https://w3id.org/databook/causalspark/named-function-architecture-v1
title: "Named Functions — Algorithmic Tracking Operations and Portable Python Invocation from SPARQL/SHACL"
type: databook
version: 1.0.0
created: 2026-09-05
author:
  - name: Kurt Cagle
    iri: https://holongraph.com/people/kurt-cagle
    role: orchestrator
license: CC-BY-4.0
domain: https://w3id.org/holonbridge/
subject:
  - holon-bridge-python
  - CausalSpark / Constellation
  - David persona
  - algorithmic tracking operations
  - SPARQL SERVICE federation
  - named-query registry
description: >
  Architecture decision record, not yet implemented, capturing two related
  design conclusions reached in conversation with Kurt Cagle on 2026-09-05:
  (1) several of the tracking operations proposed for David (and, by
  extension, any persona) -- credence banding, calibration, saturation
  detection, source-independence, gap-mint prioritisation -- need explicit,
  reproducible algorithms rather than LLM judgement, and most of them turn
  out to be expressible in native SPARQL 1.1 without any external
  computation at all; (2) the residual cases that do need real Python,
  or that must run synchronously inside a SHACL validation pass, should be
  exposed through a new admin-only "Named Function" registry hosted by
  HolonBridge itself and reached via standard SPARQL SERVICE federation --
  never through a triple-store-specific extension mechanism -- so the
  design stays portable across the swappable-backend roadmap. Relevant to
  both the holon-bridge-python implementation and the Constellation
  founder-facing architectural record, since it bears on any persona's
  tracking operations, not only David's. This copy is cross-referenced
  from CausalSpark-Ltd/Constellation/architecture/ -- same content, kept
  here because the mechanism itself belongs to this codebase.
process:
  transformer: "Claude (Cowork session)"
  transformer_type: llm
  inputs:
    - iri: urn:causalspark:conversation:2026-09-05-named-function-design
      role: primary
      description: "Live design discussion with Kurt Cagle on tracking-operation algorithms and portable Python invocation from SPARQL/SHACL, 2026-09-05"
    - iri: https://w3id.org/databook/causalspark/david-persona-capabilities-restrictions-v1
      role: context
      description: "David persona databook — origin of the census-before-why SHACL shape this design directly extends"
  timestamp: 2026-09-05T03:49:54Z
  note: >
    Not implemented tonight, by Kurt's explicit instruction — this document
    exists to capture the design before it is lost, not to specify final
    code. Several details (the Python implementation-reference shape,
    reconciliation with the actual pipeline manifest vocabulary in
    holonbridge/routes/pipeline.py) are explicitly marked open below and
    were not verified against live code in this session.
graph:
  namespace: https://w3id.org/holonbridge/
  named_graph: https://w3id.org/databook/causalspark/named-function-architecture-v1#graph
  triple_count: 36
  subjects: 10
  rdf_version: "1.1"
  turtle_version: "1.1"
  reification: false
  validator_note: >
    triple_count/subjects above describe the primary vocabulary block only
    (Named Function class and properties). The three supporting blocks in
    the technical appendix — worked registry examples, the two SHACL shape
    variants, and the illustrative pipeline-stage sketch — are counted
    separately in their own headers, since they extend rather than restate
    the primary graph.
---

## What this document is

This is an architecture decision record, not an implementation. It captures a design conversation about two problems that came up while reviewing David's persona design: several of the computations David (and any future persona) is meant to *track* — how confident he was, whether an investigation had gathered enough evidence, whether a gap is worth chasing — are currently specified as things an LLM judges, not things a defined algorithm computes; and separately, the pieces of that computation that do need real code raise a portability question, since the Holon Graph Architecture's own roadmap deliberately keeps the option of swapping triple-store backends (Neptune, Stardog, GraphDB, AllegroGraph, RDFox, Virtuoso, Fluree are all candidates), which rules out anything tied to one store's own extension mechanism.

Kurt's directive was explicit: capture this now, in enough detail that it isn't lost, without committing to code tonight. This document is addressed to both audiences that need it — the holon-bridge-python codebase, where the actual mechanism would be built, and the Constellation architectural record, where founders can see why this matters for David specifically and for the persona architecture generally.

## Problem one: the tracking operations need explicit algorithms

David's design describes several computations in terms of what they're for, not how they're computed: a credence band on an inference, a calibration score against past predictions, a saturation signal on an evidence census, a judgement of whether two claims are independently sourced, a priority estimate for a typed knowledge gap. Left as LLM judgement, none of these can be meaningfully audited later — you can't say David's calibration is improving if the calibration score itself was also just a feeling. Four of these have well-established, non-exotic formalizations once you're willing to pin one down, and the fifth needs to be reframed rather than solved as originally stated.

Calibration is the most solved of the five: Brier score (or log score) between a prediction's pre-registered stated credence and its eventual 0/1 outcome, bucketed by declared credence band, is standard forecasting methodology and pure arithmetic over resolved predictions.

Saturation detection — the gate that decides whether an evidence census can stop and a hypothesis can begin — has a real quantitative form borrowed from thematic-saturation methodology: track the marginal count of new entities or relationships surfaced per additional source consulted, and declare saturation once that marginal rate drops below a threshold for some number of consecutive sources. This matters beyond just having a formula — it converts "saturationReached" from something David asserts about himself into something a shape can actually trust, since a SHACL constraint can check that a census step precedes a hypothesis step, but it cannot check whether a self-reported saturation claim was honest.

Source-independence is a graph-connectivity question, not a judgement call: given two claims' `prov:wasDerivedFrom` chains, do their ancestor sets share a root above some threshold. This is a transitive property-path computation, conditional entirely on the provenance graph actually having been populated upstream — accurate only as far as Iris's ingestion carried real lineage through, which is a coupling risk this function cannot compensate for on its own.

Value-of-information is the one case that resists a clean formalization as originally framed — a full decision-theoretic VoI needs a utility model over downstream decisions that doesn't exist yet. The recommendation is to stop calling it VoI and specify instead the narrower, well-defined quantity it's actually reaching for: expected information gain, prior entropy minus expected posterior entropy over the candidate gap, a standard quantity from Bayesian experimental design. Narrower than true VoI, but honest and computable, rather than a heuristic priority score wearing VoI's name.

Credence banding itself needs to stop being implicit, too — a fixed, declared bucket scheme, applied deterministically, with the calibration computation run per band so that if "likely" claims only pan out 55% of the time, that's visible evidence the bands (or David's own confidence) need recalibrating.

The finding that matters most for what follows: once specified this way, calibration's arithmetic, entropy-gain's bucketing, and the provenance-connectivity check are all expressible in native SPARQL 1.1 — aggregate functions, arithmetic in `BIND`, property paths for transitive closure — with no external computation required at all. That sharply narrows what actually needs an escape hatch out of SPARQL down to the genuinely statistical or model-dependent cases (an embedding-based similarity function, something needing real numerical libraries, an LLM call itself) — saturation detection's marginal-rate tracking sits right at that boundary, and is the running example used below.

## Problem two: portable invocation without a Jena-specific mechanism

Every RDF store has its own way to extend SPARQL with custom code — Jena/ARQ's property-function registration, Stardog's function registration, GraphDB's plugin mechanism, RDFox's own extension API. All of them require deploy-time access to the store's own configuration, and none of them travel across a backend swap. Given the multi-backend roadmap, none of these are the right foundation.

The mechanism that stays inside core SPARQL rather than any vendor's dialect is `SERVICE` federation — part of the SPARQL 1.1 standard, supported by every conformant engine, explicitly designed to let a query reach out to another SPARQL-speaking endpoint mid-query without the target store needing to be modified at all. A SHACL `sh:sparql` constraint's query body is itself just SPARQL, so the same mechanism works inside a SHACL validation pass.

That still leaves the question of what the `SERVICE` call reaches. The candidate settled on in conversation is that HolonBridge should host this endpoint itself, rather than standing up a separate always-on compute service. The reasoning: HolonBridge already mediates every write through its own ACL layer, so its own liveness is already a precondition for any write being attempted in the first place — hosting the compute endpoint there adds a new *leg* onto an existing dependency (Fuseki, mid-validation, calling back out to HolonBridge) rather than a genuinely new, independently-failing dependency the way a standalone daemon would be.

## Timing split: not everything needs to be synchronous

Not every tracking computation needs to block a write, and the two shapes of need call for different mechanisms.

Deferred, batch-shaped computations — calibration reports, entropy-gain scores, credence-band assignment — don't need to interrupt anything. These fit naturally as a new pipeline-stage kind alongside HolonBridge's existing query/rule/projection stages: read via an ordinary `SELECT`, compute in a registered Python callable, write results back via an ordinary `UPDATE`. The triple store never sees anything but standard SPARQL going in and coming out, which means this is fully portable with zero backend-specific dependency, and it costs nothing new architecturally beyond adding a stage type to a pattern that already exists.

Synchronous, must-block-the-write computations — saturation detection gating the census-before-why shape is the running example — can't be deferred, because the whole point is to stop a `HypothesisGenerationStep` from being created at all. This is the case that needs live `SERVICE` federation to a HolonBridge-hosted route, described below.

## The new mechanism, concretely

HolonBridge today only acts as a SPARQL *client* — it calls out to Fuseki. Answering a `SERVICE` call requires it to also act, narrowly, as a SPARQL *server*: a route conformant enough with the SPARQL 1.1 Protocol to return proper SPARQL Results JSON for a federated `SELECT`/`ASK`. That is a genuinely new piece of surface area, though a small one, and it doesn't require reimplementing a query engine — it only needs to answer a fixed, small set of registered questions, not arbitrary SPARQL.

The natural shape, parallel to the existing named-query registry, is a new class — `hb:NamedFunction` — registered the same way named queries are: an id, declared input and output variable names, and, in place of a SPARQL body, a reference to a registered Python callable. One generic route, keyed by function id in the path, is what a SHACL constraint's `SERVICE` clause targets; it extracts the bound values from an incoming `VALUES` clause (the same binding convention `hquery:` parameters already use), dispatches in-process to the registered callable, and returns SPARQL Results JSON. Jena itself requires no modification whatsoever — the entire new surface lives on HolonBridge's side of the wire, which is the direct answer to the portability constraint.

Two costs come with this that are worth naming plainly rather than discovering later. First, it introduces a new edge in the request graph: Fuseki calling back out to HolonBridge, the reverse of today's traffic direction, which needs a routable network path from wherever Fuseki runs to wherever HolonBridge runs — not automatically true just because HolonBridge can already reach Fuseki. Second, any SHACL shape that federates out this way adds synchronous, in-request latency, and a new liveness dependency, to every write gated by that shape.

## Build-avoidance check: ask before reaching for the heavier mechanism

Before building the `SERVICE`-to-Named-Function mechanism for any specific case — starting with saturation detection — it's worth checking whether that graph's write path is already fully chokepointed through HolonBridge's own ACL layer (`check_write`/`check_replace` on every route, the same definer's-rights shape the named-query registry already relies on). If it is, the same computation can run client-side inside HolonBridge before the `UPDATE` is even constructed, with the result asserted as an ordinary triple in the write payload — no `SERVICE` call needed at all, and the SHACL shape reduces to pure structural SHACL Core, checking only that a validly-computed, correctly-ordered triple is present. That is materially cheaper to build and ship than the federated mechanism, and it still gets a genuinely deterministic value rather than a self-report.

The heavier, federated mechanism earns its cost specifically where the guarantee has to hold independent of write path — a second route into the graph that could bypass HolonBridge's own pre-computation, or a deliberate choice not to depend on chokepoint discipline holding indefinitely as the system grows. This is a per-shape, per-graph decision, not a blanket one, and the two SHACL variants in the technical appendix below show both options side by side for exactly this reason — pick the chokepoint variant by default, and only reach for the federated variant where the write path genuinely isn't singular.

## Governance: admin-only registration

Kurt's instruction on this point was unambiguous and is recorded here as a firm requirement, not a recommendation: any Named Function may be defined only by the admin account. The reasoning holds up on its own — registering a named query is "here is some SPARQL that will run when invoked"; registering a named function is "here is code that will execute automatically and unattended, every time a relevant write is attempted." That is a materially larger trust boundary than anything the existing ACL layer gates today, closer in kind to the `admin`/`founder` role split already established for CausalSpark (`admin` kept narrowly held to Kurt specifically, as a break-glass capability, not a routine founder privilege) than to the write/replace grant model that governs ordinary data. The exact implementation shape — Kurt's own suggestion is class methods on a registered, allow-listed set, rather than open dynamic import from an arbitrary path — is deliberately left open below; the principle that governs it is settled.

## What's still open

Several things here are explicitly not decided and should not be read as more settled than they are. The Python implementation-reference shape for a registered function (`hb:implementationRef` in the appendix below is a placeholder, not a design) needs its own pass — class methods on an allow-listed registry, per Kurt's steer, but the concrete mechanics (how a method gets allow-listed, what prevents registering something not on that list, whether the allow-list itself is code-deployed or graph-stored) haven't been worked out. The illustrative `hb:PythonComputeStage` pipeline-stage sketch in the appendix has not been checked against the real pipeline manifest vocabulary in `holonbridge/routes/pipeline.py` — that reconciliation is a prerequisite for implementation, not an afterthought, and this document should not be read as claiming that vocabulary already exists in the codebase. Per-shape, per-graph decisions about which SHACL variant (chokepoint vs. federated) applies where haven't been made for any specific graph yet, including David's own census shape — this document lays out the decision criterion, not the decisions themselves. And the entropy-gain formalization of gap-mint prioritisation, while well-defined as a formula, still needs someone to decide what "prior distribution" and "candidate evidence model" concretely mean for a given gap category before it's implementable, not just definable.

## Technical appendix — for whoever builds this

### Named Function vocabulary (primary block, 36 triples / 10 subjects)

```turtle
@prefix hb:   <https://w3id.org/holonbridge/> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

hb:NamedFunction a rdfs:Class ;
    rdfs:label "Named Function"@en ;
    dct:description "A registered, admin-only computation invocable by id, returning a SPARQL-results-shaped answer over bound input variables. Distinct from hb:NamedQuery: a NamedQuery's body is SPARQL executed against the dataset; a NamedFunction's implementation is Python, executed in-process by HolonBridge, and exposed to SPARQL/SHACL callers only via SERVICE federation -- never by extending the triple store itself."@en .

hb:inputVariable a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:range xsd:string ;
    rdfs:label "declared input variable name"@en ;
    dct:description "Name of a SPARQL variable this function expects to receive via a VALUES clause in the federated SERVICE call."@en .

hb:outputVariable a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:range xsd:string ;
    rdfs:label "declared output variable name"@en ;
    dct:description "Name of a SPARQL variable this function binds in its SPARQL-Results-JSON response."@en .

hb:implementationRef a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:range xsd:string ;
    rdfs:label "implementation reference"@en ;
    dct:description "Opaque reference to the registered Python callable backing this function (exact shape -- dotted import path, class-method reference, or fixed allow-list key -- deliberately left open pending implementation planning)."@en .

hb:registrationRestriction a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:label "registration restriction"@en ;
    dct:description "Who may register or alter this resource. For hb:NamedFunction this is always hb:AdminOnly -- enforced at the application/ACL layer (mirroring check_write/check_replace), not something RDF or SHACL can itself guarantee, since neither can attest to the identity of a caller."@en .

hb:AdminOnly a hb:RegistrationRestriction ;
    rdfs:label "admin account only"@en .

hb:RegistrationRestriction a rdfs:Class ;
    rdfs:label "Registration Restriction"@en .

hb:PipelineStage a rdfs:Class ;
    rdfs:label "Pipeline Stage"@en ;
    dct:description "Illustrative placeholder only in this document -- reconcile against the actual pipeline manifest vocabulary in holonbridge/routes/pipeline.py before implementing; that schema was not checked while drafting this design."@en .

hb:PythonComputeStage rdfs:subClassOf hb:PipelineStage ;
    rdfs:label "Python Compute Stage"@en ;
    dct:description "A deferred (non-blocking) pipeline stage: reads via an ordinary SPARQL SELECT, computes via a registered hb:NamedFunction, writes results back via an ordinary SPARQL UPDATE. The triple store sees only standard SPARQL in and out; portable to any backend by construction."@en .

hb:computesWith a rdf:Property ;
    rdfs:domain hb:PythonComputeStage ;
    rdfs:range hb:NamedFunction ;
    rdfs:label "computes with"@en .
```

### Worked registry examples (40 triples / 4 subjects)

The four algorithms specified in prose above, registered as `hb:NamedFunction` resources — concrete enough to show the shape, not yet implementable, since `hb:implementationRef` is explicitly a placeholder in every entry.

```turtle
@prefix hb:  <https://w3id.org/holonbridge/> .
@prefix dct: <http://purl.org/dc/terms/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

hb:function-saturation-check a hb:NamedFunction ;
    dct:identifier "saturation-check" ;
    dct:title "Evidence-census saturation detector"@en ;
    dct:description "Computes whether an evidence census has saturated: tracks the marginal count of new entities/relationships surfaced per additional source consulted, and reports saturation once that marginal rate falls below a declared threshold for N consecutive sources (a quantitative form of thematic saturation). Replaces an LLM's self-reported 'saturationReached' boolean with a deterministic, reproducible one."@en ;
    hb:inputVariable "censusSteps", "threshold", "consecutiveWindow" ;
    hb:outputVariable "saturated", "marginalRate" ;
    hb:implementationRef "TBD -- implementation planning deferred" ;
    hb:registrationRestriction hb:AdminOnly .

hb:function-brier-calibration a hb:NamedFunction ;
    dct:identifier "brier-calibration" ;
    dct:title "Credence-band calibration score"@en ;
    dct:description "Computes a Brier (or log) score between each resolved prediction's pre-registered stated credence and its eventual 0/1 outcome, bucketed by declared credence band, producing a reliability figure per band. Pure arithmetic over resolved predictions -- specified here as a NamedFunction for uniformity with the other tracking operations, though it is equally expressible as native SPARQL 1.1 aggregate/BIND arithmetic and does not strictly require this mechanism."@en ;
    hb:inputVariable "resolvedPredictions" ;
    hb:outputVariable "band", "brierScore", "sampleSize" ;
    hb:implementationRef "TBD -- implementation planning deferred" ;
    hb:registrationRestriction hb:AdminOnly .

hb:function-entropy-gain a hb:NamedFunction ;
    dct:identifier "entropy-gain" ;
    dct:title "Expected information gain (value-of-information proxy)"@en ;
    dct:description "Computes prior entropy minus expected posterior entropy for a candidate knowledge gap, as a well-defined, narrower substitute for full decision-theoretic value-of-information (which would require a utility model over downstream decisions that does not yet exist). Used to prioritise typed gap-minting rather than a heuristic score dressed up as VoI."@en ;
    hb:inputVariable "priorDistribution", "candidateEvidenceModel" ;
    hb:outputVariable "expectedInformationGain" ;
    hb:implementationRef "TBD -- implementation planning deferred" ;
    hb:registrationRestriction hb:AdminOnly .

hb:function-source-independence-check a hb:NamedFunction ;
    dct:identifier "source-independence-check" ;
    dct:title "Provenance-chain independence check"@en ;
    dct:description "Computes whether two claims are genuinely independently sourced by testing their prov:wasDerivedFrom ancestor sets for a shared root above a declared threshold -- a transitive property-path computation, accurate only to the extent the provenance graph was actually populated upstream (a coupling risk with ingestion, not something this function can compensate for)."@en ;
    hb:inputVariable "claimA", "claimB" ;
    hb:outputVariable "independent", "sharedRoot" ;
    hb:implementationRef "TBD -- implementation planning deferred" ;
    hb:registrationRestriction hb:AdminOnly .
```

### Two SHACL shape variants for the census-before-why gate (14 triples / 4 subjects)

Both target the same shape from David's own databook; the choice between them is the build-avoidance check above applied to one concrete case.

```shacl
@prefix sh:    <http://www.w3.org/ns/shacl#> .
@prefix david: <https://w3id.org/holon/persona/david#> .
@prefix hb:    <https://w3id.org/holonbridge/> .
@prefix dct:   <http://purl.org/dc/terms/> .

david:CensusBeforeWhyShape-Chokepoint a sh:NodeShape ;
    sh:targetClass david:ReasoningChain ;
    dct:description "Default recommendation: use this variant whenever the graph's write path is already fully chokepointed through HolonBridge's own ACL layer. Saturation is computed by HolonBridge in Python before the write is even constructed, and asserted as an ordinary triple in the write payload; this shape only checks that a validly-computed, correctly-ordered saturation triple is present. Pure SHACL Core -- no SERVICE call, nothing new to build." ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "A HypothesisGenerationStep requires a preceding, already-saturated EvidenceCensusStep (computed before this write, not asserted live)."@en ;
        sh:select """
            PREFIX david: <https://w3id.org/holon/persona/david#>
            SELECT $this ?hyp WHERE {
                $this david:hasStep ?hyp .
                ?hyp a david:HypothesisGenerationStep ; david:stepOrder ?hypOrder .
                FILTER NOT EXISTS {
                    $this david:hasStep ?census .
                    ?census a david:EvidenceCensusStep ;
                            david:stepOrder ?censusOrder ;
                            david:saturationReached true .
                    FILTER (?censusOrder < ?hypOrder)
                }
            }
        """ ;
    ] .

david:CensusBeforeWhyShape-Federated a sh:NodeShape ;
    sh:targetClass david:ReasoningChain ;
    dct:description "Fallback variant: use only where the saturation guarantee must hold independent of write path -- e.g. a second write route into this graph that bypasses HolonBridge's own pre-computation, or a deliberate choice not to depend on chokepoint discipline holding indefinitely. Calls the registered saturation-check NamedFunction live, mid-validation, via SPARQL SERVICE federation to a route HolonBridge itself hosts -- no Jena/store-side modification required, but adds a synchronous network dependency and in-request latency to every gated write." ;
    sh:sparql [
        a sh:SPARQLConstraint ;
        sh:message "Saturation must be confirmed live by the registered saturation-check function."@en ;
        sh:select """
            PREFIX david: <https://w3id.org/holon/persona/david#>
            PREFIX hb: <https://w3id.org/holonbridge/>
            SELECT $this ?hyp WHERE {
                $this david:hasStep ?hyp .
                ?hyp a david:HypothesisGenerationStep ; david:stepOrder ?hypOrder .
                $this david:hasStep ?census .
                ?census a david:EvidenceCensusStep ; david:stepOrder ?censusOrder .
                FILTER (?censusOrder < ?hypOrder)
                SERVICE <https://bridge.example.org/named-function/saturation-check/service> {
                    VALUES ?censusStep { ?census }
                    ?censusStep hb:computedSaturated ?saturated .
                }
                FILTER (!BOUND(?saturated) || ?saturated = false)
            }
        """ ;
    ] .
```

### Worked example: the federated SERVICE call, outside a string literal for readability

```sparql
PREFIX david: <https://w3id.org/holon/persona/david#>
PREFIX hb:    <https://w3id.org/holonbridge/>

SELECT ?censusStep ?saturated WHERE {
    VALUES ?censusStep { david:example-census-step-7 }
    SERVICE <https://bridge.example.org/named-function/saturation-check/service> {
        VALUES ?censusStep { ?censusStep }
        ?censusStep hb:computedSaturated ?saturated .
    }
}
```

### Illustrative pipeline-stage sketch (4 triples / 1 subject) — unverified against live code

```turtle
@prefix hb:     <https://w3id.org/holonbridge/> .
@prefix dct:    <http://purl.org/dc/terms/> .
@prefix david:  <https://w3id.org/holon/persona/david#> .

[] a hb:PythonComputeStage ;
    dct:description "Illustrative sketch only -- reconcile against the actual pipeline manifest vocabulary in holonbridge/routes/pipeline.py before implementing; not checked against that code while drafting this design." ;
    hb:computesWith hb:function-brier-calibration ;
    dct:title "Calibration-report stage: read resolved predictions, score by credence band, write a CalibrationReport"@en .
```
