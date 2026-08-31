"""Named rules — stored CONSTRUCTs that materialise derived triples.

A rule runs a CONSTRUCT and writes the result into a target graph under one
of four write modes:

``Append``
    Add the derived triples to whatever is already there. Nothing is removed,
    so a rule that no longer derives a triple leaves it behind.

``Replace``
    The target graph becomes exactly the rule's output. Anything else in that
    graph is destroyed, so a target shared with another writer is the wrong
    target for this mode.

``Sync``
    Reconcile: insert what is newly derived, remove what the rule used to
    derive and no longer does, leave everything else alone. This is the mode
    that makes a rule safely re-runnable.

``Supersede``
    For bitemporal, supersession-encoded data, where a destructive write
    would erase the transaction-time record the whole encoding exists to
    preserve. Nothing is ever deleted. A new value for a subject/predicate
    pair is inserted as a fresh assertion, and the value it replaces is
    annotated as superseded via an RDF 1.2 triple term — never removed.
    Verified live (2026-07-29) against a real Jena 6 instance: the
    ``<<( s p o )>>`` annotation syntax parses, and the read-guard-write-
    reread cycle correctly leaves both values present with only the old one
    tagged. Two real bugs were caught and fixed in that same pass — see
    :func:`_apply_supersede` — neither of which the stub test suite could
    have found, since it does not parse SPARQL at all.

**Nothing is parsed in-process.** The CONSTRUCT result is fetched as Turtle
and pushed straight into a scratch graph — bytes out of Jena and back into
Jena, never through rdflib — and every write mode is then a server-side
SPARQL graph operation over that scratch graph. Turtle 1.2 output survives
intact, and triple counts come from `COUNT(*)` rather than from counting
lines.

Marked non-canonical in the Node bridge pending WG IV alignment; the same
caveat applies here.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .conn import Conn
from .fuseki import FusekiClient, FusekiError
from .rdfutil import collect, local_name, pick
from .params import Parameter, ParameterError, render_term, substitute_params
from .sparql_kind import accepts_values_clause, form
from .turtle import escape_literal

log = logging.getLogger("holonbridge.named_rules")

RULE_CLASS_SUFFIX = "NamedRule"

WRITE_MODES = ("Append", "Replace", "Sync", "Supersede")
RULE_STATUSES = ("Active", "Suspended", "Deprecated")

_RULE_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "identifier"),
    "construct": ("construct", "sparql", "rule", "queryText", "query", "body"),
    "label": ("label", "title"),
    "description": ("description", "comment"),
    "target_graph": ("targetGraph", "target"),
    "write_mode": ("writeMode", "mode"),
    "status": ("ruleStatus", "status"),
    "order": ("order", "sequence", "priority"),
}
_PARAM_LINKS = ("parameter", "parameters", "hasParameter", "param")
_PARAM_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("name", "varName", "variable", "paramName"),
    "datatype": ("datatype", "dataType", "kind", "range"),
    "description": ("description", "comment"),
    "required": ("required", "isRequired"),
    "default": ("default", "defaultValue"),
}


class RuleError(RuntimeError):
    """A rule cannot be run as defined."""


class RuleSuspended(RuleError):
    """The rule exists but its status forbids execution."""


def _normalise(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    if not value:
        return default
    for candidate in allowed:
        if candidate.lower() == value.strip().lower():
            return candidate
    return default


@dataclass
class NamedRule:
    id: str
    iri: str
    construct: str
    target_graph: str
    write_mode: str = "Append"
    status: str = "Active"
    order: int | None = None
    label: str = ""
    description: str = ""
    params: list[Parameter] = field(default_factory=list)

    @property
    def declared(self) -> dict[str, Parameter]:
        return {p.name: p for p in self.params}

    @property
    def runnable(self) -> bool:
        return self.status == "Active"

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "label": self.label or self.id,
            "description": self.description,
            "targetGraph": self.target_graph,
            "writeMode": self.write_mode,
            "ruleStatus": self.status,
            "order": self.order,  # null means the rule states no opinion
            "parameters": [
                {
                    "name": p.name,
                    "datatype": p.datatype,
                    "required": p.required,
                    "description": p.description,
                    "default": p.default,
                }
                for p in self.params
            ],
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "construct": self.construct}


@dataclass
class RuleLoadResult:
    rules: list[NamedRule]
    warnings: list[str] = field(default_factory=list)

    def by_id(self, rule_id: str) -> NamedRule | None:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        return None


@dataclass
class RuleRun:
    rule_id: str
    target_graph: str
    write_mode: str
    triples_constructed: int
    triples_added: int
    triples_removed: int
    sparql: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "targetGraph": self.target_graph,
            "writeMode": self.write_mode,
            # CHANGED: was self.triples_constructed (the pre-copy scratch
            # count, identical regardless of write mode or what actually
            # landed) -- self.triples_added is what Append/Replace/Sync/
            # Supersede each measured after the write, and is the same value
            # this dict already reports as triplesAdded. Found while
            # independently verifying an external report (Ben Wortley, via
            # his own Claude instance) that flagged the identical mistake in
            # the scheduler's provenance record, which reads triples_written
            # from this same RuleRun.
            "triplesWritten": self.triples_added,
            "triplesAdded": self.triples_added,
            "triplesRemoved": self.triples_removed,
            "note": "non-canonical implementation — pending WG IV alignment",
        }


# --- loading ------------------------------------------------------------------


def _rules_query(graph: str) -> str:
    return f"""SELECT ?r ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?r a ?type .
    FILTER( STRENDS(STR(?type), "{RULE_CLASS_SUFFIX}") )
    ?r ?p ?o .
  }}
}}"""


def _rule_params_query(graph: str) -> str:
    links = " || ".join(f'STRENDS(STR(?link), "{name}")' for name in _PARAM_LINKS)
    return f"""SELECT ?r ?param ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?r a ?type .
    FILTER( STRENDS(STR(?type), "{RULE_CLASS_SUFFIX}") )
    ?r ?link ?param .
    FILTER( {links} )
    ?param ?p ?o .
  }}
}}"""


async def load_named_rules(
    client: FusekiClient, conn: Conn, *, graph: str | None = None
) -> RuleLoadResult:
    """Load every rule from ``urn:{dataset}:named-rules``.

    Degrades the same way the query registry does: an unreachable registry
    yields an empty result with a warning rather than a failed request.
    """
    registry = graph or conn.graph("named-rules")
    warnings: list[str] = []

    try:
        rows = (await client.select(conn, _rules_query(registry)))["results"]["bindings"]
    except (FusekiError, KeyError) as exc:
        message = f"named-rule load from <{registry}> failed: {exc}"
        log.warning(message)
        return RuleLoadResult(rules=[], warnings=[message])

    grouped = collect(rows, "r")

    params_by_rule: dict[str, list[Parameter]] = {}
    try:
        param_rows = (await client.select(conn, _rule_params_query(registry)))["results"][
            "bindings"
        ]
        owner = {r["param"]["value"]: r["r"]["value"] for r in param_rows}
        for node, props in collect(param_rows, "param").items():
            name = pick(props, _PARAM_FIELDS["name"])
            if not name:
                continue
            params_by_rule.setdefault(owner[node], []).append(
                Parameter(
                    name=name,
                    datatype=pick(props, _PARAM_FIELDS["datatype"]),
                    description=pick(props, _PARAM_FIELDS["description"]) or "",
                    required=(pick(props, _PARAM_FIELDS["required"]) or "").lower()
                    in {"true", "1"},
                    default=pick(props, _PARAM_FIELDS["default"]),
                )
            )
    except (FusekiError, KeyError) as exc:
        message = f"rule parameter metadata load failed: {exc}; rules remain runnable unbound"
        log.warning(message)
        warnings.append(message)

    rules: list[NamedRule] = []
    for iri, props in grouped.items():
        construct = pick(props, _RULE_FIELDS["construct"])
        if not construct:
            warnings.append(f"<{iri}> has no CONSTRUCT body; skipped")
            continue

        target = pick(props, _RULE_FIELDS["target_graph"])
        if not target:
            # A rule with nowhere to write is a configuration error, not a
            # runtime one — surface it at load time rather than on first run.
            warnings.append(f"<{iri}> declares no target graph; skipped")
            continue

        rule_id = pick(props, _RULE_FIELDS["id"]) or local_name(iri)
        raw_order = pick(props, _RULE_FIELDS["order"])
        order: int | None
        if raw_order is None:
            order = None
        else:
            try:
                order = int(raw_order)
            except ValueError:
                order = None
                warnings.append(
                    f"{rule_id}: order {raw_order!r} is not an integer; the rule runs "
                    "after those that declare one"
                )

        rules.append(
            NamedRule(
                id=rule_id,
                iri=iri,
                construct=construct,
                target_graph=target,
                write_mode=_normalise(pick(props, _RULE_FIELDS["write_mode"]), WRITE_MODES, "Append"),
                status=_normalise(pick(props, _RULE_FIELDS["status"]), RULE_STATUSES, "Active"),
                order=order,
                label=pick(props, _RULE_FIELDS["label"]) or rule_id,
                description=pick(props, _RULE_FIELDS["description"]) or "",
                params=sorted(params_by_rule.get(iri, []), key=lambda p: p.name),
            )
        )

    # Rules that state an order run first, in it. Rules that state none — or
    # state one that will not parse — run afterwards, alphabetically. An
    # unreadable order must not let a rule preempt correctly configured ones.
    rules.sort(key=lambda r: (r.order is None, r.order if r.order is not None else 0, r.id))
    return RuleLoadResult(rules=rules, warnings=warnings)


# --- binding ------------------------------------------------------------------

_FOCUS_NAMES = ("$this", "this")


def bind_rule(rule: NamedRule, supplied: Mapping[str, Any] | None) -> str:
    """Substitute ``{{placeholders}}`` and bind ``$this`` if supplied.

    ``$this`` is bound with a trailing ``VALUES`` clause rather than pasted in
    textually. SPARQL's grammar admits a ``ValuesClause`` after a
    ``ConstructQuery``, so the binding cannot corrupt a well-formed rule, and
    a rule left unbound keeps its "run over every focus node" behaviour.
    """
    values = {k: v for k, v in (supplied or {}).items() if v is not None}

    focus = None
    for name in _FOCUS_NAMES:
        if name in values:
            focus = values.pop(name)
            break

    result = substitute_params(rule.construct, values, rule.declared)
    if result.missing:
        raise ParameterError(
            f"{rule.id} has unresolved placeholders: {', '.join(result.missing)}"
        )

    sparql = result.sparql
    if focus is not None:
        if not accepts_values_clause(sparql):
            raise ParameterError(
                f"{rule.id} cannot take a $this binding: it already ends in a VALUES clause"
            )
        term = render_term(focus, "xsd:anyURI")
        sparql = f"{sparql.rstrip()}\nVALUES $this {{ {term} }}"

    return sparql


# --- execution ----------------------------------------------------------------


async def execute_named_rule(
    conn: Conn,
    client: FusekiClient,
    rule: NamedRule,
    *,
    params: Mapping[str, Any] | None = None,
    write_mode: str | None = None,
    target_graph: str | None = None,
    timeout: float | None = 60.0,
) -> RuleRun:
    """Run a rule and apply its write mode. ``conn`` comes first, as in Node.

    ``target_graph`` overrides ``rule.target_graph`` for this run only — the
    rule's own registration is untouched. This is what lets a rule be run
    against a scratch graph rather than its normal target: the SHACL gate's
    reduction hook uses it to reduce a candidate write in place before
    validating, and the same override lets a caller materialise a rule's
    output somewhere inspectable without touching what it would normally
    write to.
    """

    if not rule.runnable:
        raise RuleSuspended(f"rule {rule.id!r} is {rule.status.lower()}")

    target = target_graph or rule.target_graph
    mode = _normalise(write_mode, WRITE_MODES, rule.write_mode)
    sparql = bind_rule(rule, params)

    if form(sparql) not in {"CONSTRUCT", "DESCRIBE"}:
        raise RuleError(
            f"rule {rule.id!r} body is a {form(sparql)}; a rule must CONSTRUCT the "
            "triples it wants materialised"
        )

    scratch = f"urn:{conn.dataset}:rule-scratch:{uuid.uuid4().hex}"

    try:
        turtle = await client.construct(conn, sparql, timeout=timeout)

        await client.update(conn, f"CREATE SILENT GRAPH <{scratch}>")
        if turtle.strip():
            await client.post_graph(conn, scratch, turtle)

        constructed = await _count(client, conn, f"GRAPH <{scratch}> {{ ?s ?p ?o }}")

        if mode == "Append":
            added = await _count(
                client,
                conn,
                f"GRAPH <{scratch}> {{ ?s ?p ?o }} "
                f"FILTER NOT EXISTS {{ GRAPH <{target}> {{ ?s ?p ?o }} }}",
            )
            await client.update(conn, f"ADD SILENT <{scratch}> TO <{target}>")
            removed = 0

        elif mode == "Replace":
            removed = await _count(client, conn, f"GRAPH <{target}> {{ ?s ?p ?o }}")
            log.info(
                "REPLACE-TRACE scratch=%s turtle_bytes=%d constructed=%d "
                "scratch_now=%d target_before=%d",
                scratch,
                len(turtle or ""),
                constructed,
                await _count(client, conn, f"GRAPH <{scratch}> {{ ?s ?p ?o }}"),
                removed,
            )
            await client.update(conn, f"COPY SILENT <{scratch}> TO <{target}>")
            # Measured, not assumed. Append and Sync both count what they
            # actually changed; Replace echoed the scratch count, which is
            # how a lost triple went unnoticed -- the report said 3 while 2
            # arrived, and nothing in the response could contradict it.
            added = await _count(client, conn, f"GRAPH <{target}> {{ ?s ?p ?o }}")
            log.info(
                "REPLACE-TRACE after copy: target_now=%d (scratch reported %d)",
                added,
                constructed,
            )

        elif mode == "Sync":
            added = await _count(
                client,
                conn,
                f"GRAPH <{scratch}> {{ ?s ?p ?o }} "
                f"FILTER NOT EXISTS {{ GRAPH <{target}> {{ ?s ?p ?o }} }}",
            )
            removed = await _count(
                client,
                conn,
                f"GRAPH <{target}> {{ ?s ?p ?o }} "
                f"FILTER NOT EXISTS {{ GRAPH <{scratch}> {{ ?s ?p ?o }} }}",
            )
            await client.update(
                conn,
                f"""DELETE {{ GRAPH <{target}> {{ ?s ?p ?o }} }}
