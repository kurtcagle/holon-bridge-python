---
id: https://w3id.org/databook/causalspark/named-function-architecture-v1
title: "Named Functions — Algorithmic Tracking Operations and Portable Python Invocation from SPARQL/SHACL"
type: databook
version: 1.4.0
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
    were not verified against live code in this session. v1.1.0 adds a
    second candidate for how hb:NamedFunction's implementation is expressed
    -- storing the Python source itself as graph data in an admin-only
    graph, compiled by HolonBridge independently of its own deploy cycle --
    raised by Kurt the same session, alongside the original
    codebase-allow-list candidate. Neither has been chosen; both are
    recorded as open options. v1.2.0 adds a systems-viability assessment of
    the graph-native candidate, given at Kurt's request: execution
    isolation (recommending a restricted subprocess over raw in-process
    exec or full WASM sandboxing), versioning discipline, registration-time
    validation, reload/concurrency policy, an invocation-audit mechanism,
    and a candidate/registered review-status split modelled on the SCE
    ingestion pipeline. Net recommendation: ship hb:implementationRef
    first; treat hb:pythonSource as a second phase once isolation and the
    review workflow exist as real mechanisms. This is a recommendation
    captured in the record, not a decision Kurt has made. v1.3.0 adds a
    third candidate for hb:implementationRef, raised by Kurt the same
    session: a plugin architecture sourcing implementations from
    independently-installed Python packages via standard entry points,
    bypassing admin-graph writes as an installation vector entirely (though
    not, on this document's recommendation, as an activation vector -- an
    installed plugin's functions should still require an admin-gated step
    to go live). Assessed as a genuine refinement of hb:implementationRef
    rather than a fourth unrelated idea, and as the strongest of the three
    candidates for anything Kurt or a small deploy-trusted team would
    write, since it restores a two-gate model without hb:pythonSource's
    single-gate collapse. Flagged as needing clarification: who "users"
    refers to in this candidate materially changes the assessment, from a
    standard extension point (if deploy-trusted operators) to a much
    harder untrusted-third-party-code problem (if arbitrary end users).
    v1.4.0 records Kurt's direct resolution of that question: plugin
    installers are node admins -- whoever compiles and sets up a given
    HolonBridge instance -- not end users, and different federated nodes
    are expected to be administered by different people with genuinely
    different capability needs. This sharpens the plugin candidate's case:
    it makes capability separation between nodes real at the binary layer
    (a node simply doesn't have a plugin installed) rather than merely
    nominal (a permission check sitting on top of a shared codebase where
    every capability is present everywhere). It also surfaces two new,
    previously-invisible open items: named-query registration authority
    has not itself been specified as gated, even though a named query is,
    per Kurt's description, the only path an ordinary end user has to a
    Named Function at all; and any named query that embeds a SERVICE call
    to a Named Function should prefer hquery:'s typed VALUES-append
    parameter binding over hb:'s raw string substitution, since that is
    the one seam where user input meets executing code. Both are
    recommendations captured in the record, not decisions Kurt has made.
graph:
  namespace: https://w3id.org/holonbridge/
  named_graph: https://w3id.org/databook/causalspark/named-function-architecture-v1#graph
  triple_count: 41
  subjects: 11
  rdf_version: "1.1"
  turtle_version: "1.1"
  reification: false
  validator_note: >
    triple_count/subjects above describe the primary vocabulary block only
    (Named Function class and properties, including the v1.1.0
    hb:pythonSource addition). The six supporting blocks in the technical
    appendix — worked registry examples, the two SHACL shape variants, the
    illustrative pipeline-stage sketch, the v1.2.0 systems-assessment
    vocabulary sketch (47 triples / 14 subjects), the v1.3.0 plugin-sourced
    implementation vocabulary sketch (10 triples / 2 subjects), and the
    v1.4.0 named-query gating vocabulary sketch (4 triples / 1 subject) —
    are counted separately in their own headers, since they extend rather
    than restate the primary graph.
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

## Candidate: graph-native Python source, not decided

A second candidate for `hb:implementationRef` surfaced later the same session, worth recording alongside the codebase-allow-list candidate above rather than in place of it. Instead of a function's implementation being a reference to a callable already reviewed and shipped in the HolonBridge codebase, the Python source itself could be stored as literal graph data — in the same admin-only graph a `hb:NamedFunction` resource already lives in — with HolonBridge compiling it independently of its own deploy cycle, the same way named queries, SHACL shapes, and rules are already graph-native rather than baked into code.

This is a genuine, not merely cosmetic, alternative, and it earns its consideration on the same grounds everything else in this stack does: it would make Named Functions consistent with the rest of the architecture's graph-is-truth philosophy, where adding a new tracked algorithm becomes a Turtle write rather than a Python change, a PR, and a redeploy. It also comes with a real, distinct benefit the allow-list candidate doesn't offer as cleanly: automatic provenance and versioning. If the source itself is a graph literal, it is timestamped and history-tracked the same way any other assertion is, so a calibration score computed six months from now can be tied precisely to the exact source text that produced it — durable auditability of exactly the kind Ada's and David's own design already values, obtained here for free rather than by cross-referencing an external git commit against a log timestamp.

The cost is a real shift in the trust boundary, and it deserves to be named plainly rather than discovered later. In the allow-list candidate, admin-only registration means "admin selects among a set of callables a human already code-reviewed and shipped" — two gates, code review and admin approval, in series. In the graph-native-source candidate, admin-only registration is the *only* gate before HolonBridge executes whatever was written — there is no code review step at all, by construction, since the whole point is to skip the deploy cycle. Given admin is already narrowly held to Kurt personally, this mostly narrows to "Kurt no longer gets a second look at his own code before it runs," which is a smaller risk than it would be if admin were a broader role — but it stops being small the moment anything else (an automated agent, a persona, a compromised credential) can write to that graph with admin-equivalent standing, since at that point the single admin-only gate is the entire safety story. Worth deciding deliberately rather than by default.

The mechanics of "compile independently" also matter, and aren't yet specified here. A raw `exec()` of the stored source in HolonBridge's own process would be the simplest to build and the most dangerous, since the function would then run with whatever privileges HolonBridge itself has — its Fuseki credentials, its filesystem access, its network egress. True sandboxing inside a single Python interpreter has a long history of escape bugs and shouldn't be relied on; real isolation would mean compiling and running in a separate, privilege-limited process rather than in-process. Registration should also attempt to compile — and ideally exercise a dry run against the function's declared input/output variables — at write time, so a broken definition fails the write rather than failing silently on the first real invocation, which for anything on the synchronous SHACL-gating path would mean every write attempt using that shape starts failing before anyone notices why. And cache invalidation has an obvious, idiomatic answer already sitting in this codebase: `reload_named_queries`, `reload_named_rules`, and `reload_named_triggers` already exist as the pattern for "recompile from the graph on demand" — a `reload_named_functions` counterpart is the natural fourth, not a new idea.

Choosing between this and the codebase-allow-list candidate is explicitly not resolved here. Both remain live options for whoever specifies `hb:implementationRef`'s final shape.

## Systems assessment: what graph-native execution would actually require

Evaluated on its own merits, the graph-native candidate is architecturally sound and consistent with the rest of this stack's philosophy -- but it is a materially different kind of change than adding one more graph-native resource type. Named queries, rules, and triggers are all constrained to SPARQL, a language that (even in its UPDATE form) cannot make a syscall, open a socket, read an arbitrary file, or execute at the operating-system level. Python is general-purpose code carrying the full ambient authority of whatever process runs it. Storing Python source in the graph therefore doesn't extend the existing "graph is truth" pattern into a new but similar case -- it introduces a live code-execution surface where none currently exists. That reframing doesn't disqualify the candidate; it does mean viability has to be assessed as a systems problem, not a schema problem, and several pieces of that problem are not yet specified anywhere in this document.

Execution isolation is the piece that matters most. Raw in-process `exec()` of the stored source is the simplest to build and the most dangerous to run: the function executes with HolonBridge's own Fuseki credentials, filesystem access, and network egress, which means a function -- malicious, buggy, or simply unanticipated in its effects -- could rewrite the NamedFunction registry itself, escalating from "one admin-approved function" to "arbitrary future functions," entirely inside the graph, without a second gate ever being crossed. True in-interpreter sandboxing has a long history of escape bugs and isn't a credible substitute for real isolation. Full WASM sandboxing is the principled alternative but is probably premature here specifically: the tracking-operation functions this design exists to serve (Brier scoring, entropy-gain, saturation counting) are numeric and may eventually want `numpy` or `scipy`, and WASM-compiled scientific Python remains genuinely difficult today. The recommended middle path is a restricted subprocess, or small pool of them, supervised and restarted by HolonBridge itself (preserving the liveness-coupling property already established for the SERVICE-endpoint mechanism above), with no filesystem or network capability and a hard allow-list of importable modules -- `math`, `statistics`, cautiously `numpy` -- excluding anything that could import `os`, `subprocess`, or `socket`. This is not sandboxing in the research sense, but it closes the paths that matter (exfiltration, lateral movement, self-escalation) at a cost proportionate to the actual functions this design is meant to carry.

It is worth being honest about what "risk is small while admin is narrowly held to Kurt personally" actually means here: that is a mitigation by policy (who currently holds admin), not by mechanism (what the executed code can actually do once it runs). That is a reasonable interim answer given today's trust model, but the two claims should not be conflated -- the design is not inherently safe, it is currently safe because of a narrow role assignment that could change.

A handful of other pieces need deciding before this is buildable, none of them exotic on their own. Versioning is not automatically free just because the source lives in the graph -- overwriting `hb:pythonSource` in place loses history unless a discipline is chosen: either a new `hb:NamedFunction` IRI per version with a "current" pointer, or RDF 1.2 reification timestamping each version of the literal. Registration needs to do more than accept a write -- at minimum a `compile()` step and, ideally, a dry run against synthetic inputs matching the function's declared `hb:inputVariable`s, so that a broken definition fails the write itself rather than failing silently on its first live invocation, which for anything on the synchronous SHACL-gating path means every write using that shape starts failing before anyone notices why. Hot reload needs a stated concurrency policy, not just a `reload_named_functions` tool: SPARQL queries are stateless per call, so swapping them mid-flight is harmless, but a compiled Python callable is not automatically safe to swap under an in-flight invocation, so the simplest correct rule is that in-flight calls finish on the version they started with and only new calls pick up a reload. And the "free provenance" benefit this candidate is largely justified by has to actually be built: storing source in the graph tells you what could have run, not what did -- a per-invocation audit record (function id, bound inputs, output, and the exact source version that produced it), kept the same append-only way the rest of HGA's event graph already works, is what turns that benefit from an assumption into a fact.

The one gap worth naming as an opportunity rather than only a cost: `hb:implementationRef` functions get code review and CI for free, because they live in an ordinary codebase with an ordinary pull request in front of every change. Graph-native functions bypass that apparatus by construction -- there is no pull request for a graph edit. This stack already has the right shape to reintroduce a review gate without falling back to git, though: the SCE ingestion pipeline's `holon:CandidateStatus`/`holon:RegisteredStatus` split for holon registration is exactly the pattern a NamedFunction's registration could reuse -- draft a function as a candidate, run it through compile-plus-dry-run (and, where warranted, a human look) before promoting it to registered and live. Doing this would mean the graph-native candidate's missing review step is not actually missing, just moved into the graph itself.

Taken together, the recommendation is to treat the graph-native candidate as a second phase rather than a same-release alternative to `hb:implementationRef`. Shipping the codebase-allow-list version first proves out the SERVICE/registry/pipeline-stage mechanics already designed above against real, reviewed functions, without also having to solve process isolation and a graph-native review workflow in the same pass. `hb:pythonSource` becomes viable once the restricted-execution environment and the candidate/registered workflow both exist as real mechanisms, not before.

## Candidate: plugin architecture, bypassing admin-graph installation entirely

Kurt raised a third candidate the same session: instead of choosing between `hb:implementationRef` referencing a callable already shipped in the main HolonBridge codebase, or `hb:pythonSource` storing source in the admin graph, formalize a proper plugin system -- Python packages built and installed independently of HolonBridge's own release cycle, each declaring which NamedFunction ids it implements via a standard extension-point mechanism. Python's own `importlib.metadata` entry points are the mature, nothing-novel way to do this: a plugin's `pyproject.toml` declares an entry point in a HolonBridge-defined group (e.g. `[project.entry-points."holonbridge.named_functions"]` `saturation-check = "causalspark_plugins.tracking:saturation_check"`), and HolonBridge scans installed distributions for that group at startup. New code is added by installing a package into HolonBridge's Python environment -- `pip install`, or baking it into the container image -- not by writing to the admin graph at all.

This is a genuine refinement of `hb:implementationRef`, not a fourth unrelated idea, and it answers directly the open question the allow-list candidate left unresolved above ("how does a method get allow-listed and what prevents registering something off that list"): the allow-list is whatever is actually installed, discovered structurally through entry points, rather than a hand-maintained list inside the main codebase.

It's worth being precise about what "bypasses the admin layer" actually buys, because it's real but narrower than it first sounds. It removes the admin graph-write as a vector for installing new code -- a compromised or malicious admin credential can no longer, by itself, get arbitrary Python running inside HolonBridge, which is exactly the single-gate collapse the graph-native-source candidate couldn't avoid. But it does this by moving the installation gate to deploy/ops access (whoever can get a package into the running environment), not by removing the need for a gate. And it does not, by itself, decide whether a newly installed plugin's functions go live the moment the package is present, or only once something -- ideally still an admin-gated step -- registers or activates them as callable `hb:NamedFunction` resources. The recommendation is to keep that activation step: install should not automatically mean live, the same way a candidate holon isn't automatically registered just by existing. Skipping it trades one single-gate problem for a different one -- dropping a plugin into the environment would itself become an entirely ungated code-execution event.

It's also worth separating two things this idea's phrasing conflates: being a plugin and being isolated are independent properties. A plugin loaded in-process still runs with HolonBridge's full ambient authority once loaded -- the entry-point mechanism controls who can get code onto the machine, not what that code can do once it's there. The restricted-subprocess recommendation from the systems assessment above still applies and composes cleanly with this candidate: load each plugin into its own supervised, privilege-limited worker process rather than into HolonBridge's main process, so the plugin architecture and the isolation strategy solve two different problems at once instead of one problem twice.

One thing worth clarifying rather than assuming: who "users" refers to in this candidate matters enormously to the assessment. If it means Kurt, or whoever operates HolonBridge's deployment pipeline, this is a clean, standard extension-point pattern riding on an already-existing trust boundary -- the same one that already gates every other code change to the system. If it means end users of the platform -- personas, external API callers, or anyone without deploy access -- submitting their own plugins, that is a substantially harder problem, closer to running untrusted third-party native code, and the admin-graph question becomes secondary to a much bigger one: sandboxing arbitrary user-submitted code is a different order of engineering effort than anything else considered in this document, regardless of whether the entry point is a graph write or a plugin install.

Net assessment: of the three candidates now on the table, this is the strongest for anything Kurt or a small, deploy-trusted team would write. It restores a genuine two-gate model -- deploy access, then admin activation -- without `hb:pythonSource`'s single-gate collapse, and it can be built almost entirely on mature, existing Python packaging primitives rather than inventing new mechanics. It is not a replacement for `hb:pythonSource`'s use case, though: graph-native source is still the only candidate here that lets a tracked algorithm change without any deploy step at all, however small. The three candidates now sit on a real, ordered tradeoff: `hb:implementationRef` against the main codebase (safest, requires main-codebase changes), plugin-sourced `hb:implementationRef` (nearly as safe, decouples release cycles via standard packaging), `hb:pythonSource` (fastest to change, weakest gate). Which point on that line is right depends on how often new tracking algorithms are expected to be added and by whom -- a question this document can name but not answer.

## Resolved: what "admin" and "users" mean in a federated deployment

Kurt clarified the ambiguity flagged in the plugin-architecture section directly: "users" who compile and install plugins are admins -- specifically, whoever compiles and sets up a given HolonBridge instance in the first place. This resolves the open question in favour of the safer reading, and it sharpens the plugin architecture's case rather than merely confirming it, because it surfaces a second, distinct benefit the earlier assessment didn't have in view.

HolonBridge is not a single deployment with a single admin -- it is a federated architecture, and different instances are administered by different people for different reasons. Two node administrators can have genuinely different needs: one running a client-facing federated node has no reason to want, and every reason not to want, the capabilities a co-founder's own instance needs for internal architecture work. Under `hb:implementationRef` against the shared main codebase, every deployment ships the same binary, so every capability the codebase has ever accumulated is *present*, whether or not it's *registered*, at every node -- separation between what one admin's node can do and what another's can do exists only at the level of which `hb:NamedFunction` resources happen to be registered in that node's own graph, not at the level of what code is actually on the machine. Under the plugin architecture, that separation becomes real rather than merely nominal: a node administrator installs only the plugin packages their own instance needs, so a node built for federated client work simply does not have a co-founder's internal-analysis plugin installed at all -- not unregistered, absent. That is a materially stronger property than a permission check sitting on top of a shared capability surface, since it holds even against bugs or compromises in the registration/permission layer itself: there is no function to invoke if the code was never on the machine.

This also settles how ordinary end users of the platform -- as distinct from the node administrators just described -- relate to Named Functions at all: they don't, directly. Per Kurt's description, a user never authors a SPARQL `SERVICE` clause or invokes a NamedFunction by id themselves; they only ever reach one indirectly, through a named query a node's own admin has already authored, registered, and (implicitly) vetted, which happens to embed a `SERVICE` call to a sanctioned function inside its own query text. A user calling that named query by id, with its declared parameters, has no path to any Named Function the admin didn't choose to expose this way.

That said, this only holds together end to end if two things are also true, and neither has been nailed down yet in what this document has captured so far. First, named-query *registration* itself needs to be gated at least as tightly as Named Function registration is -- otherwise the model has a hole in it: nothing so far prevents a lower-trust caller from registering their *own* named query, with its own `SERVICE` clause pointing at a Named Function whose id they know or can discover, and thereby self-granting exactly the access this design is meant to withhold from them. The Named Function admin-only gate is airtight on its own terms; the surrounding named-query layer is only as strong as whatever governs who may register a named query at all, and that governance hasn't been specified in any of this document's versions to date. Second, once a named query is safely authored by a trusted admin, its own declared *parameters* are still user-supplied at call time, and the `hb:` vocabulary's parameter binding is raw `{{placeholder}}` string substitution -- exactly the kind of substitution that shouldn't sit directly upstream of a call into compiled, potentially plugin-sourced code. The `hquery:` vocabulary's alternative -- binding via a `VALUES` clause appended after the query, which can't corrupt the query's structure the way string substitution in principle could -- is the safer of the two existing mechanisms for any named query that gates a Named Function specifically, even though `hb:` remains a reasonable default everywhere else. Recommendation, not yet a decision: any named query that embeds a `SERVICE` call to a registered Named Function should be authored in `hquery:`, not `hb:`, precisely because this is the one seam in the whole design where ordinary user input meets code that actually executes.

## What's still open

Several things here are explicitly not decided and should not be read as more settled than they are. Whether a registered function's implementation is expressed via `hb:implementationRef` against the main codebase (a reference to a pre-vetted, code-reviewed callable — class methods on an allow-listed registry, per Kurt's original steer), via `hb:implementationRef` sourced from an independently-installed plugin (Kurt's third-candidate refinement, using standard Python entry points), or via `hb:pythonSource` (the source itself stored as graph data, compiled independently of a deploy cycle — Kurt's second-candidate addition) is now explicitly a three-way open question rather than a single design with mechanics still to fill in; see "Candidate: graph-native Python source", "Candidate: plugin architecture", and the "Systems assessment" section above for the tradeoffs and the net recommendation (main-codebase allow-list is safest; plugin-sourced is nearly as safe while decoupling release cycles; graph-native source is fastest to change but weakest-gated). None of the three candidates' mechanics are fully worked out: the main-codebase allow-list path still needs how a method gets allow-listed and what prevents registering something off that list; the plugin path still needs its activation-gate decision (does an installed plugin's function go live automatically, or only once admin-registered) and whether plugins run in-process or in their own isolated worker; the graph-native-source path still needs a chosen isolation strategy (the "Systems assessment" section recommends a restricted, privilege-limited subprocess over raw in-process exec or full WASM sandboxing, but this is a recommendation, not a decision), a versioning discipline for `hb:pythonSource` edits, a registration-time compile-plus-dry-run validation story, a reload/concurrency policy for in-flight invocations, an invocation-audit mechanism, and a candidate/registered review workflow modelled on the SCE ingestion pipeline's status split. Who is meant to install a plugin is now resolved (per-instance node admins, not end users of the platform — see "Resolved: what 'admin' and 'users' mean in a federated deployment" above), but that resolution opens two further items that were not visible before it: named-query *registration* authority has not itself been specified as gated at all, let alone gated as tightly as Named Function registration, and without that gate a lower-trust caller could self-author a query that reaches a Named Function the admin-only rule was meant to keep them from; and any named query that does embed a `SERVICE` call to a registered Named Function should, on this document's recommendation, use `hquery:`'s typed `VALUES`-append parameter binding rather than `hb:`'s raw string substitution, since that is the one seam in the whole design where ordinary user input meets executing code, but this too is a recommendation captured here, not a decision made. The illustrative `hb:PythonComputeStage` pipeline-stage sketch in the appendix has not been checked against the real pipeline manifest vocabulary in `holonbridge/routes/pipeline.py` — that reconciliation is a prerequisite for implementation, not an afterthought, and this document should not be read as claiming that vocabulary already exists in the codebase. Per-shape, per-graph decisions about which SHACL variant (chokepoint vs. federated) applies where haven't been made for any specific graph yet, including David's own census shape — this document lays out the decision criterion, not the decisions themselves. And the entropy-gain formalization of gap-mint prioritisation, while well-defined as a formula, still needs someone to decide what "prior distribution" and "candidate evidence model" concretely mean for a given gap category before it's implementable, not just definable.

