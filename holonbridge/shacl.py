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
"""

from __future__ import annotations

import uuid
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


async def validate_full(
    client: FusekiClient,
    conn: Conn,
    *,
    turtle: str,
    shapes_graph: str,
    target_graph: str | None = None,
    write_mode: str = "merge",
    reduction_rule_id: str | None = None,
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
    """
    shapes = await client.get_graph(conn, shapes_graph)
    if not shapes.strip():
        raise ValueError(f"shapes graph <{shapes_graph}> is empty or absent")

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
    """
    shapes = await client.get_graph(conn, shapes_graph)
    if not shapes.strip():
        raise ValueError(f"shapes graph <{shapes_graph}> is empty or absent")

    scratch = _scratch_iri(conn)
    try:
        # Baseline: the target exactly as it stands, validated in place. A
        # target that does not exist yet has no baseline -- and asking Jena
        # to validate a graph it has never seen is a 404, not an empty
        # report, so the check has to happen before the call rather than
        # around it.
        if await _graph_has_content(client, conn, target_graph):
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
        merged_turtle = await client.shacl_validate(
            conn, target_graph=scratch, shapes_turtle=shapes
        )
        merged = parse_report(merged_turtle)
    finally:
        await client.drop_graph(conn, scratch)

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
    conforms, report_graph, _ = pyshacl_validate(
        data, shacl_graph=shapes, inference="none", advanced=True
    )
    report = parse_report(report_graph.serialize(format="turtle"), mode="local")
    # ``not report.blocking``, not ``not report.results`` — the offline path
    # must agree with the backed one about what a warning means, or the same
    # payload conforms against Jena and fails against pyshacl.
    report.conforms = conforms and not report.blocking
    return report