WHERE {{
  GRAPH <{target}> {{ ?s ?p ?o }}
  FILTER NOT EXISTS {{ GRAPH <{scratch}> {{ ?s ?p ?o }} }}
}}""",
            )
            await client.update(
                conn,
                f"""INSERT {{ GRAPH <{target}> {{ ?s ?p ?o }} }}
WHERE {{
  GRAPH <{scratch}> {{ ?s ?p ?o }}
  FILTER NOT EXISTS {{ GRAPH <{target}> {{ ?s ?p ?o }} }}
}}""",
            )

        else:  # Supersede
            added, removed = await _apply_supersede(client, conn, target, scratch)
    finally:
        await client.drop_graph(conn, scratch)

    return RuleRun(
        rule_id=rule.id,
        target_graph=target,
        write_mode=mode,
        triples_constructed=constructed,
        triples_added=added,
        triples_removed=removed,
        sparql=sparql,
    )


async def _apply_supersede(
    client: FusekiClient, conn: Conn, target: str, scratch: str
) -> tuple[int, int]:
    """Write the scratch graph's derived values into ``target`` without ever
    deleting anything.

    **Verified live, 2026-07-29, against a real Jena 6 instance.** Two real
    bugs were caught in that pass, neither visible to the stub test suite
    (which does not parse SPARQL):

    1. The annotation INSERT restated the full triple-term subject after a
       ``;`` continuation instead of just the next predicate — invalid
       Turtle regardless of triple terms. Fixed by continuing with the
       predicate alone.
    2. The whole UPDATE built two separate ``INSERT {...}`` blocks back to
       back. SPARQL Update's DELETE/INSERT operation admits exactly one
       INSERT template per modify clause; two in sequence is a parse error.
       Fixed by merging both into the single INSERT block.

    Confirmed correct after the fix: the ``<<( s p o )>>`` annotation syntax
    parses, both the old and new values remain present in the target graph
    (nothing deleted), and a query for the live value via
    ``FILTER NOT EXISTS { <<( s p ?o )>> hb:supersededBy ?next }`` correctly
    returns only the new one.

    Identity key is ``(subject, predicate)`` — the default assumption for a
    functional fluent, where at most one value is current at a time. A
    property that is legitimately multi-valued needs a different identity
    key than this function provides; it is not solved here, and a rule
    whose CONSTRUCT yields more than one new object for the same
    ``(subject, predicate)`` pair is refused outright, since there is no
    generic way to know which one this function should treat as *the*
    replacement.

    Mechanism, per touched ``(subject, predicate)`` pair, mirroring
    :func:`holonbridge.sequence.mint`'s compare-and-set exactly: read the
    live (not yet superseded) current value, guard the write on it still
    being live, write, then re-read to confirm — retrying on loss rather
    than trusting the UPDATE's own reported success, since a WHERE-guarded
    UPDATE that matches nothing still "succeeds" having done nothing. The
    retry loop's *retry* path specifically (an actual lost race, not just
    the single-attempt success path above) remains stub-tested only —
    simulating genuine concurrent contention against a live store needs a
    second writer, not exercised in this pass.

    Nothing is ever deleted. A newly superseded value is annotated, not
    removed, and stays in the graph as history. ``removed`` is therefore
    always ``0`` in the returned counts — the value is meaningful for
    Append/Sync, and reported as zero here for shape-compatibility with
    :class:`RuleRun` rather than because anything was actually removed.
    """
    pairs = await client.select(
        conn,
        f"""SELECT DISTINCT ?s ?p WHERE {{ GRAPH <{scratch}> {{ ?s ?p ?o }} }}""",
    )
    added = 0

    for row in pairs.get("results", {}).get("bindings", []):
        s, p = row["s"]["value"], row["p"]["value"]
        s_ref, p_ref = _term_ref(row["s"]), _term_ref(row["p"])

        new_values = await client.select(
            conn,
            f"""SELECT DISTINCT ?o WHERE {{
  GRAPH <{scratch}> {{ {s_ref} {p_ref} ?o }}
}}""",
        )
        new_bindings = new_values.get("results", {}).get("bindings", [])
        if len(new_bindings) != 1:
            raise RuleError(
                f"cannot Supersede <{s}> <{p}>: the rule's CONSTRUCT derives "
                f"{len(new_bindings)} distinct values for this subject/predicate "
                "pair, and Supersede's default (subject, predicate) identity key "
                "has no generic way to know which one replaces which -- this "
                "pair needs a rule authored for a finer-grained identity key, "
                "not the (subject, predicate) default"
            )
        new_o_ref = _term_ref(new_bindings[0]["o"])

        for _ in range(8):
            live = await client.select(
                conn,
                f"""SELECT ?o WHERE {{
  GRAPH <{target}> {{ {s_ref} {p_ref} ?o }}
  FILTER NOT EXISTS {{ <<( {s_ref} {p_ref} ?o )>> hb:supersededBy ?next }}
}}""",
            )
            live_bindings = live.get("results", {}).get("bindings", [])
            old_o_ref = _term_ref(live_bindings[0]["o"]) if live_bindings else None

            if old_o_ref is not None and _terms_equal(live_bindings[0]["o"], new_bindings[0]["o"]):
                break  # already current -- nothing to supersede, nothing to add

            if old_o_ref is not None:
                # A live value exists: guard re-verifies it is still exactly
                # what was just read -- still present, still not superseded
                # by anything else -- before superseding it.
                guard = f"""GRAPH <{target}> {{ {s_ref} {p_ref} {old_o_ref} }}
  FILTER NOT EXISTS {{ <<( {s_ref} {p_ref} {old_o_ref} )>> hb:supersededBy ?g }}"""
                # A single ";"-continuation on the SAME subject, not a second
                # statement restating it -- restating the subject after ";"
                # is a syntax error, confirmed live (2026-07-29) against a
                # real Jena 6 instance before this was corrected.
                supersede_old = f"""
  <<( {s_ref} {p_ref} {old_o_ref} )>> hb:supersededBy <<( {s_ref} {p_ref} {new_o_ref} )>> ;
    hb:supersededAt ?now ."""
            else:
                # No live value exists yet: guard re-verifies that is still
                # true -- nothing raced in and created one since the read
                # above -- rather than referencing a value that does not
                # exist.
                guard = f"""FILTER NOT EXISTS {{
    GRAPH <{target}> {{ {s_ref} {p_ref} ?anyO }}
    FILTER NOT EXISTS {{ <<( {s_ref} {p_ref} ?anyO )>> hb:supersededBy ?g2 }}
  }}"""
                supersede_old = ""

            await client.update(
                conn,
                # A single INSERT template, not two. SPARQL Update's
                # DELETE/INSERT operation admits exactly one INSERT block per
                # modify clause -- two back-to-back INSERT{...} INSERT{...}
                # blocks is a parse error, confirmed live (2026-07-29) in the
                # same session that found the ";"-continuation bug above.
                # Both were caught by running this exact template against a
                # real Jena 6 instance before trusting it, not by the stub
                # tests, which do not parse SPARQL at all.
                f"""PREFIX hb: <https://w3id.org/holonbridge/>