## Technical appendix — for whoever builds this

### Named Function vocabulary (primary block, 41 triples / 11 subjects)

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
    dct:description "Opaque reference to the registered Python callable backing this function (exact shape -- dotted import path, class-method reference, or fixed allow-list key -- deliberately left open pending implementation planning). One of two candidate ways to express a NamedFunction's implementation -- see hb:pythonSource for the other -- neither chosen yet."@en .

hb:pythonSource a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:range xsd:string ;
    rdfs:label "inline Python source"@en ;
    dct:description "Alternative to hb:implementationRef: the function's actual Python source, stored as a literal in an admin-only graph and compiled by HolonBridge independently of its own deploy cycle, rather than referencing a pre-vetted callable already shipped in the codebase. A candidate implementation strategy raised 2026-09-05, not yet chosen over hb:implementationRef -- see 'Candidate: graph-native Python source' in the body for the tradeoff (mainly: no code-review gate before execution, versus automatic graph-native provenance/versioning)."@en .

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

### Systems-assessment vocabulary sketch (illustrative, not decided — 47 triples / 14 subjects)

Names the concepts the "Systems assessment" section above argues are needed if `hb:pythonSource` is ever chosen over `hb:implementationRef`: a declared isolation strategy per function, a candidate/registered review-status split mirroring the SCE ingestion pipeline, and an append-only invocation-audit event. None of this is a decision — it exists so the shape of the requirement is captured alongside the prose, the same way `hb:PipelineStage` was captured as a placeholder above pending reconciliation with real pipeline code.

