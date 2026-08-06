"""Named-query registry loading and parameter binding.

The bridge understands two vocabularies that describe the same kind of thing,
and this module is the single place that difference is reconciled.

**Why there are two.** ``hb:`` (``https://w3id.org/holonbridge/``) is the
bridge's own original scheme. ``hquery:`` is the HGA Named Query
Specification. Both register a query, a body, and some parameters.

**Why the difference survives loading.** ``hb:`` bodies carry ``{{placeholder}}``
tokens and want string substitution. ``hquery:`` bodies carry ordinary SPARQL
variables and want them bound. Running an ``hquery:`` query through
``{{...}}`` substitution matches nothing, so the query executes
unparameterised and returns every row — a wrong answer with no error. Every
loaded query therefore carries its vocabulary, and binding dispatches on it.

**Why binding appends VALUES.** SPARQL's grammar places ``ValuesClause`` after
the entire query form, past ORDER BY and LIMIT. Appending therefore needs no
parsing of the body and cannot corrupt a well-formed query. Unsupplied
parameters stay unbound, which preserves the "omit for all" behaviour HGA
queries rely on.

**Class matching is by local name.** The loader finds anything typed
``*NamedQuery`` in the registry graph and derives the vocabulary from that
type's namespace, rather than hardcoding predicate IRIs for each scheme.
Properties are matched by local name too. This tolerates the two schemes
differing in spelling and survives a third being added. The trade is that a
different vocabulary also called ``NamedQuery`` would be picked up; see
:data:`QUERY_CLASS_SUFFIX` to tighten it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from .conn import Conn
from .fuseki import FusekiClient, FusekiError
from .rdfutil import collect, local_name, namespace, pick
from .params import (
    Parameter,
    ParameterError,
    placeholders,
    render_term,
    substitute_params,
    values_clause,
)
from .sparql_kind import accepts_values_clause, form

log = logging.getLogger("holonbridge.named_queries")

HB_NAMESPACE = "https://w3id.org/holonbridge/"

#: Local name that marks a registered query. Change to a full IRI match here
#: if the loose matching described in the module docstring becomes a problem.
QUERY_CLASS_SUFFIX = "NamedQuery"

# Property local names, in precedence order. First match wins.
_QUERY_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "identifier"),
    "sparql": ("sparql", "queryText", "queryString", "query", "body"),
    "label": ("label", "title"),
    "description": ("description", "comment"),
    "query_type": ("queryType", "form"),
    "target_graph": ("targetGraph", "defaultGraph", "graph"),
}
_PARAM_LINKS = ("parameter", "parameters", "hasParameter", "param")
_PARAM_FIELDS: dict[str, tuple[str, ...]] = {
    "name": ("name", "varName", "variable", "paramName"),
    "datatype": ("datatype", "dataType", "kind", "range"),
    "description": ("description", "comment"),
    "required": ("required", "isRequired"),
    "default": ("default", "defaultValue"),
}


@dataclass
class NamedQuery:
    """A query as loaded from the registry."""

    id: str
    iri: str
    sparql: str
    vocabulary: str  # "hb" or "hquery"
    label: str = ""
    description: str = ""
    query_type: str = "SELECT"
    target_graph: str | None = None
    params: list[Parameter] = field(default_factory=list)
    source: str = "rdf"

    @property
    def declared(self) -> dict[str, Parameter]:
        return {p.name: p for p in self.params}

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "label": self.label or self.id,
            "description": self.description,
            "queryType": self.query_type,
            "vocabulary": self.vocabulary,
            "targetGraph": self.target_graph,
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
        return {**self.summary(), "sparql": self.sparql}


@dataclass
class BoundQuery:
    sparql: str
    bound: list[str]
    missing: list[str]
    unused: list[str]
    strategy: str


@dataclass
class LoadResult:
    queries: list[NamedQuery]
    warnings: list[str] = field(default_factory=list)

    def by_id(self, query_id: str) -> NamedQuery | None:
        for query in self.queries:
            if query.id == query_id:
                return query
        return None


# --- loading ------------------------------------------------------------------


def _queries_query(graph: str) -> str:
    return f"""SELECT ?q ?type ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?q a ?type .
    FILTER( STRENDS(STR(?type), "{QUERY_CLASS_SUFFIX}") )
    ?q ?p ?o .
  }}
}}"""


def _params_query(graph: str) -> str:
    links = " || ".join(f'STRENDS(STR(?link), "{name}")' for name in _PARAM_LINKS)
    return f"""SELECT ?q ?param ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?q a ?type .
    FILTER( STRENDS(STR(?type), "{QUERY_CLASS_SUFFIX}") )
    ?q ?link ?param .
    FILTER( {links} )
    ?param ?p ?o .
  }}
}}"""


async def load_named_queries(
    client: FusekiClient, conn: Conn, *, graph: str | None = None
) -> LoadResult:
    """Load every registered query from ``urn:{dataset}:named-queries``.

    Degrades rather than failing: an unreachable registry yields an empty
    result with a warning, and a query whose parameter metadata will not load
    is still returned, because it remains runnable unbound.
    """
    registry = graph or conn.graph("named-queries")
    warnings: list[str] = []

    try:
        rows = (await client.select(conn, _queries_query(registry)))["results"]["bindings"]
    except (FusekiError, KeyError) as exc:
        message = f"named-query load from <{registry}> failed: {exc}"
        log.warning(message)
        return LoadResult(queries=[], warnings=[message])

    types: dict[str, str] = {}
    for row in rows:
        types.setdefault(row["q"]["value"], row["type"]["value"])
    grouped = collect(rows, "q")

    params_by_query: dict[str, list[Parameter]] = {}
    try:
        param_rows = (await client.select(conn, _params_query(registry)))["results"][
            "bindings"
        ]
        param_props = collect(param_rows, "param")
        owner = {r["param"]["value"]: r["q"]["value"] for r in param_rows}
        for node, props in param_props.items():
            name = pick(props, _PARAM_FIELDS["name"])
            if not name:
                continue
            required = (pick(props, _PARAM_FIELDS["required"]) or "").lower() in {
                "true",
                "1",
            }
            params_by_query.setdefault(owner[node], []).append(
                Parameter(
                    name=name,
                    datatype=pick(props, _PARAM_FIELDS["datatype"]),
                    description=pick(props, _PARAM_FIELDS["description"]) or "",
                    required=required,
                    default=pick(props, _PARAM_FIELDS["default"]),
                )
            )
    except (FusekiError, KeyError) as exc:
        message = f"parameter metadata load failed: {exc}; queries remain runnable unbound"
        log.warning(message)
        warnings.append(message)

    queries: list[NamedQuery] = []
    seen: dict[str, NamedQuery] = {}

    for iri, props in grouped.items():
        sparql = pick(props, _QUERY_FIELDS["sparql"])
        if not sparql:
            warnings.append(f"<{iri}> has no query body; skipped")
            continue

        type_iri = types.get(iri, "")
        vocabulary = "hb" if "holonbridge" in namespace(type_iri) else "hquery"

        # hquery: queries carry no explicit id — the IRI's local name is the id.
        query_id = pick(props, _QUERY_FIELDS["id"]) or local_name(iri)

        declared_type = (pick(props, _QUERY_FIELDS["query_type"]) or "").upper()
        query = NamedQuery(
            id=query_id,
            iri=iri,
            sparql=sparql,
            vocabulary=vocabulary,
            label=pick(props, _QUERY_FIELDS["label"]) or query_id,
            description=pick(props, _QUERY_FIELDS["description"]) or "",
            query_type=declared_type or form(sparql),
            target_graph=pick(props, _QUERY_FIELDS["target_graph"]),
            params=sorted(params_by_query.get(iri, []), key=lambda p: p.name),
        )

        if query_id in seen:
            other = seen[query_id]
            # Canonical hquery: wins; the shadowed definition is reported, not
            # dropped silently, because a duplicate id is a registry bug.
            keep, drop = (
                (query, other) if query.vocabulary == "hquery" else (other, query)
            )
            warnings.append(
                f"id {query_id!r} is registered twice: keeping the {keep.vocabulary}: "
                f"definition <{keep.iri}>, shadowing <{drop.iri}>"
            )
            queries = [q for q in queries if q.id != query_id]
            queries.append(keep)
            seen[query_id] = keep
            continue

        seen[query_id] = query
        queries.append(query)

    queries.sort(key=lambda q: q.id)
    return LoadResult(queries=queries, warnings=warnings)


# --- binding ------------------------------------------------------------------


def apply_query_params(
    query: NamedQuery, supplied: Mapping[str, Any] | None
) -> BoundQuery:
    """Bind caller-supplied parameters, dispatching on vocabulary."""
    values = {k: v for k, v in (supplied or {}).items() if v is not None}

    if query.vocabulary != "hquery":
        return _bind_placeholders(query, values)
    return _bind_values(query, values)


def _bind_placeholders(query: NamedQuery, supplied: Mapping[str, Any]) -> BoundQuery:
    declared = query.declared
    result = substitute_params(query.sparql, supplied, declared)
    known = set(placeholders(query.sparql)) | set(declared)
    unused = sorted(name for name in supplied if name not in known)
    return BoundQuery(
        sparql=result.sparql,
        bound=result.substituted,
        missing=result.missing,
        unused=unused,
        strategy="placeholder",
    )


def _bind_values(query: NamedQuery, supplied: Mapping[str, Any]) -> BoundQuery:
    declared = query.declared

    # An undeclared VALUES variable does not error — it quietly constrains
    # nothing. Reject the typo rather than running a query nobody meant.
    undeclared = sorted(name for name in supplied if name not in declared)
    if undeclared:
        raise ParameterError(
            f"{query.id} does not declare {', '.join(undeclared)}; "
            f"declared parameters are {', '.join(sorted(declared)) or '(none)'}"
        )

    missing = sorted(
        p.name
        for p in query.params
        if p.required and p.name not in supplied and p.default is None
    )

    pairs: list[tuple[str, str]] = []
    for name, param in sorted(declared.items()):
        if name in supplied:
            value: Any = supplied[name]
        elif param.default is not None:
            value = param.default
        else:
            continue  # unsupplied stays unbound: "omit for all"
        pairs.append((name, render_term(value, param.datatype)))

    clause = values_clause(pairs)
    sparql = query.sparql
    if clause:
        if not accepts_values_clause(sparql):
            raise ParameterError(
                f"{query.id} cannot take a VALUES clause: it is "
                f"{'an update' if form(sparql) == 'UPDATE' else 'already bound by a trailing VALUES clause'}"
            )
        sparql = f"{sparql.rstrip()}\n{clause}"

    return BoundQuery(
        sparql=sparql,
        bound=[name for name, _ in pairs],
        missing=missing,
        unused=[],
        strategy="values",
    )