INSERT {{
  GRAPH <{target}> {{ {s_ref} {p_ref} {new_o_ref} }}{supersede_old}
}}
WHERE {{
  BIND(NOW() AS ?now)
  {guard}
}}""",
            )

            confirmed = await client.select(
                conn,
                f"""SELECT ?o WHERE {{
  GRAPH <{target}> {{ {s_ref} {p_ref} ?o }}
  FILTER NOT EXISTS {{ <<( {s_ref} {p_ref} ?o )>> hb:supersededBy ?next }}
}}""",
            )
            confirmed_bindings = confirmed.get("results", {}).get("bindings", [])
            if len(confirmed_bindings) == 1 and _terms_equal(
                confirmed_bindings[0]["o"], new_bindings[0]["o"]
            ):
                added += 1
                break
        else:
            raise RuleError(
                f"could not Supersede <{s}> <{p}> after 8 attempts "
                "(contention, or another writer touching the same pair)"
            )

    return added, 0


def _term_ref(binding: dict[str, str]) -> str:
    """Render a SPARQL JSON result binding back into a SPARQL term reference."""
    if binding["type"] == "uri":
        return f"<{binding['value']}>"
    if binding["type"] == "bnode":
        return f"_:{binding['value']}"
    datatype = binding.get("datatype")
    if datatype:
        return f'"{escape_literal(binding["value"])}"^^<{datatype}>'
    lang = binding.get("xml:lang")
    if lang:
        return f'"{escape_literal(binding["value"])}"@{lang}'
    return f'"{escape_literal(binding["value"])}"'


def _terms_equal(a: dict[str, str], b: dict[str, str]) -> bool:
    return (
        a.get("type") == b.get("type")
        and a.get("value") == b.get("value")
        and a.get("datatype") == b.get("datatype")
        and a.get("xml:lang") == b.get("xml:lang")
    )


async def _count(client: FusekiClient, conn: Conn, pattern: str) -> int:
    results = await client.select(
        conn, f"SELECT (COUNT(*) AS ?n) WHERE {{ {pattern} }}"
    )
    bindings = results.get("results", {}).get("bindings", [])
    if not bindings:
        return 0
    return int(bindings[0]["n"]["value"])
