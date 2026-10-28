"""Fuseki assembler-file provisioning: additive config patches for a dataset.

This module edits Fuseki's own assembler Turtle files on disk -- it does
not talk to Fuseki over HTTP, and it does not touch the running server at
all. A change written here has no effect until Fuseki is restarted (or,
for a directory-mode ``configuration/`` file, until Fuseki re-reads it --
which in practice still means a restart for most deployments).

Deliberately scoped to *additive* changes only, and deliberately textual
rather than parse-and-reserialise: round-tripping an assembler file through
rdflib would preserve the triples but not the human's own formatting,
prefix ordering, or comments, and these files are hand-maintained. A patch
here either finds nothing to do (already present -- idempotent) or appends
new blocks and rewrites exactly the one line that needs to point at them.
It never removes or reorders anything that was already there, and it
refuses to guess when a file doesn't match the expected shape rather than
silently doing something the caller didn't ask for.

This intentionally does NOT create or drop datasets, and does not touch
Fuseki's own admin API. (Creating one is now ``dataset_create.py``, over the
admin API; dropping remains out -- see routes/dataset_admin.py.) Patching a dataset's *existing* assembler file to
add a non-destructive capability (a text index) is narrower and additive --
but it is still a filesystem write on the machine Fuseki runs on, so it is
gated the same way (admin animus only, see routes/dataset_admin.py) for the
same underlying reason: one shared bearer token cannot yet answer "who
asked for this."

CHANGED 2026-09-27: fixed a real bug found the hard way in production --
the entityMap's ``text:map ( ... )`` collection items were being joined
with commas. A comma is valid between objects in a predicate-object list;
it is NOT valid between items of a Turtle collection, which are
whitespace-separated only. Every write this function made before this fix
produced syntactically invalid Turtle, and because Fuseki's own
``readConfigurationDirectory`` aborts the *entire* server on one bad
assembler file rather than skipping it, this took down every dataset on
the instance, not just the one being provisioned -- confirmed live against
kurtcagle's chloe.ttl. Two changes as a result: (1) the join separator is
now whitespace, not a comma; (2) the generated content is now parsed with
:func:`holonbridge.turtle.parse` before ``ensure_text_index`` will write it
or ``preview_text_index`` will return it as a success -- a syntax error is
now a raised :class:`ProvisioningError` naming the parse failure, not a
file quietly written and discovered broken at the next Fuseki restart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .turtle import TurtleSyntaxError
from .turtle import parse as parse_turtle

# Default free-text fields for a newly-provisioned Lucene index. Deliberately
# limited to vocabulary that means "human-readable label or description" in
# any dataset, regardless of that dataset's own domain schema -- rdfs/skos/sh
# label-and-documentation properties, plus dcterms:title. A dataset's own
# domain-specific text fields (e.g. chloe:noteText, chloe:resolutionNote)
# are NOT included here on purpose: they don't exist in a fresh dataset, and
# baking them into the tool's defaults would silently do nothing useful for
# any dataset that isn't chloe. Pass them via ``extra_predicates`` instead.
DEFAULT_TEXT_PREDICATES: tuple[tuple[str, str, str], ...] = (
    ("label", "rdfs", "label"),
    ("comment", "rdfs", "comment"),
    ("prefLabel", "skos", "prefLabel"),
    ("altLabel", "skos", "altLabel"),
    ("definition", "skos", "definition"),
    ("skosComment", "skos", "comment"),
    ("title", "dcterms", "title"),
    ("shName", "sh", "name"),
    ("shDescription", "sh", "description"),
)

_KNOWN_PREFIXES = {
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "sh": "http://www.w3.org/ns/shacl#",
    "dcterms": "http://purl.org/dc/terms/",
    "text": "http://jena.apache.org/text#",
}


class ProvisioningError(ValueError):
    """Assembler file missing, not in a shape this tool will patch, or the
    patch this function would produce does not parse as valid Turtle."""


@dataclass
class ProvisionResult:
    dataset: str
    path: Path
    changed: bool
    reason: str
    new_text: str | None = None  # full proposed file content; None if unchanged
    restart_required: bool = False


def _dataset_file(config_dir: Path, dataset: str) -> Path:
    path = config_dir / f"{dataset}.ttl"
    if not path.is_file():
        raise ProvisioningError(
            f"no assembler file for dataset {dataset!r} at {path} -- this "
            "tool patches an existing file, it does not create one"
        )
    return path


def _plan_text_index(
    dataset: str,
    config_dir: Path,
    *,
    extra_predicates: list[tuple[str, str, str]] | None = None,
    lucene_dir: str | None = None,
) -> ProvisionResult:
    path = _dataset_file(config_dir, dataset)
    text = path.read_text(encoding="utf-8")

    if re.search(r"\btext:TextDataset\b", text):
        return ProvisionResult(
            dataset=dataset,
            path=path,
            changed=False,
            reason="a text:TextDataset is already declared in this file",
        )

    match = re.search(r"(fuseki:dataset\s+)(:\w+)(\s*;)", text)
    if not match:
        raise ProvisioningError(
            f"could not find a `fuseki:dataset :name ;` line in {path} -- "
            "file doesn't match the expected single-dataset service shape, "
            "refusing to guess"
        )
    original_ref = match.group(2)  # e.g. ":tdb_dataset_readwrite"

    predicates = list(DEFAULT_TEXT_PREDICATES) + list(extra_predicates or [])
    unknown = {
        prefix
        for _, prefix, _ in predicates
        if prefix not in _KNOWN_PREFIXES and f"PREFIX {prefix}:" not in text
    }
    if unknown:
        raise ProvisioningError(
            f"extra_predicates uses prefix(es) {sorted(unknown)} that are "
            "neither already declared in the file nor one this tool knows "
            "how to add -- declare the PREFIX yourself first"
        )

    # 1. Repoint the service at a new :text_dataset resource.
    patched = (
        text[: match.start()]
        + match.group(1)
        + ":text_dataset"
        + match.group(3)
        + text[match.end() :]
    )

    # 2. Add any PREFIX lines this block needs that aren't already present,
    #    inserted right after the file's existing PREFIX block.
    needed = {"text": _KNOWN_PREFIXES["text"]}
    for _, prefix, _ in predicates:
        if prefix in _KNOWN_PREFIXES:
            needed[prefix] = _KNOWN_PREFIXES[prefix]
    missing = {p: iri for p, iri in needed.items() if f"PREFIX {p}:" not in patched}
    if missing:
        prefix_lines = "\n".join(
            f"PREFIX {p}: <{iri}>" for p, iri in sorted(missing.items())
        )
        last_prefix = None
        for m in re.finditer(r"^PREFIX\s+\w+:\s+<[^>]+>\s*$", patched, re.MULTILINE):
            last_prefix = m
        if last_prefix:
            insert_at = last_prefix.end()
            patched = patched[:insert_at] + "\n" + prefix_lines + patched[insert_at:]
        else:
            patched = prefix_lines + "\n\n" + patched

    # 3. Build the entityMap field list and append the new blocks. Collection
    #    items in Turtle ``( ... )`` are whitespace-separated -- NOT joined
    #    with commas, which is a predicate-object-list separator and invalid
    #    here. (Confirmed with a parser after the earlier comma bug shipped;
    #    see module docstring.)
    field_lines = "\n".join(
        f'            [ text:field "{field}" ; text:predicate {prefix}:{local} ]'
        for field, prefix, local in predicates
    )

    lucene_path = lucene_dir or f"c:/Apache/apache-jena-fuseki-6.1.0/run/lucene/{dataset}"

    block = f"""

