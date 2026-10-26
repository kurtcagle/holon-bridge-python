"""SHACL validation, including delta mode.

Naive validate-then-push makes every write answerable for the whole target
graph: a single pre-existing violation blocks every subsequent write, which
is why the gate stays disarmed in practice.

Delta mode fixes that. It validates the target graph as it stands, validates
the target merged with the payload, and reports only the results the payload
introduced. The merge happens server-side in a scratch graph via SPARQL
``COPY``/GSP ``POST`` — no Turtle round trip through the bridge.

Jena is the validator by default so RDF 1.2 payloads are handled correctly.
``pyshacl`` is supported as an offline fallback for Turtle 1.1 only.

CHANGED 2026-09-30: context-aware validation. Everything above validates the
target graph *alone*, which is wrong for a shape that needs the ontology
visible -- a SPARQL constraint over a property *classified* in the ontology
graph, or a ``sh:class`` check against a class declared there. Validated on
its own, the first is vacuous (no focus node exists) and the second reports a
false violation for every correct node. ``validate_full`` and
``validate_delta`` now take ``context_graphs``: read-only graphs ``ADD``-ed to
the scratch copy before validating. ``validate_context_pack`` runs the
dataset's second shapes graph (``urn:{dataset}:shacl-context``) that way, and
``combine_reports`` folds its result into the ordinary one. No ``GRAPH``
keyword is involved, so the graph-crossing guard below is unaffected; a
context pack is ordinary flat SPARQL evaluated over a merged graph.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF

from .conn import Conn
from .fuseki import FusekiClient, FusekiError
from .params import is_iri_datatype

SH = Namespace("http://www.w3.org/ns/shacl#")

_RESULT_FIELDS = (
    SH.focusNode,
    SH.resultPath,
    SH.sourceShape,
    SH.sourceConstraintComponent,
    SH.value,
    SH.resultSeverity,
    SH.resultMessage,
)


#: SHACL's default severity when a shape declares none (SHACL spec 2.1.2).
SH_VIOLATION = "http://www.w3.org/ns/shacl#Violation"


def is_blocking(result: dict[str, str]) -> bool:
    """Whether a validation result should stop a write.

    Only ``sh:Violation`` does. ``sh:Warning`` and ``sh:Info`` are reported
    and then got out of the way of — that distinction is the whole reason a
    shape author reaches for them. Treating every result as blocking makes a
    deliberate ``sh:Warning`` indistinguishable from a ``sh:Violation``, which
    silently discards the shape author's intent: a constraint marked Warning
    *because* it cannot be fully checked at write time (a range check needing
    another graph, say) would reject writes it was never meant to reject.

    An absent severity means Violation, per spec.
    """
    return result.get("resultSeverity", SH_VIOLATION) == SH_VIOLATION


@dataclass
class ValidationReport:
    """Normalised view of a ``sh:ValidationReport``."""

    conforms: bool
    results: list[dict[str, str]] = field(default_factory=list)
    turtle: str = ""
    mode: str = "full"

    @property
    def blocking(self) -> list[dict[str, str]]:
        return [r for r in self.results if is_blocking(r)]

    @property
    def advisory(self) -> list[dict[str, str]]:
        """Warnings and info — reported, never a reason to refuse a write."""
        return [r for r in self.results if not is_blocking(r)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "conforms": self.conforms,
            "mode": self.mode,
            "violations": len(self.blocking),
            "warnings": len(self.advisory),
            "results": self.results,
            "report": self.turtle,
        }


def parse_report(turtle: str, *, mode: str = "full") -> ValidationReport:
    """Turn a SHACL report in Turtle into a :class:`ValidationReport`."""
    graph = Graph()
    graph.parse(data=turtle, format="turtle")

    conforms = True
    for _, _, value in graph.triples((None, SH.conforms, None)):
        conforms = bool(value.toPython())

    results: list[dict[str, str]] = []
    for node in graph.subjects(RDF.type, SH.ValidationResult):
        entry: dict[str, str] = {}
        for predicate in _RESULT_FIELDS:
            value = graph.value(node, predicate)
            if value is not None:
                key = str(predicate).rsplit("#", 1)[-1]
                entry[key] = str(value)
        results.append(entry)

    blocking = [r for r in results if is_blocking(r)]
    return ValidationReport(
        conforms=conforms and not blocking, results=results, turtle=turtle, mode=mode
    )


def _fingerprint(result: dict[str, str]) -> tuple[str, ...]:
    """Stable identity for a validation result, used to diff two reports."""
    return tuple(
        result.get(str(field).rsplit("#", 1)[-1], "") for field in _RESULT_FIELDS
    )


# --- graph-crossing SPARQLConstraint detection --------------------------------
#
# Jena's ``/{ds}/shacl?graph=<target>`` service (and, for the identical
# underlying reason, ``pyshacl`` run over a plain ``rdflib.Graph`` in
# ``validate_local`` below) validates a single flat graph, never a full
# dataset. A SPARQLConstraint whose ``sh:select``/``sh:ask`` body contains a
# ``GRAPH ?g { ... }`` block has no named graphs to iterate over in that
# context: the pattern matches zero rows and the constraint silently reports
# conformance instead of firing. Confirmed against raw Fuseki 6.1.0 by Ben
# Wortley, 2026-09-08 — not a bridge bug, and the same class of failure as
# the Level1CapabilityNotationConstraint issue on the Node reference
# implementation. See https://github.com/kurtcagle/holon-bridge-python/issues/2.
#
# This is a detection gate, not a fix: neither validator this module calls
# can actually enforce a graph-crossing constraint today. Failing loud here
# is a deliberate stopgap until graph-aware constraints can be routed to a
# validator that sees across named graphs (tracked as a follow-on; see the
# issue above for the fuller hybrid-architecture discussion).
#
# Correction, 2026-10-25: pyshacl 0.40.1 over a plain ``rdflib.Graph`` does not
# silently conform -- it raises ("requires a dataset"). For ``validate_local``
# this guard is therefore a clearer error; it is Jena's service that silently
# conforms. (pyshacl over an ``rdflib.Dataset`` does enforce such a constraint.)
#
# A regex scan for the ``GRAPH`` keyword is a heuristic, not a SPARQL parse — it can in
# principle mis-flag a body where "graph" only appears in a comment or
# string literal, but a false-positive rejection prompting a look is a far
# safer failure than the silent no-op it replaces.

_GRAPH_KEYWORD_RE = re.compile(r"\bGRAPH\b", re.IGNORECASE)
_SPARQL_CONSTRAINT_PREDICATES = (SH.select, SH.ask)


def _graph_crossing_hits(shapes_graph: Graph) -> list[tuple[str, str, list[str]]]:
    """(constraint node, sh:select-or-sh:ask, owning shape IRIs) for every
    SPARQL body that references GRAPH — the pattern neither validator below
    can enforce. ``sh:sparql`` constraints are usually blank nodes, so the
    owning shape (found via ``?shape sh:sparql <constraint>``) is what a
    shape author can actually search their Turtle for; it may be empty if
    the constraint is unreferenced or reached some other way.
    """
    hits: list[tuple[str, str, list[str]]] = []
    for predicate in _SPARQL_CONSTRAINT_PREDICATES:
        for constraint, _, value in shapes_graph.triples((None, predicate, None)):
            if _GRAPH_KEYWORD_RE.search(str(value)):
                owners = [str(s) for s in shapes_graph.subjects(SH.sparql, constraint)]
                hits.append((str(constraint), str(predicate).rsplit("#", 1)[-1], owners))
    return hits


def _describe_graph_crossing_hit(constraint: str, prop: str, owners: list[str]) -> str:
    label = ", ".join(owners) if owners else f"<{constraint}>"
    return f"{label} (sh:{prop})"


def _raise_if_graph_crossing(hits: list[tuple[str, str, list[str]]]) -> None:
    if not hits:
        return
    described = "; ".join(_describe_graph_crossing_hit(*hit) for hit in hits)
    raise ValueError(
        "shapes graph declares a SPARQLConstraint that references GRAPH "
        "inside its SPARQL body -- this bridge cannot enforce a "
        "graph-crossing constraint today (both Jena's dataset-scoped SHACL "
        "service and pyshacl's local fallback validate a single flat graph "
        f"with no named graphs to iterate over): {described}. See "
        "https://github.com/kurtcagle/holon-bridge-python/issues/2."
    )


def ensure_enforceable(shapes_turtle: str) -> None:
    """Public name for the graph-crossing guard, for gates outside this module
    (the fluent pre-commit check validates through the client directly, so it
    must call this itself). Raises ``ValueError`` naming the offending shape."""
    _reject_graph_crossing_constraints(shapes_turtle)


def _reject_graph_crossing_constraints(shapes_turtle: str) -> None:
    """Raise ``ValueError`` if ``shapes_turtle`` declares a GRAPH-crossing
    SPARQLConstraint, rather than let it silently pass every validation.
    """
    graph = Graph()
    graph.parse(data=shapes_turtle, format="turtle")
    _raise_if_graph_crossing(_graph_crossing_hits(graph))


async def validate_full(
    client: FusekiClient,
    conn: Conn,
    *,
    turtle: str,
    shapes_graph: str,
    target_graph: str | None = None,
    write_mode: str = "merge",
    reduction_rule_id: str | None = None,
    context_graphs: Sequence[str] = (),
) -> ValidationReport:
    """Validate a payload in isolation, or combined with a target graph.

    This is the pre-flight validators call directly. It does not gate a write.

    ``write_mode`` decides what "combined" means, and getting it wrong is not
    cosmetic: under ``replace`` the post-write state is the payload *alone*,
    so validating the union of target and payload validates a state that will
    never exist.

    ``reduction_rule_id``, if given, names a registered named rule to run
    against the scratch graph — in place, replacing its content — before
    validation. See :func:`_apply_reduction` for what that rule's CONSTRUCT
    needs to look like. This is mechanism only: the bridge does not know or
    care what "reduction" means for any particular temporal model, only that
    a rule can be designated to compute it.

    ``context_graphs`` are read-only graphs added to the scratch copy after
    the payload (and after any reduction) and before validating, so a shape
    that needs them visible -- the ontology, typically -- can see them. They
    are copied, never modified. Each IRI is checked before any Fuseki call.
    """
    shapes = await client.get_graph(conn, shapes_graph)
    if not shapes.strip():
        raise ValueError(f"shapes graph <{shapes_graph}> is empty or absent")
    _reject_graph_crossing_constraints(shapes)
    context_graphs = _checked_context_graphs(context_graphs)

    scratch = _scratch_iri(conn)
    try:
        # Under replace the payload IS the result, so carrying the target in
        # would validate triples the write is about to delete.
        if target_graph and write_mode != "replace":
            await client.update(conn, f"COPY SILENT GRAPH <{target_graph}> TO <{scratch}>")
        else:
            await client.drop_graph(conn, scratch)
        await client.post_graph(conn, scratch, turtle)
        if reduction_rule_id:
            await _apply_reduction(client, conn, reduction_rule_id, scratch)
        # Context goes in AFTER reduction: a reduction rule replaces the
        # scratch graph's content with its CONSTRUCT result, so anything added
        # before it would be discarded, or reduced as if it were candidate state.
        await _add_context(client, conn, scratch, context_graphs)
        report = await client.shacl_validate(
            conn, target_graph=scratch, shapes_turtle=shapes
        )
    finally:
        await client.drop_graph(conn, scratch)

    return parse_report(report, mode="full")


async def validate_delta(
    client: FusekiClient,
    conn: Conn,
    *,
    turtle: str,
    shapes_graph: str,
    target_graph: str,
    write_mode: str = "merge",
    reduction_rule_id: str | None = None,
    context_graphs: Sequence[str] = (),
) -> ValidationReport:
    """Report only the violations this payload introduces.

    ``write_mode`` is load-bearing. Under ``merge`` the post-write state is
    target plus payload; under ``replace`` it is the payload alone, and the
    difference is not academic. Validating the union for a replace hides
    every violation caused by *removal* -- a required property deleted by the
    replace is still sitting in the union satisfying its own minCount. The
    gate then reports conformance and writes a graph that violates its
    shapes, which is a worse failure than refusing a good write.

    ``reduction_rule_id`` reduces the *merged* candidate state before it is
    validated -- see :func:`_apply_reduction`. Known limitation: the baseline
    (the target as it stands today) is validated unreduced. Reducing it too
    would mean copying the live target into a second scratch graph rather
    than validating it in place, which is a real increment on its own and is
    deliberately not folded into this one -- a half-solved version of "reduce
    the baseline too" would be worse than clearly not doing it yet.

    ``context_graphs`` are merged into BOTH sides of the comparison. That is
    not optional: the live target does not have the context merged into it,
    so validating the baseline in place while validating a context-merged
    candidate would report every violation the context itself carries (an
    ontology node failing a shape, say) as introduced by the payload. With
    context the baseline is therefore built in its own scratch graph, exactly
    as the merged state is. Without context the baseline is validated in
    place, as before.
    """
    shapes = await client.get_graph(conn, shapes_graph)
    if not shapes.strip():
        raise ValueError(f"shapes graph <{shapes_graph}> is empty or absent")
    _reject_graph_crossing_constraints(shapes)
    context_graphs = _checked_context_graphs(context_graphs)

    scratch = _scratch_iri(conn)
    baseline_scratch = _scratch_iri(conn) if context_graphs else None
    try:
        # Baseline: the target exactly as it stands, validated in place. A
        # target that does not exist yet has no baseline -- and asking Jena
        # to validate a graph it has never seen is a 404, not an empty
        # report, so the check has to happen before the call rather than
        # around it.
        if baseline_scratch is not None:
            # With context, the baseline is built like the merged state below.
            if await _graph_has_content(client, conn, target_graph):
                await client.update(
                    conn, f"COPY SILENT GRAPH <{target_graph}> TO <{baseline_scratch}>"
                )
            await _add_context(client, conn, baseline_scratch, context_graphs)
            if await _graph_has_content(client, conn, baseline_scratch):
                baseline = parse_report(
                    await client.shacl_validate(
                        conn, target_graph=baseline_scratch, shapes_turtle=shapes
                    )
                )
            else:
                baseline = ValidationReport(conforms=True, mode="delta")
        elif await _graph_has_content(client, conn, target_graph):
            baseline = parse_report(
                await client.shacl_validate(
                    conn, target_graph=target_graph, shapes_turtle=shapes
                )
            )
        else:
            baseline = ValidationReport(conforms=True, mode="delta")

        # Merged: the state that will actually exist once the write lands.
        # For replace that is the payload by itself -- no COPY.
        if write_mode != "replace":
            await client.update(
                conn, f"COPY SILENT GRAPH <{target_graph}> TO <{scratch}>"
            )
        await client.post_graph(conn, scratch, turtle)
        if reduction_rule_id:
            await _apply_reduction(client, conn, reduction_rule_id, scratch)
        await _add_context(client, conn, scratch, context_graphs)
        merged_turtle = await client.shacl_validate(
            conn, target_graph=scratch, shapes_turtle=shapes
        )
        merged = parse_report(merged_turtle)
    finally:
        await client.drop_graph(conn, scratch)
        if baseline_scratch is not None:
            await client.drop_graph(conn, baseline_scratch)

    known = {_fingerprint(r) for r in baseline.results}
    introduced = [r for r in merged.results if _fingerprint(r) not in known]

    return ValidationReport(
        # Only newly introduced *blocking* results refuse the write. A
        # newly introduced warning is still worth reporting — it goes in
        # results either way — but it is not grounds for a 422.
        conforms=not any(is_blocking(r) for r in introduced),
        results=introduced,
        turtle=merged_turtle,
        mode="delta",
    )


# --- context-aware validation --------------------------------------------------
#
# A dataset may keep a second shapes graph, ``urn:{dataset}:shacl-context``,
# for shapes that only make sense with the ontology visible alongside the
# data. The ordinary shapes graph is validated against the target alone (so a
# shape there is correct on the target graph by itself); this one is
# validated against the target merged with ``conn.validation_context_graphs``.
# The two are reported together. A dataset with no content in the context
# shapes graph behaves exactly as it did before this existed.
#
# Context graph IRIs are interpolated into SPARQL, so they are checked
# before any Fuseki call. They come from ``Conn`` (derived from the dataset),
# never from a request: merging an arbitrary caller-named graph into a
# scratch copy and returning the validation results would be a way to read a
# graph the caller may not be entitled to.

_GRAPH_IRI_UNSAFE = frozenset('<>"{}|^`\\')


def _checked_context_graphs(context_graphs: Sequence[str]) -> tuple[str, ...]:
    """Validate every context graph IRI up front, before any scratch work."""
    if isinstance(context_graphs, str):
        raise TypeError("context_graphs must be a sequence of IRIs, not a single string")
    checked: list[str] = []
    for graph in context_graphs:
        if not graph or any(
            ch in _GRAPH_IRI_UNSAFE or ch.isspace() or ord(ch) < 0x20 for ch in graph
        ):
            raise ValueError(f"{graph!r} cannot be used as a validation context graph IRI")
        checked.append(graph)
    return tuple(checked)


async def _add_context(
    client: FusekiClient, conn: Conn, scratch: str, context_graphs: Sequence[str]
) -> None:
    """Copy each context graph's triples into ``scratch``. Read-only with
    respect to the context graphs; ``SILENT`` so an absent one is a no-op."""
    for graph in context_graphs:
        await client.update(conn, f"ADD SILENT GRAPH <{graph}> TO <{scratch}>")


async def validate_context_pack(
    client: FusekiClient,
    conn: Conn,
    *,
    turtle: str,
    target_graph: str | None,
    delta: bool,
    write_mode: str = "merge",
    reduction_rule_id: str | None = None,
) -> ValidationReport | None:
    """Validate against the dataset's context shapes graph, with the dataset's
    context graphs merged in. Returns ``None`` when the dataset has no context
    shapes -- the graph is absent or empty -- so callers can treat "no pack" as
    "nothing to add" and carry on.

    ``delta=True`` needs a ``target_graph`` and reports only what the payload
    introduces, like :func:`validate_delta`; otherwise the merged state is
    validated in full, like :func:`validate_full`.
    """
    if not await _graph_has_content(client, conn, conn.context_shapes_graph):
        return None
    common: dict[str, Any] = {
        "turtle": turtle,
        "shapes_graph": conn.context_shapes_graph,
        "write_mode": write_mode,
        "reduction_rule_id": reduction_rule_id,
        "context_graphs": conn.validation_context_graphs,
    }
    if delta:
        if target_graph is None:
            raise ValueError("delta validation of the context pack requires a target_graph")
        return await validate_delta(client, conn, target_graph=target_graph, **common)
    return await validate_full(client, conn, target_graph=target_graph, **common)


def combine_reports(
    local: ValidationReport,
    context: ValidationReport | None,
    *,
    context_shapes_graph: str | None = None,
    context_graphs: Sequence[str] = (),
) -> dict[str, Any]:
    """Fold the context pack's report into the ordinary one.

    With no context report this is exactly ``local.as_dict()``. With one, the
    top-level ``conforms``, ``violations``, ``warnings`` and ``results`` cover
    both packs (each result tagged with its ``pack``), ``report`` stays the
    ordinary pack's Turtle, and ``packs`` carries the per-pack breakdown,
    including the context pack's own Turtle report. The two Turtle reports are
    kept apart on purpose: concatenating them would merge their blank nodes.
    """
    payload = local.as_dict()
    if context is None:
        return payload
    payload["conforms"] = local.conforms and context.conforms
    payload["violations"] = len(local.blocking) + len(context.blocking)
    payload["warnings"] = len(local.advisory) + len(context.advisory)
    payload["results"] = [{**r, "pack": "local"} for r in local.results] + [
        {**r, "pack": "context"} for r in context.results
    ]
    payload["packs"] = {
        "local": {
            "conforms": local.conforms,
            "violations": len(local.blocking),
            "warnings": len(local.advisory),
        },
        "context": {
            "shapes_graph": context_shapes_graph,
            "context_graphs": list(context_graphs),
            "conforms": context.conforms,
            "violations": len(context.blocking),
            "warnings": len(context.advisory),
            "report": context.turtle,
        },
    }
    return payload


async def _apply_reduction(
    client: FusekiClient, conn: Conn, rule_id: str, scratch: str
) -> None:
    """Reduce ``scratch`` in place by running a designated named rule over it.

    Mechanism only, deliberately ignorant of any specific temporal model. A
    reduction rule is an ordinary :class:`~holonbridge.named_rules.NamedRule`
    whose CONSTRUCT reads ``{{scope_graph}}`` rather than a literal graph
    IRI -- that placeholder is bound to ``scratch`` here, so the rule reads
    the candidate state this validation is about to check and writes back
    into that same graph, replacing it. What the rule's body actually does
    with what it reads -- filtering to current assertions under whatever
    "superseded" turns out to mean -- is entirely the model's business; the
    bridge only knows there is a rule to call.

    An unknown ``rule_id`` or a rule whose body does not use ``{{scope_graph}}``
    is refused rather than silently skipped: a validation gate that quietly
    stopped reducing would look identical to one that never was, and that is
    exactly the shape of failure this codebase has spent the most effort
    closing off today.
    """
    from .named_rules import (  # noqa: PLC0415 - avoid a hard import at module load
        ParameterError,
        RuleError,
        execute_named_rule,
        load_named_rules,
    )

    result = await load_named_rules(client, conn)
    rule = result.by_id(rule_id)
    if rule is None:
        raise ValueError(
            f"reduction_rule_id {rule_id!r} is not a registered named rule "
            f"(dataset {conn.dataset!r})"
        )
    if "{{scope_graph}}" not in rule.construct:
        raise ValueError(
            f"rule {rule_id!r} cannot be used as a reduction rule: its CONSTRUCT "
            "does not reference {{scope_graph}}, so it would read whatever graph "
            "it was originally written against rather than the candidate state "
            "actually being validated"
        )
    scope_param = rule.declared.get("scope_graph")
    if scope_param is None or not is_iri_datatype(scope_param.datatype):
        raise ValueError(
            f"rule {rule_id!r} must declare a parameter named 'scope_graph' with "
            "an IRI datatype (xsd:anyURI, or IRI/iri/uri/URI/Resource). Without "
            "it, substitute_params renders {{scope_graph}} as a quoted string "
            "literal rather than <a graph reference>, which is a SPARQL syntax "
            "error inside a GRAPH clause -- not a validation failure, a rule "
            "authoring mistake that would otherwise surface as a confusing "
            "parse error from Jena instead of a clear message here."
        )

    try:
        await execute_named_rule(
            conn,
            client,
            rule,
            params={"scope_graph": scratch},
            write_mode="Replace",
            target_graph=scratch,
        )
    except (RuleError, ParameterError) as exc:
        raise ValueError(f"reduction rule {rule_id!r} failed: {exc}") from exc


async def _graph_has_content(
    client: FusekiClient, conn: Conn, graph_iri: str
) -> bool:
    """Whether a named graph exists with at least one triple.

    ASK rather than a triple count: the only question here is whether there
    is anything to validate, and ASK stops at the first match.
    """
    result = await client.select(conn, f"ASK {{ GRAPH <{graph_iri}> {{ ?s ?p ?o }} }}")
    return bool(result.get("boolean", False))


def _scratch_iri(conn: Conn) -> str:
    return f"urn:{conn.dataset}:scratch:{uuid.uuid4().hex}"


# --- optional offline path ----------------------------------------------------


def validate_local(turtle: str, shapes_turtle: str) -> ValidationReport:
    """Validate with ``pyshacl``, for Turtle 1.1 payloads with no backend.

    Kept deliberately separate: it will reject valid RDF 1.2 and it does not
    see anything already in the store, so it can only ever be a convenience.
    """
    try:
        from pyshacl import validate as pyshacl_validate  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "local validation needs pyshacl: pip install pyshacl"
        ) from exc

    data = Graph().parse(data=turtle, format="turtle")
    shapes = Graph().parse(data=shapes_turtle, format="turtle")
    _raise_if_graph_crossing(_graph_crossing_hits(shapes))
    conforms, report_graph, _ = pyshacl_validate(
        data, shacl_graph=shapes, inference="none", advanced=True
    )
    report = parse_report(report_graph.serialize(format="turtle"), mode="local")
    # ``not report.blocking``, not ``not report.results`` — the offline path
    # must agree with the backed one about what a warning means, or the same
    # payload conforms against Jena and fails against pyshacl.
    report.conforms = conforms and not report.blocking
    return report
