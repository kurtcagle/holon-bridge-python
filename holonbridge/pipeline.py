"""Pipeline manifests — a DAG of DataBook stages, executed in dependency order.

A manifest is RDF in the ``build:`` vocabulary
(``https://w3id.org/databook/ns#``): ``build:Source`` nodes with no
dependencies, ``build:Stage`` nodes that transform, and ``build:Target``
nodes that are the goal. ``build:dependsOn`` gives the edges.

Because it is RDF rather than a build DSL, the dependency graph is queryable —
change impact is ``build:dependsOn+`` and nothing more. This module adds the
two things a queryable manifest still needs: a total order to run in, and
something that actually runs.

**One graph per manifest.** Each is registered at
``urn:{dataset}:pipeline:{id}`` with an index entry in
``urn:{dataset}:pipelines``. A manifest is then replaceable and droppable on
its own, and there is no ambiguity about which triples belong to which
pipeline — the problem you get from putting several manifests in one graph.

**What the bridge can and cannot run.** Stages declaring a ``sparql``
transformer run a named rule; ``shacl`` stages validate. ``llm``, ``human``,
``external`` and ``composite`` stages are recorded as ``Deferred``, not
failed — the bridge cannot run a human, and pretending a stage succeeded
because nothing executed would be worse than saying so.

**Prompt-based ingestion (``llm`` stages with a prompt).** A stage still
never gets executed by the bridge — the bridge cannot call a model itself,
and pretending otherwise would be the same mistake ``DEFERRED`` already
exists to avoid. What changed: a ``llm`` stage may declare ``build:promptText``,
and when it does, ``_run_llm_stage`` resolves the two fixed placeholders a
prompt template may use — ``{{input}}`` (this run's own ``params``) and
``{{context}}`` (the captured result of this stage's one upstream
context-only dependency, if any) — and records the fully rendered prompt on
``StageRecord.result``. The stage is still reported ``Deferred``; only what
accompanies that status changed, from a placeholder sentence to a usable
prompt. Whatever produced the prompt's actual completion outside the bridge
(a human, an external model call, a CNL parse) then submits the resulting
Turtle back through ``triggers.propose_pipeline_candidate`` — staged and
gated exactly like a Named Trigger's own review-required candidates, never
written directly. This is deliberately the same reviewed-then-merge
contract as everything else that writes, not a second, less-audited path.

**Extra param slots.** Beyond ``{{input}}``/``{{context}}``, every other key
in this run's own ``params`` is exposed as its own ``{{name}}`` slot too —
e.g. a caller passing ``params={"input": ..., "submitter": "<person IRI>"}``
gets a working ``{{submitter}}`` placeholder in the prompt template, with no
bespoke field needed for every piece of run-specific metadata a template
might want. This exists because a completion produced outside the bridge
(a human, an external model) has no other reliable way to learn who is
actually running this pipeline invocation — the bridge knows that (or
whatever else the caller supplies) and can simply hand it over. ``{{input}}``
keeps its own reserved, whole-payload meaning (see ``_stringify_input``) and
is never duplicated or overridden by this — an ``"input"`` key in ``params``
is consumed only by the ``{{input}}`` slot, not also exposed under its own
name.

**Context-only stages.** A ``sparql``/``rule`` stage that reads the graph to
prepare a mapping, without itself writing anywhere, opts in explicitly with
``build:contextOnly "true"`` — this is *not* inferred from an absent
``targetGraph``, because an absent ``targetGraph`` on a stage already has an
established, different meaning here: fall through to the named rule's own
declared target (see ``_run_rule_stage``). Overloading that absence to also
mean "read-only" would silently turn existing real writes into no-ops.
A context-only stage is never executed via ``execute_named_rule`` (which
writes for real); instead its rule is bound and previewed read-only via
``client.construct`` — the same dry-run shape ``triggers.evaluate_triggers``
already uses for a review-required trigger firing — and the result is kept
only in this run's own ``context_results`` map (keyed by the stage's IRI),
for a dependent ``llm`` stage's ``{{context}}`` to resolve against. Nothing
about a context-only stage's read is written to any graph, logged, or
otherwise persisted beyond this one run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .conn import Conn
from .fuseki import FusekiClient, FusekiError
from .messages import Message, StageRecord
from .named_projections import render_template
from .named_rules import RuleError, bind_rule, execute_named_rule, load_named_rules
from .params import ParameterError
from .rdfutil import collect, local_name, pick, pick_all, truthy
from .shacl import validate_full

log = logging.getLogger("holonbridge.pipeline")

BUILD = "https://w3id.org/databook/ns#"

NODE_KINDS = ("Target", "Stage", "Source")

#: Transformers the bridge executes. Everything else is recorded as deferred.
EXECUTABLE = {"sparql", "rule", "shacl", "validate", "projection", "external", "agent"}
#: Still not executed by the bridge. An ``external`` or ``xslt`` stage that
#: names a projection hook is executable — the hook is exactly the contract
#: for handing work to something the bridge does not implement.
DEFERRED = {"llm", "human", "composite", "xslt"}

_NODE_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "identifier", "stageId"),
    "label": ("label", "title"),
    "transformer": ("transformer",),
    "input_type": ("inputType",),
    "output_type": ("outputType",),
    "order": ("order",),
    "rule": ("rule", "namedRule", "ruleId"),
    "shapes": ("shapes", "shapesGraph"),
    "projection": ("projection", "hook", "projectionHook"),
    "target_graph": ("targetGraph", "produces", "target"),
    "prompt_text": ("promptText", "prompt"),
    # CHANGED 2026-11-01: ``agent`` stages place a call to an agent endpoint.
    "agent": ("agentEndpoint", "agent"),
    "agent_context": ("agentContext",),
}
_DEPENDS = ("dependsOn",)
#: Explicit opt-in for a read-only stage -- see the module docstring for why
#: this is a dedicated predicate rather than inferred from an absent
#: ``targetGraph``.
_CONTEXT_ONLY = ("contextOnly",)


class PipelineError(RuntimeError):
    """A manifest cannot be ordered or run as written."""


@dataclass
class PipelineNode:
    iri: str
    id: str
    kind: str
    label: str = ""
    transformer: str = ""
    input_type: str = ""
    output_type: str = ""
    order: int | None = None
    rule: str = ""
    shapes: str = ""
    projection: str = ""
    target_graph: str = ""
    depends_on: list[str] = field(default_factory=list)
    #: The stage's own prompt template, from ``build:promptText`` -- only
    #: meaningful on an ``llm`` stage. See the module docstring.
    prompt_text: str = ""
    #: Explicit opt-in: read the graph via this stage's rule, write nothing.
    #: See the module docstring for why this cannot be inferred from an
    #: absent ``target_graph``.
    context_only: bool = False
    #: ``agent`` stages: the id of the ``hb:AgentEndpoint`` to call, and an
    #: optional named query whose result travels with the request. The task text
    #: is ``prompt_text``. See ``_run_agent_stage``.
    agent: str = ""
    agent_context: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "kind": self.kind,
            "label": self.label or self.id,
            "transformer": self.transformer,
            "inputType": self.input_type,
            "outputType": self.output_type,
            "order": self.order,
            "rule": self.rule,
            "shapes": self.shapes,
            "projection": self.projection,
            "targetGraph": self.target_graph,
            "dependsOn": self.depends_on,
            "promptText": self.prompt_text,
            "contextOnly": self.context_only,
            "agent": self.agent,
            "agentContext": self.agent_context,
            "executable": self.transformer.lower() in EXECUTABLE,
        }


@dataclass
class Manifest:
    id: str
    graph: str
    nodes: dict[str, PipelineNode] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def by_id(self, node_id: str) -> PipelineNode | None:
        for node in self.nodes.values():
            if node.id == node_id:
                return node
        return None

    def summary(self, order: list[PipelineNode] | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "graph": self.graph,
            "nodes": [n.summary() for n in self.nodes.values()],
            "order": [n.id for n in (order or [])],
            "warnings": self.warnings,
        }


# --- loading ------------------------------------------------------------------


def _manifest_query(graph: str) -> str:
    kinds = " || ".join(f'STRENDS(STR(?type), "{kind}")' for kind in NODE_KINDS)
    return f"""SELECT ?n ?type ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?n a ?type .
    FILTER( {kinds} )
    ?n ?p ?o .
  }}
}}"""


async def load_manifest(
    client: FusekiClient, conn: Conn, pipeline_id: str
) -> Manifest:
    """Load one manifest from ``urn:{dataset}:pipeline:{id}``."""
    graph = conn.scoped("pipelines", pipeline_id)
    manifest = Manifest(id=pipeline_id, graph=graph)

    try:
        rows = (await client.select(conn, _manifest_query(graph)))["results"]["bindings"]
    except (FusekiError, KeyError) as exc:
        manifest.warnings.append(f"manifest load from <{graph}> failed: {exc}")
        return manifest

    types: dict[str, str] = {}
    for row in rows:
        types.setdefault(row["n"]["value"], row["type"]["value"])

    for iri, props in collect(rows, "n").items():
        kind = local_name(types.get(iri, ""))
        kind = next((k for k in NODE_KINDS if kind.endswith(k)), "Stage")

        raw_order = pick(props, _NODE_FIELDS["order"])
        try:
            order = int(raw_order) if raw_order is not None else None
        except ValueError:
            order = None
            manifest.warnings.append(
                f"{local_name(iri)}: order {raw_order!r} is not an integer; ignored"
            )

        context_only_raw = pick(props, _CONTEXT_ONLY)
        context_only = truthy(context_only_raw) if context_only_raw is not None else False
        target_graph = pick(props, _NODE_FIELDS["target_graph"]) or ""
        if context_only and target_graph:
            manifest.warnings.append(
                f"{local_name(iri)} declares both contextOnly and a targetGraph "
                "-- a stage that both reads for context and writes is a "
                "modeling error; targetGraph is ignored while contextOnly holds"
            )

        manifest.nodes[iri] = PipelineNode(
            iri=iri,
            id=pick(props, _NODE_FIELDS["id"]) or local_name(iri),
            kind=kind,
            label=pick(props, _NODE_FIELDS["label"]) or "",
            transformer=(pick(props, _NODE_FIELDS["transformer"]) or "").lower(),
            input_type=pick(props, _NODE_FIELDS["input_type"]) or "",
            output_type=pick(props, _NODE_FIELDS["output_type"]) or "",
            order=order,
            rule=pick(props, _NODE_FIELDS["rule"]) or "",
            shapes=pick(props, _NODE_FIELDS["shapes"]) or "",
            projection=pick(props, _NODE_FIELDS["projection"]) or "",
            target_graph=target_graph,
            depends_on=pick_all(props, _DEPENDS),
            prompt_text=pick(props, _NODE_FIELDS["prompt_text"]) or "",
            context_only=context_only,
            agent=pick(props, _NODE_FIELDS["agent"]) or "",
            agent_context=pick(props, _NODE_FIELDS["agent_context"]) or "",
        )

    manifest.warnings.extend(_type_mismatches(manifest))
    return manifest


def _type_mismatches(manifest: Manifest) -> list[str]:
    """Report stages whose declared input does not match what feeds them.

    A warning, not an error: the declaration is a hint the manifest author
    supplies, and a mismatch is more often a stale annotation than a broken
    pipeline.
    """
    out: list[str] = []
    for node in manifest.nodes.values():
        if not node.input_type:
            continue
        for dependency in node.depends_on:
            producer = manifest.nodes.get(dependency)
            if producer and producer.output_type and producer.output_type != node.input_type:
                out.append(
                    f"{node.id} expects {node.input_type!r} but {producer.id} "
                    f"produces {producer.output_type!r}"
                )
    return out


async def list_pipelines(client: FusekiClient, conn: Conn) -> list[dict[str, Any]]:
    """Registered manifests, from the index graph."""
    index = conn.graph("pipelines")
    try:
        results = await client.select(
            conn,
            f"""SELECT ?id ?label ?graph ?registeredAt
