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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from .conn import Conn
from .fuseki import FusekiClient, FusekiError
from .messages import Message, StageRecord
from .named_rules import RuleError, execute_named_rule, load_named_rules
from .rdfutil import collect, local_name, pick, pick_all
from .shacl import validate_full

log = logging.getLogger("holonbridge.pipeline")

BUILD = "https://w3id.org/databook/ns#"

NODE_KINDS = ("Target", "Stage", "Source")

#: Transformers the bridge executes. Everything else is recorded as deferred.
EXECUTABLE = {"sparql", "rule", "shacl", "validate", "projection", "external"}
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
}
_DEPENDS = ("dependsOn",)


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
            target_graph=pick(props, _NODE_FIELDS["target_graph"]) or "",
            depends_on=pick_all(props, _DEPENDS),
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
) -> Message:
    """Run every stage in dependency order, recording each on the message.

    ``conn`` comes first and is a frozen value, so it is safe to hold across
    the await points of a background task — unlike a request object, which is
    not valid once the response has gone out.
    """
    order = topological_order(manifest)
    rules = await load_named_rules(client, conn)

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

        if transformer and transformer not in EXECUTABLE:
            record.status = "Deferred"
            record.detail = (
                f"{transformer!r} stages are not executed by the bridge"
                if transformer in DEFERRED
                else f"unknown transformer {transformer!r}"
            )
            continue

        try:
            if node.projection or transformer in {"projection", "external"}:
                await _run_projection_stage(conn, client, node, record)
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
