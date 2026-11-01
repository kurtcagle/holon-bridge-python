"""Which predicates did a write touch?

A state trigger may declare ``watchedPredicate``s, and the post-write hook uses
the answer here to skip triggers that cannot be affected by the write. That is
only an optimisation, so the rule for every function here is: **answer only
when sure, otherwise say ``None``**. ``None`` means "could not tell" and the
hook then evaluates every trigger. An answer that is too small would silently
miss a firing; an answer that is too large (or absent) only costs a query.

Both functions therefore return ``None`` for anything they cannot account for
completely: RDF 1.2 syntax rdflib cannot read, text that fails to parse, a
predicate that is a variable or a property path, and whole-graph operations
(CLEAR, DROP, LOAD, COPY, MOVE, ADD) that remove or add arbitrary predicates.
"""

from __future__ import annotations

from rdflib import Graph, URIRef, Variable

from .turtle import looks_like_rdf12

__all__ = ["turtle_predicates", "update_predicates"]


def turtle_predicates(turtle: str) -> frozenset[str] | None:
    """Every predicate used in ``turtle``, or ``None`` if it can't be read."""
    if not turtle or looks_like_rdf12(turtle):
        return None if turtle else frozenset()
    try:
        graph = Graph().parse(data=turtle, format="turtle")
    except Exception:  # noqa: BLE001 - any failure means "could not tell"
        return None
    return frozenset(str(p) for p in set(graph.predicates()))


def _triples(clause) -> list[tuple]:
    """Every triple pattern in an update clause, from its default-graph
    ``triples`` and its per-graph ``quads``."""
    out = list(clause["triples"]) if "triples" in clause else []
    if "quads" in clause:
        for triples in clause["quads"].values():
            out.extend(triples)
    return out


def update_predicates(update: str) -> frozenset[str] | None:
    """Every predicate a SPARQL update inserts or deletes, or ``None``.

    Only the INSERT / DELETE templates count: a WHERE clause reads, it does not
    change anything.
    """
    from rdflib.plugins.sparql.algebra import translateUpdate  # noqa: PLC0415
    from rdflib.plugins.sparql.parser import parseUpdate  # noqa: PLC0415

    try:
        requests = translateUpdate(parseUpdate(update)).algebra
    except Exception:  # noqa: BLE001
        return None

    found: set[str] = set()
    for request in requests:
        if request.name in {"InsertData", "DeleteData", "DeleteWhere"}:
            clauses = [request]
        elif request.name == "Modify":
            # Membership, not .get(): rdflib's CompValue.get() hands back the
            # key itself for a clause that is absent.
            clauses = [request[key] for key in ("delete", "insert") if key in request]
        else:
            # Clear, Drop, Load, Add, Move, Copy, Create: not a statement about
            # particular predicates.
            return None
        for clause in clauses:
            for _s, predicate, _o in _triples(clause):
                if isinstance(predicate, Variable) or not isinstance(predicate, URIRef):
                    return None
                found.add(str(predicate))
    return frozenset(found)