:text_dataset  a  text:TextDataset ;
        text:dataset  {original_ref} ;
        text:index    :indexLucene .

:indexLucene  a  text:TextIndexLucene ;
        text:directory  <file:///{lucene_path}> ;
        text:entityMap  :entMap .

:entMap  a  text:EntityMap ;
        text:entityField   "uri" ;
        text:defaultField  "label" ;
        text:map (
{field_lines}
        ) .
"""
    patched = patched.rstrip() + "\n" + block

    # 4. Never hand back (or write) content that doesn't actually parse.
    # This is the safety net the earlier comma bug should have been caught
    # by -- a syntax error here becomes a raised ProvisioningError, not a
    # file that looks fine until Fuseki's own restart discovers otherwise.
    try:
        parse_turtle(patched)
    except TurtleSyntaxError as exc:
        raise ProvisioningError(
            f"the patch this would produce for {dataset!r} does not parse as "
            f"valid Turtle ({exc}) -- refusing to write or preview it. This "
            "is a bug in provisioning.py itself, not in your existing file; "
            "please report it rather than working around it."
        ) from exc

    return ProvisionResult(
        dataset=dataset,
        path=path,
        changed=True,
        reason=(
            f"added text:TextDataset wrapping {original_ref}, with "
            f"{len(predicates)} indexed fields"
        ),
        new_text=patched,
        restart_required=True,
    )


def preview_text_index(
    dataset: str,
    config_dir: Path,
    *,
    extra_predicates: list[tuple[str, str, str]] | None = None,
    lucene_dir: str | None = None,
) -> ProvisionResult:
    """Compute the patch for :func:`ensure_text_index` without writing it."""
    return _plan_text_index(
        dataset, config_dir, extra_predicates=extra_predicates, lucene_dir=lucene_dir
    )


def ensure_text_index(
    dataset: str,
    config_dir: Path,
    *,
    extra_predicates: list[tuple[str, str, str]] | None = None,
    lucene_dir: str | None = None,
) -> ProvisionResult:
    """Add a Lucene (``jena-text``) index to a dataset's assembler file.

    Idempotent: if the file already declares a ``text:TextDataset``, this
    returns unchanged with ``changed=False`` rather than layering a second
    index on top -- re-running this against an already-provisioned dataset
    is always safe.

    ``extra_predicates`` extends :data:`DEFAULT_TEXT_PREDICATES` with fields
    specific to this dataset's own schema, e.g. ``[("noteText", "chloe",
    "noteText"), ("resolutionNote", "chloe", "resolutionNote")]`` for chloe.
    Each entry is ``(field_name, prefix, local_name)``; the prefix must
    already be declared in the file or be one this function knows how to
    add (rdfs/skos/sh/dcterms/text) -- an unknown prefix raises rather than
    silently emitting a Turtle file that won't parse.

    Writes the file only after confirming the result actually parses as
    Turtle -- see the module docstring for why that check exists. Does not
    restart Fuseki -- ``ProvisionResult.restart_required`` is always
    ``True`` on a real change, as a reminder that the caller still owns
    that step.
    """
    result = _plan_text_index(
        dataset, config_dir, extra_predicates=extra_predicates, lucene_dir=lucene_dir
    )
    if result.changed and result.new_text is not None:
        result.path.write_text(result.new_text, encoding="utf-8")
    return result
