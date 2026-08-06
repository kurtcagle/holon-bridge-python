"""Parameter binding primitives.

This module is a security boundary. Every caller-supplied value that reaches
a SPARQL string passes through :func:`render_term`, which emits a complete,
escaped SPARQL term or raises. Nothing is ever pasted in raw.

Datatypes come from the registry's parameter declarations, never from the
Python type of the supplied value — so a query's behaviour does not depend
on how a caller happened to type an argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

XSD = "http://www.w3.org/2001/XMLSchema#"

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_IRI_UNSAFE = re.compile(r"[<>\"{}|\\^`\s]")
_VARNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Datatype local names that mean "this parameter is an IRI, not a literal".
IRI_DATATYPES = frozenset({"anyURI", "IRI", "iri", "uri", "URI", "Resource"})

_LITERAL_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class ParameterError(ValueError):
    """A supplied parameter cannot be rendered as the declared datatype."""


@dataclass(frozen=True)
class Parameter:
    """A parameter declaration as it appears in the registry."""

    name: str
    datatype: str | None = None
    description: str = ""
    required: bool = False
    default: str | None = None


# --- term rendering -----------------------------------------------------------


def _local_name(iri: str) -> str:
    cut = max(iri.rfind("/"), iri.rfind("#"))
    return iri[cut + 1 :] if cut >= 0 else iri


def _expand_datatype(datatype: str) -> str:
    if datatype.startswith("xsd:"):
        return f"{XSD}{datatype[4:]}"
    return datatype


def is_iri_datatype(datatype: str | None) -> bool:
    """Whether a declared datatype renders {{placeholder}} as <an IRI reference>.

    Public because more than parameter substitution needs this now: the SHACL
    reduction hook checks a rule's own scope_graph parameter the same way,
    to fail with a clear message rather than a Jena syntax error two layers
    downstream.
    """
    if not datatype:
        return False
    return _local_name(_expand_datatype(datatype)) in IRI_DATATYPES


def escape_literal(value: str) -> str:
    return "".join(_LITERAL_ESCAPES.get(ch, ch) for ch in value)


def render_term(value: Any, datatype: str | None = None) -> str:
    """Render one value as a complete SPARQL term.

    Raises :class:`ParameterError` rather than emitting anything it cannot
    vouch for.
    """
    if value is None:
        raise ParameterError("cannot render a null parameter value")

    text = str(value)

    if is_iri_datatype(datatype):
        if not _SCHEME.match(text):
            raise ParameterError(
                f"parameter declared as an IRI but {text!r} has no scheme"
            )
        if _IRI_UNSAFE.search(text):
            raise ParameterError(
                f"parameter declared as an IRI but {text!r} contains characters "
                "that cannot appear in an IRI reference"
            )
        return f"<{text}>"

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)

    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ParameterError(f"cannot render non-finite number {value!r}")
        return repr(value)

    if not datatype:
        return f'"{escape_literal(text)}"'

    expanded = _expand_datatype(datatype)
    _check_lexical_form(text, expanded)
    return f'"{escape_literal(text)}"^^<{expanded}>'


def _check_lexical_form(text: str, datatype: str) -> None:
    """Catch the lexical-form errors that produce silent wrong answers."""
    local = _local_name(datatype)

    if local in {"integer", "int", "long", "short", "nonNegativeInteger", "positiveInteger"}:
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ParameterError(f"{text!r} is not a valid xsd:{local}")

    elif local in {"decimal", "double", "float"}:
        if not re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text):
            raise ParameterError(f"{text!r} is not a valid xsd:{local}")

    elif local == "boolean":
        if text not in {"true", "false", "0", "1"}:
            raise ParameterError(f"{text!r} is not a valid xsd:boolean")

    elif local == "dateTime":
        # A dateTime without a timezone compares indeterminately against
        # timezone-qualified values within +/-14 hours, so a recent-window
        # filter silently returns nothing while a distant one works. Refuse
        # the value rather than guessing the caller meant UTC.
        if not re.fullmatch(
            r"-?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})", text
        ):
            raise ParameterError(
                f"{text!r} is not a valid xsd:dateTime with a timezone. "
                "Comparisons against timezone-qualified values are indeterminate "
                "without one — append 'Z' or an explicit offset."
            )

    elif local == "date":
        if not re.fullmatch(r"-?\d{4}-\d{2}-\d{2}(Z|[+-]\d{2}:\d{2})?", text):
            raise ParameterError(f"{text!r} is not a valid xsd:date")


# --- hb: placeholder substitution ---------------------------------------------


@dataclass
class Substitution:
    sparql: str
    substituted: list[str]
    missing: list[str]


def substitute_params(
    sparql: str,
    supplied: Mapping[str, Any],
    declared: Mapping[str, Parameter] | None = None,
) -> Substitution:
    """Replace ``{{name}}`` placeholders with rendered terms.

    Placeholders with no supplied value are left in place and reported in
    ``missing``. Leaving them untouched matters: a query with an unresolved
    placeholder fails to parse, which is the correct outcome. Blanking them
    would produce a syntactically valid query that means something else.
    """
    declared = declared or {}
    substituted: list[str] = []
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in supplied or supplied[name] is None:
            default = declared.get(name).default if name in declared else None
            if default is None:
                missing.append(name)
                return match.group(0)
            value: Any = default
        else:
            value = supplied[name]

        datatype = declared[name].datatype if name in declared else None
        term = render_term(value, datatype)
        substituted.append(name)
        return term

    return Substitution(
        sparql=_PLACEHOLDER.sub(replace, sparql),
        substituted=substituted,
        missing=sorted(set(missing)),
    )


def placeholders(sparql: str) -> list[str]:
    """Every distinct ``{{name}}`` in a query body, in order of appearance."""
    seen: list[str] = []
    for match in _PLACEHOLDER.finditer(sparql):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


# --- hquery: VALUES binding ---------------------------------------------------


def values_clause(bindings: Iterable[tuple[str, str]]) -> str:
    """Build a ``VALUES`` clause from ``(variable, rendered term)`` pairs.

    SPARQL's grammar places ``ValuesClause`` after the whole query form —
    past ORDER BY and LIMIT — so this can be appended to a well-formed query
    without parsing it, and cannot corrupt one.
    """
    pairs = list(bindings)
    if not pairs:
        return ""

    for name, _ in pairs:
        if not _VARNAME.match(name):
            raise ParameterError(f"{name!r} is not a usable SPARQL variable name")

    if len(pairs) == 1:
        name, term = pairs[0]
        return f"VALUES ?{name} {{ {term} }}"

    names = " ".join(f"?{name}" for name, _ in pairs)
    terms = " ".join(term for _, term in pairs)
    return f"VALUES ({names}) {{ ({terms}) }}"
