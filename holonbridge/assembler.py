"""The Fuseki assembler the bridge generates for a dataset it creates.

Fuseki's admin API can create a dataset from just ``dbName``/``dbType``, but the
dataset that results has no SHACL endpoint (observed on a dataset made that way;
the services it reports are query, update and the two graph-store operations).
The bridge's validation needs ``/{dataset}/shacl``, so creation goes through an
assembler instead: the same standard services plus ``fuseki:shacl``.

The assembler is **generated, never relayed**. Its only inputs are a dataset
name that has already passed the strict name check and, for a persistent
dataset, a database directory the operator configured -- so a caller cannot
name arbitrary paths or Java classes the way a caller-supplied assembler could.
The text is parsed before it is returned, so a template mistake fails here
rather than as an opaque Fuseki error (or, worse, a file Fuseki cannot load).
"""

from __future__ import annotations

import re

from .turtle import TurtleSyntaxError, escape_literal
from .turtle import parse as parse_turtle

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: ``(endpoint name, fuseki operation)``. ``/query``, ``/update``, ``/data`` and
#: ``/shacl`` are what the bridge calls; ``get`` is the read-only graph store.
ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("query", "query"),
    ("update", "update"),
    ("get", "gsp-r"),
    ("data", "gsp-rw"),
    ("shacl", "shacl"),
)


def build_dataset_assembler(name: str, *, persistent: bool, location: str | None = None) -> str:
    """Turtle for a Fuseki service named ``name`` with the standard endpoints and SHACL.

    ``location`` is the absolute TDB2 directory and is required (only) when
    ``persistent``. Raises ``ValueError`` for a bad name, a missing location, or
    output that does not parse.
    """
    if not _NAME.match(name or ""):
        raise ValueError(f"unsafe dataset name for an assembler: {name!r}")
    if persistent and not location:
        raise ValueError("a persistent dataset needs an absolute database location")

    endpoints = " ;\n".join(
        f'    fuseki:endpoint [ fuseki:operation fuseki:{op} ; fuseki:name "{ep}" ]'
        for ep, op in ENDPOINTS
    )
    if persistent:
        dataset = (
            ":dataset rdf:type tdb2:DatasetTDB2 ;\n"
            f'    tdb2:location "{escape_literal(location or "")}" .'
        )
    else:
        dataset = ":dataset rdf:type ja:MemoryDataset ."

    text = f"""@prefix :       <#> .
@prefix fuseki: <http://jena.apache.org/fuseki#> .
@prefix rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix ja:     <http://jena.hpl.hp.com/2005/11/Assembler#> .
@prefix tdb2:   <http://jena.apache.org/2016/tdb#> .

:service rdf:type fuseki:Service ;
    fuseki:name "{name}" ;
{endpoints} ;
    fuseki:dataset :dataset .

{dataset}
"""
    try:
        parse_turtle(text)
    except TurtleSyntaxError as exc:  # a bug in this template, not in the caller's input
        raise ValueError(f"generated assembler does not parse: {exc}") from exc
    return text
