"""JSON-LD input and output.

The store speaks Turtle. This module is the only place JSON-LD is converted to
or from it, so the rules below hold for every caller: ``/graph/push``,
``/sparql/construct``, ``GET /graph``, and the DataBook ``json-ld`` block that
``create_holon`` and ``create_message`` accept.

**Input** is parsed in-process by rdflib (rdflib 7 reads JSON-LD 1.1), then
re-serialised as Turtle, so Fuseki still only ever receives ``text/turtle``.
Three things a bare ``Graph.parse`` gets wrong are handled here:

* *Remote contexts.* JSON-LD lets a document name a ``@context`` by URL, and
  rdflib fetches it during the parse -- from the bridge's network position, with
  no egress policy, before any authorisation check. A payload that names one is
  refused. ``JSONLD_FETCH_CONTEXTS=true`` instead fetches each one through
  :func:`holonbridge.fetch.fetch_source` (the ``/ingest`` egress policy and size
  cap) and inlines it, so the parser never touches the network itself.
  ``@import`` is refused either way.
* *Named graphs.* A JSON-LD document can carry named graphs; the store write
  targets one graph chosen by the caller. rdflib's plain ``Graph`` silently drops
  the named ones, so they are refused with a message naming them. Default-graph
  documents (including a top-level ``@graph`` array) are unaffected.
* *Silent emptiness.* JSON-LD drops any property its context does not map. A
  document that converts to no triples at all is refused, because writing
  nothing (or, with ``mode=replace``, erasing the graph) is never what the
  caller meant. A document where only *some* keys are unmapped still loses
  those keys without error -- that is how JSON-LD defines expansion, and the
  conversion cannot tell from the outside; the README says so.
* *Size.* ``JSONLD_MAX_BYTES``, both ways.

**Output** is produced by rdflib by default: the store's Turtle is parsed and
written as compact JSON-LD, with the Turtle's prefixes as the ``@context``
(or expanded JSON-LD when asked). ``JSONLD_OUTPUT_ENGINE=jena`` asks Fuseki for
``application/ld+json`` instead and falls back to rdflib when Fuseki cannot
produce it. JSON-LD cannot express RDF 1.2 triple terms; a result that contains
one is refused with a pointer back to Turtle.

Honest limit: the ``jena`` engine and Jena's JSON-LD writer are covered by tests
against a stubbed Fuseki only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from rdflib import Dataset, Graph

from .config import Settings, get_settings
from .fuseki import FusekiError
from .turtle import TurtleSyntaxError, looks_like_rdf12

log = logging.getLogger(__name__)

JSONLD_MEDIA_TYPE = "application/ld+json"
TURTLE_MEDIA_TYPE = "text/turtle"

#: Rounds of "fetch the contexts the document names, inline them, look again".
MAX_CONTEXT_ROUNDS = 3
#: Distinct remote contexts fetched for one document.
MAX_CONTEXT_FETCHES = 10

#: Upstream statuses that mean "Fuseki cannot write this format", as opposed to
#: "the request failed" -- only the former falls back to rdflib.
_JENA_FALLBACK_STATUSES = frozenset({400, 406, 415, 501})


class JsonLdError(TurtleSyntaxError):
    """The payload cannot be used as JSON-LD.

    Subclasses :class:`TurtleSyntaxError` so the callers that already catch that
    for a ``json-ld`` DataBook block keep working unchanged.
    """


class JsonLdTooLarge(JsonLdError):
    """The document exceeds ``JSONLD_MAX_BYTES``."""


class JsonLdUnsupported(JsonLdError):
    """The data is valid but JSON-LD cannot represent it (RDF 1.2 triple terms)."""


def settings_for(request: Request) -> Settings:
    """The app's settings, or the process settings for an app that has none.

    The read routes did not depend on settings before JSON-LD output existed, so
    an app assembled without ``app.state.settings`` (tests, embedders) keeps
    working for a plain Turtle read.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


def to_http(exc: JsonLdError) -> HTTPException:
    if isinstance(exc, JsonLdTooLarge):
        return HTTPException(413, detail=str(exc))
    if isinstance(exc, JsonLdUnsupported):
        return HTTPException(422, detail=str(exc))
    return HTTPException(400, detail=str(exc))


# --- input ---------------------------------------------------------------------


def _check_size(size: int, limit: int, what: str) -> None:
    if size > limit:
        raise JsonLdTooLarge(
            f"{what} is {size} bytes; JSONLD_MAX_BYTES is {limit}. "
            "Use Turtle for a payload this large."
        )


def _load_json(text: str, limit: int) -> Any:
    _check_size(len(text.encode("utf-8")), limit, "json-ld payload")
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise JsonLdError(f"could not parse json-ld payload: not valid JSON ({exc})") from exc
    if not isinstance(doc, (dict, list)):
        raise JsonLdError("could not parse json-ld payload: top level must be an object or array")
    return doc