```turtle
@prefix hb:   <https://w3id.org/holonbridge/> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix prov: <http://www.w3.org/ns/prov#> .

hb:IsolationStrategy a rdfs:Class ;
    rdfs:label "Isolation Strategy"@en ;
    dct:description "Illustrative sketch only -- names the execution-isolation options considered for hb:pythonSource, not a decided vocabulary. Applies only to the graph-native-source candidate; hb:implementationRef functions run inside HolonBridge's own already-reviewed codebase and don't need a declared isolation strategy."@en .

hb:InProcessExec a hb:IsolationStrategy ;
    rdfs:label "in-process exec"@en ;
    dct:description "Simplest, not recommended: runs with HolonBridge's own Fuseki credentials, filesystem access, and network egress."@en .

hb:RestrictedSubprocess a hb:IsolationStrategy ;
    rdfs:label "restricted subprocess"@en ;
    dct:description "Recommended default: a supervised, privilege-limited subprocess (or small pool) with no filesystem or network capability and a hard module allow-list."@en .

hb:WasmSandbox a hb:IsolationStrategy ;
    rdfs:label "WASM sandbox"@en ;
    dct:description "Principled but likely premature given current difficulty compiling scientific Python (numpy/scipy) to WASM."@en .

hb:invocationIsolation a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:range hb:IsolationStrategy ;
    rdfs:label "declared isolation strategy"@en ;
    dct:description "Which isolation strategy backs this function's execution, when hb:pythonSource is used. Not applicable to hb:implementationRef functions."@en .

hb:registrationStatus a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:label "registration status"@en ;
    dct:description "Candidate vs. registered status for a NamedFunction, mirroring the holon:CandidateStatus/holon:RegisteredStatus split the SCE ingestion pipeline already uses for holon registration -- proposed here as the review gate a graph-native function otherwise lacks by construction, since no pull request exists for a graph edit."@en .

hb:CandidateFunction a rdfs:Class ;
    rdfs:label "candidate function status"@en .

hb:RegisteredFunction a rdfs:Class ;
    rdfs:label "registered function status"@en .

hb:FunctionInvocationEvent a rdfs:Class ;
    rdfs:subClassOf prov:Activity ;
    rdfs:label "Function Invocation Event"@en ;
    dct:description "An append-only audit record of one NamedFunction invocation, in the same event-graph style as the rest of HGA. Makes the graph-native candidate's provenance benefit an actual fact rather than an assumption: source in the graph records what could have run, this records what did."@en .

hb:invokedFunction a rdf:Property ;
    rdfs:domain hb:FunctionInvocationEvent ;
    rdfs:range hb:NamedFunction ;
    rdfs:label "invoked function"@en .

hb:invocationSourceVersion a rdf:Property ;
    rdfs:domain hb:FunctionInvocationEvent ;
    rdfs:range xsd:string ;
    rdfs:label "source version invoked"@en .

hb:invocationInput a rdf:Property ;
    rdfs:domain hb:FunctionInvocationEvent ;
    rdfs:label "invocation input binding"@en .

hb:invocationOutput a rdf:Property ;
    rdfs:domain hb:FunctionInvocationEvent ;
    rdfs:label "invocation output binding"@en .

hb:invocationTimestamp a rdf:Property ;
    rdfs:domain hb:FunctionInvocationEvent ;
    rdfs:range xsd:dateTime ;
    rdfs:label "invocation timestamp"@en .
```

