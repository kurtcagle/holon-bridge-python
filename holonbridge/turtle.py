"""Turtle utilities.

Two things matter here.

1. **Literal escaping.** Anything the bridge writes on a client's behalf —
   scheduler provenance, sequence records, holon metadata — goes through
   :func:`escape_literal`. Unescaped quotes and newlines in generated Turtle
   are the classic silent-write-failure bug.

2. **RDF 1.2.** rdflib parses Turtle 1.1. Jena 6.0 parses Turtle 1.2,
   including triple terms (``<<( s p o )>>``). So payloads are passed to
   Jena unparsed by default and Jena is the syntax authority. Local parsing
   is opt-in and is used only where the bridge genuinely needs the triples
   in-process (delta SHACL, holon projection).

CHANGED 2026-08-28: added :func:`from_json_ld`. create_holon and
create_message both accept a ``json-ld`` DataBook block as an alternative
to ``turtle``/``turtle12`` (see ``databook.DataBook.primary_graph_block``).
Rather than teach ``FusekiClient._gsp`` a second content-type, JSON-LD is
converted to Turtle once, in-process, before it reaches the single GSP
write path -- which stays ``text/turtle`` unconditionally, same as every
other caller. rdflib 6+ parses ``json-ld`` natively; no extra plugin
package required.
"""

from __future__ import annotations

import re
from typing import Iterable

from rdflib import Graph

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

# Turtle 1.2 triple-term / reifier syntax that rdflib 7.x cannot read.
_RDF12_MARKERS = (
    re.compile(r"<<\(", re.MULTILINE),  # triple term
    re.compile(r"~\s*[:<_]", re.MULTILINE),  # reifier
)


class TurtleSyntaxError(ValueError):
    """Raised when local parsing fails and the payload is not RDF 1.2."""


def escape_literal(value: str) -> str:
    """Escape a string for use inside a Turtle short-form literal."""
    return "".join(_ESCAPES.get(ch, ch) for ch in value)


def literal(value: str, *, datatype: str | None = None, lang: str | None = None) -> str:
    """Render a fully-formed Turtle literal, escaping the lexical form."""
    body = f'"{escape_literal(value)}"'
    if lang:
        return f"{body}@{lang}"
    if datatype:
        return f"{body}^^{datatype}"
    return body


def looks_like_rdf12(turtle: str) -> bool:
    """Heuristic: does this payload use syntax rdflib cannot parse?"""
    return any(pattern.search(turtle) for pattern in _RDF12_MARKERS)


def parse(turtle: str, *, base: str | None = None) -> Graph:
    """Parse Turtle 1.1 into an rdflib graph.

    Raises :class:`TurtleSyntaxError` with a pointed message when the payload
    is RDF 1.2, rather than reporting a misleading syntax error.
    """
    graph = Graph()
    try:
        graph.parse(data=turtle, format="turtle", publicID=base)
    except Exception as exc:  # rdflib raises a range of parser errors
        if looks_like_rdf12(turtle):
            raise TurtleSyntaxError(
                "payload appears to use RDF 1.2 syntax, which cannot be parsed "
                "locally; send it through in passthrough mode and let Jena parse it"
            ) from exc
        raise TurtleSyntaxError(str(exc)) from exc
    return graph


def from_json_ld(text: str, *, base: str | None = None) -> str:
    """Convert a JSON-LD payload to Turtle.

    Used by create_holon and create_message when a DataBook's matched
    block is ``json-ld`` rather than ``turtle``/``turtle12`` -- the result
    goes on to the same ``write_turtle_to_graph`` path either way, so
    Fuseki only ever receives ``text/turtle``. Raises
    :class:`TurtleSyntaxError` on a payload rdflib cannot parse as
    JSON-LD, same failure type ``parse`` raises for Turtle, so callers can
    catch one exception regardless of which serialisation a block used.
    """
    graph = Graph()
    try:
        graph.parse(data=text, format="json-ld", publicID=base)
    except Exception as exc:  # rdflib raises a range of parser errors
        raise TurtleSyntaxError(f"could not parse json-ld payload: {exc}") from exc
    return graph.serialize(format="turtle")


def serialise(graph: Graph) -> str:
    """Serialise an rdflib graph back to Turtle."""
    return graph.serialize(format="turtle")


def prefixes(pairs: Iterable[tuple[str, str]]) -> str:
    """Render a prefix preamble."""
    return "\n".join(f"@prefix {p}: <{iri}> ." for p, iri in pairs)