@dataclass
class _Ref:
    """One remote context reference: ``container[key]`` is the URL string."""

    container: Any
    key: Any
    url: str


def _walk_contexts(doc: Any) -> list[_Ref]:
    """Every string ``@context`` value in ``doc``, wherever it sits.

    Raises on ``@import``. Iterative: a hostile document can nest deeper than the
    interpreter will recurse. A ``@context`` key inside a JSON literal
    (``@type: @json``) is reported too -- refusing a harmless document is the
    safe direction to be wrong in.
    """
    refs: list[_Ref] = []
    stack: list[Any] = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "@context":
                    entries = value if isinstance(value, list) else [value]
                    for index, entry in enumerate(entries):
                        if isinstance(entry, str):
                            refs.append(
                                _Ref(value, index, entry)
                                if isinstance(value, list)
                                else _Ref(node, key, entry)
                            )
                        elif isinstance(entry, dict) and "@import" in entry:
                            raise JsonLdError(
                                "json-ld @import is not supported; inline the imported context"
                            )
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(item for item in node if isinstance(item, (dict, list)))
    return refs


def _refuse_remote(refs: list[_Ref]) -> None:
    if not refs:
        return
    shown = sorted({r.url for r in refs})[:3]
    raise JsonLdError(
        "json-ld payload names a remote @context "
        f"({', '.join(shown)}); the bridge does not fetch remote contexts. "
        "Inline the context, or have the operator set JSONLD_FETCH_CONTEXTS=true."
    )


def _parse_default_graph(doc: Any, *, base: str | None) -> Graph:
    """Parse a JSON-LD document that has no remote context left in it."""
    dataset = Dataset()
    try:
        dataset.parse(data=json.dumps(doc), format="json-ld", publicID=base)
    except Exception as exc:  # rdflib raises a range of parser errors
        raise JsonLdError(f"could not parse json-ld payload: {exc}") from exc

    default_id = dataset.default_graph.identifier
    named = [g for g in dataset.graphs() if g.identifier != default_id and len(g) > 0]
    if named:
        names = ", ".join(str(g.identifier) for g in named[:3])
        more = "" if len(named) <= 3 else f" (and {len(named) - 3} more)"
        raise JsonLdError(
            f"json-ld payload contains named graphs ({names}{more}); only a "
            "default-graph document can be written into the one graph you choose. "
            "Flatten it, or push each graph separately."
        )

    graph = Graph()
    for triple in dataset.default_graph:
        graph.add(triple)
    if len(graph) == 0:
        raise JsonLdError(
            "json-ld payload produced no triples. JSON-LD drops any property its "
            "@context (or @vocab) does not map to an IRI, so a document with no "
            "context, or with unmapped keys, can convert to nothing without error. "
            "Add a @context or @vocab, or use full IRIs as keys."
        )
    return graph


def _to_turtle(graph: Graph) -> str:
    return graph.serialize(format="turtle")


def from_json_ld_local(text: str, *, base: str | None = None, max_bytes: int | None = None) -> str:
    """JSON-LD to Turtle with remote contexts refused. Synchronous.

    This is what ``turtle.from_json_ld`` calls. Callers that run in a request
    handler should prefer :func:`json_ld_to_turtle`, which also honours
    ``JSONLD_FETCH_CONTEXTS``.
    """
    limit = max_bytes if max_bytes is not None else get_settings().jsonld_max_bytes
    doc = _load_json(text, limit)
    _refuse_remote(_walk_contexts(doc))
    return _to_turtle(_parse_default_graph(doc, base=base))


async def json_ld_to_turtle(
    text: str,
    *,
    settings: Settings,
    base: str | None = None,
    fetch: Callable[[str], Awaitable[tuple[str, str]]] | None = None,
) -> str:
    """JSON-LD to Turtle, for use inside a request handler.

    With ``settings.jsonld_fetch_contexts`` off (the default) this is
    :func:`from_json_ld_local`. With it on, remote string contexts are fetched
    via ``fetch`` (default: :func:`holonbridge.fetch.fetch_source`, which applies
    the egress policy and size cap) and inlined before the parse.
    """
    limit = settings.jsonld_max_bytes
    doc = _load_json(text, limit)

    if not settings.jsonld_fetch_contexts:
        _refuse_remote(_walk_contexts(doc))
        return _to_turtle(_parse_default_graph(doc, base=base))

    if fetch is None:
        from .fetch import fetch_source

        async def _default_fetch(url: str) -> tuple[str, str]:
            return await fetch_source(url, max_bytes=limit)

        fetch = _default_fetch

    fetched: dict[str, Any] = {}
    for _ in range(MAX_CONTEXT_ROUNDS):
        refs = _walk_contexts(doc)
        if not refs:
            break
        for ref in refs:
            if ref.url not in fetched:
                if len(fetched) >= MAX_CONTEXT_FETCHES:
                    raise JsonLdError(
                        f"json-ld payload names more than {MAX_CONTEXT_FETCHES} remote contexts"
                    )
                fetched[ref.url] = await _fetch_context(ref.url, fetch, limit)
            # Each use gets its own copy: the walk mutates nothing but this slot.
            ref.container[ref.key] = json.loads(json.dumps(fetched[ref.url]))
    _refuse_remote(_walk_contexts(doc))  # still nested after the last round
    return _to_turtle(_parse_default_graph(doc, base=base))


