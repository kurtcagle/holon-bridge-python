"""Classify a SPARQL string as a read or an update, and identify its form.

Reads and updates go to different Fuseki endpoints, and sending one to the
other's endpoint produces a 400 that says nothing useful. Both the raw SPARQL
routes and the named-query runner need the same judgement, so it lives here.

This is deliberately a lexical check, not a parse. It exists to catch the
obvious mistake early; Jena remains the authority on whether a query is valid.
"""

from __future__ import annotations

import re
from typing import Literal

Kind = Literal["read", "update", "unknown"]
Form = Literal["SELECT", "CONSTRUCT", "ASK", "DESCRIBE", "UPDATE", "UNKNOWN"]

_PROLOGUE = re.compile(
    r"^\s*(?:(?:PREFIX\s+[^\s:]*:\s*<[^>]*>)|(?:BASE\s*<[^>]*>))\s*", re.IGNORECASE
)
_STRING_DELIMITERS = ('"""', "\'\'\'", '"', "\'")


def strip_comments(sparql: str) -> str:
    """Remove ``#`` comments without touching ``#`` inside IRIs or literals.

    A regex cannot do this. Almost every RDF namespace ends in ``#``, so a
    naive stripper truncates ``<...XMLSchema#>`` and everything after it on
    that line — which silently turns a valid update into an unclassifiable
    fragment.
    """
    out: list[str] = []
    i = 0
    length = len(sparql)

    while i < length:
        ch = sparql[i]

        if ch == "#":
            end = sparql.find("\n", i)
            if end == -1:
                break
            i = end
            continue

        if ch == "<" and _is_iri_start(sparql, i):
            end = sparql.index(">", i)
            out.append(sparql[i : end + 1])
            i = end + 1
            continue

        for delimiter in _STRING_DELIMITERS:
            if sparql.startswith(delimiter, i):
                end = _end_of_string(sparql, i + len(delimiter), delimiter)
                out.append(sparql[i:end])
                i = end
                break
        else:
            out.append(ch)
            i += 1

    return "".join(out)


def _is_iri_start(sparql: str, i: int) -> bool:
    """Distinguish an IRI reference from a less-than operator."""
    j = i + 1
    while j < len(sparql):
        ch = sparql[j]
        if ch == ">":
            return True
        if ch.isspace() or ch in "<\"{}|^`":
            return False
        j += 1
    return False


def _end_of_string(sparql: str, i: int, delimiter: str) -> int:
    while i < len(sparql):
        if sparql[i] == "\\":
            i += 2
            continue
        if sparql.startswith(delimiter, i):
            return i + len(delimiter)
        i += 1
    return len(sparql)


_READ_FORMS = ("SELECT", "CONSTRUCT", "ASK", "DESCRIBE")
_UPDATE_FORMS = (
    "INSERT",
    "DELETE",
    "LOAD",
    "CLEAR",
    "DROP",
    "CREATE",
    "COPY",
    "MOVE",
    "ADD",
    "WITH",
)


def _strip(sparql: str) -> str:
    """Remove comments and the prologue, leaving the first real keyword."""
    body = strip_comments(sparql)
    previous = None
    while previous != body:
        previous = body
        body = _PROLOGUE.sub("", body, count=1)
    return body.lstrip()


def form(sparql: str) -> Form:
    """The query form, determined from the first keyword after the prologue."""
    head = _strip(sparql).upper()
    for candidate in _READ_FORMS:
        if head.startswith(candidate):
            return candidate  # type: ignore[return-value]
    for candidate in _UPDATE_FORMS:
        if head.startswith(candidate):
            return "UPDATE"
    return "UNKNOWN"


def classify(sparql: str) -> Kind:
    """``read``, ``update``, or ``unknown`` when the first keyword is neither."""
    detected = form(sparql)
    if detected == "UPDATE":
        return "update"
    if detected == "UNKNOWN":
        return "unknown"
    return "read"


def accepts_values_clause(sparql: str) -> bool:
    """Can a ``VALUES`` clause be appended to this query?

    Only query forms take a trailing ``ValuesClause``; updates do not. A query
    that already ends in one cannot take a second.
    """
    if classify(sparql) != "read":
        return False
    return not re.search(
        r"\bVALUES\b[^}]*\}\s*$", strip_comments(sparql), re.IGNORECASE
    )