### Plugin-sourced implementation vocabulary sketch (illustrative, not decided — 10 triples / 2 subjects)

Names the two additional fields `hb:implementationRef` would need if sourced from an installed plugin package via a Python entry point (the "Candidate: plugin architecture" section above) rather than a method already living in HolonBridge's main codebase. Not a decision; captured for the same reason every other illustrative block in this appendix is.

```turtle
@prefix hb:   <https://w3id.org/holonbridge/> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct:  <http://purl.org/dc/terms/> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

hb:pluginPackage a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:range xsd:string ;
    rdfs:label "plugin package name"@en ;
    dct:description "Illustrative sketch only, not decided. Name of the installed Python distribution providing this function's implementation, when hb:implementationRef is plugin-sourced via a standard entry point rather than a method in HolonBridge's own main codebase."@en .

hb:pluginEntryPoint a rdf:Property ;
    rdfs:domain hb:NamedFunction ;
    rdfs:range xsd:string ;
    rdfs:label "plugin entry-point name"@en ;
    dct:description "Illustrative sketch only, not decided. The entry-point name within the holonbridge.named_functions group (Python importlib.metadata entry points), used to resolve this function's callable from an installed plugin package at startup."@en .
```

### Named-query gating vocabulary sketch (illustrative, not decided — 4 triples / 1 subject)

Names the one property the "Resolved: what 'admin' and 'users' mean" section above recommends: a way to declare, on a registered named query, which Named Function(s) it gates via an embedded `SERVICE` call -- so that the surrounding named-query layer (the only path Kurt describes an ordinary user having to a Named Function at all) is at least auditable, even before its registration-authority question is settled.

```turtle
@prefix hb:   <https://w3id.org/holonbridge/> .
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dct:  <http://purl.org/dc/terms/> .

hb:gatesNamedFunction a rdf:Property ;
    rdfs:range hb:NamedFunction ;
    rdfs:label "gates named function"@en ;
    dct:description "Illustrative sketch only, not decided. Declares that a registered named query (hb:NamedQuery or hquery:NamedQuery) embeds a SERVICE clause invoking the given hb:NamedFunction. Recommended so that a query gating a NamedFunction is discoverable, and so that registering it can be required to carry the same registration authority as the function it gates -- since, per Kurt's clarification, a named query is the only path by which an ordinary user ever reaches a NamedFunction at all."@en .
```