async def _fetch_context(
    url: str, fetch: Callable[[str], Awaitable[tuple[str, str]]], limit: int
) -> Any:
    if urlsplit(url).scheme not in ("http", "https"):
        raise JsonLdError(f"json-ld @context {url!r} is not an absolute http(s) URL")
    from .fetch import SourceFetchError

    try:
        body, _content_type = await fetch(url)
    except SourceFetchError as exc:
        raise JsonLdError(f"could not fetch json-ld @context: {exc}") from exc
    _check_size(len(body.encode("utf-8")), limit, f"json-ld @context {url}")
    try:
        document = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise JsonLdError(f"json-ld @context {url} is not valid JSON") from exc
    if not isinstance(document, dict) or "@context" not in document:
        raise JsonLdError(f"json-ld @context {url} has no top-level @context")
    return document["@context"]


# --- output --------------------------------------------------------------------


@dataclass(frozen=True)
class Rendered:
    text: str
    media_type: str
    #: Which engine produced it, for the ``X-JSONLD-Engine`` header: ``rdflib``,
    #: ``jena``, or ``rdflib (jena unavailable)``.
    engine: str | None = None


def turtle_to_json_ld(turtle: str, *, compact: bool = True, max_bytes: int | None = None) -> str:
    """Turtle to JSON-LD with rdflib.

    Compact output uses the Turtle's own prefixes as the ``@context``; with none
    declared it is effectively expanded. ``compact=False`` is expanded JSON-LD
    (full IRIs, no ``@context``) -- the form that needs no context to read.
    """
    limit = max_bytes if max_bytes is not None else get_settings().jsonld_max_bytes
    if looks_like_rdf12(turtle):
        raise JsonLdUnsupported(
            "this result uses RDF 1.2 triple terms, which JSON-LD cannot express; "
            "request format=turtle"
        )
    _check_size(len(turtle.encode("utf-8")), limit, "result")
    graph = Graph(bind_namespaces="none")
    try:
        graph.parse(data=turtle, format="turtle")
    except Exception as exc:
        raise JsonLdError(f"could not read the store's Turtle to convert it: {exc}") from exc
    if len(graph) == 0:
        return "[]"
    context = {p: str(ns) for p, ns in graph.namespaces() if p} if compact else None
    text = graph.serialize(format="json-ld", context=context, indent=2)
    _check_size(len(text.encode("utf-8")), limit, "JSON-LD result")
    return text


async def render(
    turtle_fn: Callable[[], Awaitable[str]],
    jena_fn: Callable[[], Awaitable[str]],
    *,
    fmt: str,
    form: str,
    settings: Settings,
) -> Rendered:
    """Produce ``fmt`` (``turtle`` or ``json-ld``) from the two ways of asking Fuseki.

    ``turtle_fn`` returns the result as Turtle. ``jena_fn`` returns it as
    ``application/ld+json``; it is only called for the ``jena`` engine with the
    compact form, since Fuseki offers no way to ask for expanded output.
    """
    if fmt == "turtle":
        return Rendered(await turtle_fn(), TURTLE_MEDIA_TYPE)

    compact = form == "compact"
    if settings.jsonld_output_engine == "jena" and compact:
        jena_text: str | None = None
        try:
            jena_text = await jena_fn()
        except FusekiError as exc:
            if exc.status not in _JENA_FALLBACK_STATUSES:
                raise
            log.warning("Fuseki could not write JSON-LD (%s); using rdflib", exc.status)
        if jena_text is not None:
            try:
                json.loads(jena_text)
            except ValueError:
                log.warning("Fuseki returned JSON-LD that is not valid JSON; using rdflib")
            else:
                _check_size(
                    len(jena_text.encode("utf-8")), settings.jsonld_max_bytes, "JSON-LD result"
                )
                return Rendered(jena_text, JSONLD_MEDIA_TYPE, "jena")
        return Rendered(
            turtle_to_json_ld(await turtle_fn(), compact=True, max_bytes=settings.jsonld_max_bytes),
            JSONLD_MEDIA_TYPE,
            "rdflib (jena unavailable)",
        )

    return Rendered(
        turtle_to_json_ld(await turtle_fn(), compact=compact, max_bytes=settings.jsonld_max_bytes),
        JSONLD_MEDIA_TYPE,
        "rdflib",
    )