WHERE {{
  GRAPH <{index}> {{
    ?m a <{BUILD}Manifest> ;
       <{BUILD}pipelineId> ?id ;
       <{BUILD}graph> ?graph .
    OPTIONAL {{ ?m <http://www.w3.org/2000/01/rdf-schema#label> ?label }}
    OPTIONAL {{ ?m <{BUILD}registeredAt> ?registeredAt }}
  }}
}}
ORDER BY ?id""",
        )
    except (FusekiError, KeyError) as exc:
        log.warning("pipeline index load failed: %s", exc)
        return []

    return [
        {
            "id": row["id"]["value"],
            "label": row.get("label", {}).get("value", row["id"]["value"]),
            "graph": row["graph"]["value"],
            "registeredAt": row.get("registeredAt", {}).get("value", ""),
        }
        for row in results.get("results", {}).get("bindings", [])
    ]


# --- ordering -----------------------------------------------------------------


def topological_order(manifest: Manifest) -> list[PipelineNode]:
    """Kahn's algorithm over ``build:dependsOn``, with ``build:order`` as tiebreak.

    Dependencies come first, since a stage's inputs must exist before it runs.
    Where several stages are ready at once, declared order decides; a stage
    with no declared order goes after those that have one, consistent with how
    named rules are ordered.

    A cycle raises rather than silently dropping the nodes involved.
    """
    nodes = manifest.nodes
    indegree = {iri: 0 for iri in nodes}
    dependents: dict[str, list[str]] = {iri: [] for iri in nodes}

    for iri, node in nodes.items():
        for dependency in node.depends_on:
            if dependency not in nodes:
                manifest.warnings.append(
                    f"{node.id} depends on <{dependency}>, which the manifest does not define"
                )
                continue
            indegree[iri] += 1
            dependents[dependency].append(iri)

    def sort_key(iri: str) -> tuple[bool, int, str]:
        node = nodes[iri]
        return (node.order is None, node.order if node.order is not None else 0, node.id)

    ready = sorted([iri for iri, degree in indegree.items() if degree == 0], key=sort_key)
    ordered: list[PipelineNode] = []

    while ready:
        current = ready.pop(0)
        ordered.append(nodes[current])
        for dependent in dependents[current]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready.sort(key=sort_key)

    if len(ordered) != len(nodes):
        cycle = sorted(nodes[iri].id for iri in nodes if indegree[iri] > 0)
        raise PipelineError(
            f"manifest {manifest.id!r} has a dependency cycle involving: {', '.join(cycle)}"
        )

    return ordered


# --- execution ----------------------------------------------------------------


async def run_pipeline(
    conn: Conn,
    client: FusekiClient,
    manifest: Manifest,
    message: Message,
    *,
    params: Mapping[str, Any] | None = None,
    stop_on_error: bool = True,
    requested_by: str | None = None,
) -> Message:
    """Run every stage in dependency order, recording each on the message.

    ``requested_by`` is the person on whose behalf the run is made. Stages run
    as the bridge, so it matters only to a stage that must act *as someone*:
    an ``agent`` stage places its call as this person, under their access.

    ``conn`` comes first and is a frozen value, so it is safe to hold across
    the await points of a background task — unlike a request object, which is
    not valid once the response has gone out.
    """
    order = topological_order(manifest)
    rules = await load_named_rules(client, conn)
    #: This run's own context-only results, keyed by the producing stage's
    #: IRI (matching how ``node.depends_on`` itself records dependencies) --
    #: never written to any graph, never persisted past this call. See the
    #: module docstring.
    context_results: dict[str, str] = {}

    for index, node in enumerate(order):
        record = StageRecord(
            name=node.id, transformer=node.transformer or node.kind.lower(), order=index
        )
        message.stages.append(record)

        if node.kind == "Source":
            record.status = "Skipped"
            record.detail = "source node — nothing to execute"
            continue

        transformer = node.transformer.lower()
        if transformer in {"external", "xslt"} and not node.projection:
            # An external stage with no hook has nothing the bridge can act on.
            record.status = "Deferred"
            record.detail = (
                f"{transformer!r} stage names no projection hook; nothing to hand over"
            )
            continue

        if transformer == "llm":
            _run_llm_stage(node, context_results, params, record)
            continue

        if transformer and transformer not in EXECUTABLE:
            record.status = "Deferred"
            record.detail = (
                f"{transformer!r} stages are not executed by the bridge"
                if transformer in DEFERRED
                else f"unknown transformer {transformer!r}"
            )
            continue

        try:
            if transformer == "agent":
                await _run_agent_stage(
                    conn, client, manifest, node, context_results, params, record, requested_by
                )
            elif node.projection or transformer in {"projection", "external"}:
                await _run_projection_stage(conn, client, node, record)
            elif node.context_only and (transformer in {"sparql", "rule"} or node.rule):
                result = await _run_context_stage(conn, client, node, rules, params, record)
                context_results[node.iri] = result
            elif transformer in {"sparql", "rule"} or node.rule:
                await _run_rule_stage(conn, client, node, rules, params, record)
            elif transformer in {"shacl", "validate"}:
                await _run_shacl_stage(conn, client, node, record)
            else:
                record.status = "Deferred"
                record.detail = "stage declares no transformer and no rule"
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised or not
            record.status = "Failed"
            record.detail = str(exc)
            if stop_on_error:
                message.error = f"stage {node.id!r} failed: {exc}"
                return message

    return message


async def _run_rule_stage(
    conn: Conn,
    client: FusekiClient,
    node: PipelineNode,
    rules,  # RuleLoadResult
    params: Mapping[str, Any] | None,
    record: StageRecord,
) -> None:
    if not node.rule:
        raise PipelineError(
            f"stage {node.id!r} declares a sparql transformer but names no rule"
        )
    rule = rules.by_id(node.rule)
    if rule is None:
        raise PipelineError(f"stage {node.id!r} names unknown rule {node.rule!r}")

    if node.target_graph and node.target_graph != rule.target_graph:
        # The manifest's target wins: the stage says where its output belongs
        # in this pipeline, which may differ from the rule's standalone default.
        rule = replace(rule, target_graph=node.target_graph)

    try:
        run = await execute_named_rule(conn, client, rule, params=params)
    except RuleError as exc:
        raise PipelineError(str(exc)) from exc

    record.status = "Completed"
    record.triples_written = run.triples_constructed
    record.detail = (
        f"{run.write_mode} into <{run.target_graph}>: "
        f"+{run.triples_added} / -{run.triples_removed}"
    )


def _stringify_input(params: Mapping[str, Any] | None) -> str:
    """Render this run's own ``params`` as the ``{{input}}`` slot's value.

    A single ``"input"`` key is used verbatim, as the raw payload text a
    caller supplied for exactly this purpose. Otherwise, when there is any
    other content in ``params``, it is rendered as JSON so a mapping-shaped
    input is still visible in the prompt rather than silently dropped.
    Empty when ``params`` is empty or absent -- no run-supplied payload for
    the placeholder to resolve to.
    """
    if not params:
        return ""
    if "input" in params:
        return str(params["input"])
    import json

    return json.dumps(params, default=str)


def _extra_param_slots(params: Mapping[str, Any] | None) -> dict[str, str]:
    """Every ``params`` key besides ``"input"`` itself, stringified, so a
    prompt template can name any of them directly as its own ``{{slot}}``.

    See the module docstring's "Extra param slots" section for why this
    exists. ``"input"`` is deliberately excluded here even though it is
    also a key in ``params`` — it already has its own reserved slot via
    :func:`_stringify_input`, and exposing it a second time under its own
    name would be pure duplication, not a second distinct piece of data.
    """
    if not params:
        return {}
    return {str(key): str(value) for key, value in params.items() if key != "input"}


def _context_slot(node: PipelineNode, context_results: dict[str, str]) -> tuple[str, str]:
    """The ``{{context}}`` value for a stage, and a note saying where it came from."""
    context_deps = [d for d in node.depends_on if d in context_results]
    if len(context_deps) == 1:
        return context_results[context_deps[0]], f"context from {local_name(context_deps[0])!r}"
    if not context_deps:
        return "", "no context dependency"
    return "", f"{len(context_deps)} context dependencies; {{context}} left empty"


def _run_llm_stage(
    node: PipelineNode,
    context_results: dict[str, str],
    params: Mapping[str, Any] | None,
    record: StageRecord,
) -> None:
    """Render an ``llm`` stage's prompt. Still never executed by the bridge
    -- ``record.status`` stays ``"Deferred"`` either way, per ``DEFERRED``
    above -- but a stage with ``build:promptText`` now hands back a
    ready-to-send prompt on ``record.result`` instead of only a placeholder
    sentence. A stage with no ``promptText`` keeps the exact original
    detail text, unchanged, for backward compatibility.

    ``{{input}}`` resolves via :func:`_stringify_input`. ``{{context}}``
    resolves to this stage's one upstream context-only dependency's
    captured result; with zero or more than one such dependency it
    resolves to an empty string rather than guessing, and that is visible
    in ``record.detail``. See the module docstring for why a stage must be
    a *declared* context-only dependency to feed ``{{context}}`` at all.
    Every other ``params`` key is also available as its own ``{{name}}``
    slot via :func:`_extra_param_slots` — see the module docstring's "Extra
    param slots" section.
    """
    if not node.prompt_text:
        record.status = "Deferred"
        record.detail = "'llm' stages are not executed by the bridge"
        return

    context_value, context_note = _context_slot(node, context_results)

    rendered = render_template(
        node.prompt_text,
        {
            "input": _stringify_input(params),
            "context": context_value,
            **_extra_param_slots(params),
        },
    )

    record.status = "Deferred"
    record.result = rendered
    record.detail = (
        f"'llm' stages are not executed by the bridge; prompt rendered ({context_note})"
    )


async def _run_agent_stage(
    conn: Conn,
    client: FusekiClient,
    manifest: "Manifest",
    node: PipelineNode,
    context_results: dict[str, str],
    params: Mapping[str, Any] | None,
    record: StageRecord,
    requested_by: str | None,
) -> None:
    """Place a call to an agent endpoint. **Nothing is sent.**

    The stage's ``promptText`` is the task, rendered with the same slots an
    ``llm`` stage gets. The call is recorded as ``PendingApproval`` with the
    exact body a person will read before it leaves the bridge (a pipeline run
    never queues a call for policy dispatch: whoever ran it reviews it). The
    stage ends ``Deferred`` -- the same honest status an ``llm`` stage has, for
    work the bridge does not do inside the run -- with the call's id on
    ``record.result``. The agent's answer, if it includes Turtle, comes back
    through the candidate queue, not this run.

    It is placed as ``requested_by`` and fails if the run has none: a scheduled
    pipeline has no person, and an agent call must always be someone's.
    """
    from .agents import service as agent_service  # noqa: PLC0415 - cycle with triggers
    from .agents.registry import load_endpoints  # noqa: PLC0415
    from .named_queries import load_named_queries  # noqa: PLC0415
    from .trigger_authz import TriggerDenied  # noqa: PLC0415

    if not node.agent:
        raise PipelineError(f"stage {node.id!r} is an agent stage but names no agentEndpoint")
    if not node.prompt_text.strip():
        raise PipelineError(f"stage {node.id!r} is an agent stage but has no promptText (the task)")
    if not requested_by:
        raise PipelineError(
            f"stage {node.id!r} places an agent call on behalf of whoever ran the pipeline, "
            "and this run has no identified person (a scheduled pipeline cannot place agent "
            "calls; use a temporal trigger with onFireAgent instead)"
        )

    endpoint = (await load_endpoints(client, conn)).by_id(node.agent)
    if endpoint is None:
        raise PipelineError(f"stage {node.id!r} names unknown agent endpoint {node.agent!r}")
    context_query = None
    if node.agent_context:
        context_query = (await load_named_queries(client, conn)).by_id(node.agent_context)
        if context_query is None:
            raise PipelineError(f"stage {node.id!r} names unknown context query {node.agent_context!r}")

    context_value, _note = _context_slot(node, context_results)
    task = render_template(
        node.prompt_text,
        {"input": _stringify_input(params), "context": context_value, **_extra_param_slots(params)},
    )
    payload: dict[str, Any] = {"pipeline": manifest.id, "stage": node.id}
    if params:
        import json  # noqa: PLC0415

        payload["params"] = json.loads(json.dumps(dict(params), default=str))

    try:
        call = await agent_service.request_call(
            client, conn,
            requested_by=requested_by, endpoint=endpoint, task=task, payload=payload,
            context_query=context_query, target_graph=node.target_graph or None,
            via_pipeline=manifest.id, via_stage=node.id,
        )
    except (agent_service.CallInvalid, TriggerDenied) as exc:
        reason = exc.reason if isinstance(exc, TriggerDenied) else str(exc)
        raise PipelineError(f"stage {node.id!r} could not place its call: {reason}") from exc

    record.status = "Deferred"
    record.result = call.id
    record.detail = f"agent call {call.id} to {endpoint.id!r} awaits approval"


async def _run_context_stage(
    conn: Conn,
    client: FusekiClient,
    node: PipelineNode,
    rules,  # RuleLoadResult
    params: Mapping[str, Any] | None,
    record: StageRecord,
) -> str:
    """Read-only preview of a context-only stage's rule.

    Binds and CONSTRUCTs, the same dry-run shape ``triggers.evaluate_triggers``
    already uses for a review-required trigger firing -- nothing is written.
    The result is returned to the caller for ``context_results``, not stored
    on ``record`` beyond ``record.detail``'s byte count, since it is
    internal working state for this run, not part of the stage's own
    public-facing outcome the way an ``llm`` stage's rendered prompt is.
    """
    if not node.rule:
        raise PipelineError(
            f"context-only stage {node.id!r} declares no rule; nothing to read"
        )
    rule = rules.by_id(node.rule)
    if rule is None:
        raise PipelineError(f"stage {node.id!r} names unknown rule {node.rule!r}")

    try:
        sparql = bind_rule(rule, dict(params or {}))
        turtle = await client.construct(conn, sparql)
    except (RuleError, ParameterError) as exc:
        raise PipelineError(str(exc)) from exc

    record.status = "Completed"
    record.detail = f"context read, {len(turtle)} character(s); not written to any graph"
    return turtle


async def _run_shacl_stage(
    conn: Conn, client: FusekiClient, node: PipelineNode, record: StageRecord
) -> None:
    target = node.target_graph
    if not target:
        raise PipelineError(f"validation stage {node.id!r} names no target graph")

    shapes = node.shapes or conn.shapes_graph
    turtle = await client.get_graph(conn, target)
    if not turtle.strip():
        record.status = "Completed"
        record.detail = f"<{target}> is empty; nothing to validate"
        return

    # Delta mode needs a payload distinct from the graph it merges into. Here
    # the graph *is* the payload, so full validation is the right call — the
    # question a validation stage asks is whether the stage output conforms,
    # not whether it made things worse.
    report = await validate_full(client, conn, turtle=turtle, shapes_graph=shapes)

    if report.conforms:
        record.status = "Completed"
        record.detail = f"conforms against <{shapes}>"
        return

    record.status = "Failed"
    record.detail = f"{len(report.results)} violation(s) against <{shapes}>"
    raise PipelineError(record.detail)


async def _run_projection_stage(
    conn: Conn, client: FusekiClient, node: PipelineNode, record: StageRecord
) -> None:
    """Hand this stage's output to an external target through a hook.

    This is where a pipeline stops pretending it can do everything. The bridge
    computes the slice and the difference; the target does the transforming.
    """
    from .projection import ProjectionError, ProjectionRunner, ProjectionStore

    hooks = await ProjectionStore(client).hooks(conn)
    hook = next((h for h in hooks if h.id == node.projection), None)
    if hook is None:
        raise PipelineError(
            f"stage {node.id!r} names unknown projection hook {node.projection!r}"
        )

    try:
        delivery, envelope = await ProjectionRunner(client).run(conn, hook)
    except ProjectionError as exc:
        raise PipelineError(str(exc)) from exc

    record.status = "Completed" if delivery.status != "failed" else "Failed"
    record.triples_written = envelope.addition_count
    record.detail = (
        f"{delivery.status} to {hook.target}: "
        f"+{envelope.addition_count} / -{envelope.retraction_count}"
    )
    if delivery.status == "failed":
        raise PipelineError(record.detail)
