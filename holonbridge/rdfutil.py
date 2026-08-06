"""Small helpers for working with SPARQL result rows as property bags.

The registries all load the same way: select ``?s ?p ?o`` for typed subjects,
group by subject, then match properties by local name. Matching on local names
rather than full IRIs is what lets one loader read vocabularies that spell the
same concept differently — see the named-query module for why that matters.
"""

from __future__ import annotations

from typing import Any, Mapping

Props = dict[str, list[str]]


def local_name(iri: str) -> str:
    """Everything after the last ``/`` or ``#``."""
    cut = max(iri.rfind("/"), iri.rfind("#"))
    return iri[cut + 1 :] if cut >= 0 else iri


def namespace(iri: str) -> str:
    """Everything up to and including the last ``/`` or ``#``."""
    cut = max(iri.rfind("/"), iri.rfind("#"))
    return iri[: cut + 1] if cut >= 0 else iri


def collect(rows: list[dict[str, Any]], subject_var: str) -> dict[str, Props]:
    """Group ``?s ?p ?o`` bindings into ``{subject: {local name: [values]}}``."""
    out: dict[str, Props] = {}
    for row in rows:
        subject = row[subject_var]["value"]
        predicate = local_name(row["p"]["value"])
        out.setdefault(subject, {}).setdefault(predicate, []).append(row["o"]["value"])
    return out


def pick(props: Mapping[str, list[str]], names: tuple[str, ...]) -> str | None:
    """First present value across a precedence-ordered list of local names."""
    for name in names:
        if props.get(name):
            return props[name][0]
    return None


def pick_all(props: Mapping[str, list[str]], names: tuple[str, ...]) -> list[str]:
    """Every value across the named properties, de-duplicated, order preserved."""
    seen: list[str] = []
    for name in names:
        for value in props.get(name, []):
            if value not in seen:
                seen.append(value)
    return seen


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes"}
