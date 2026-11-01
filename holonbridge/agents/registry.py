"""The ``hb:AgentEndpoint`` registry: loading, and the Turtle endpoints are stored as.

Same shape as the trigger registry (``trigger_registry.py``) and for the same
reason: the loader matches properties by local name, so a hand-authored entry
and one written through the API are indistinguishable to everything downstream.

Nothing secret is stored. ``credentialRef`` and ``signingRef`` are ``env:NAME``
references (see ``policy.py``).
"""

from __future__ import annotations

import logging
import re

from ..conn import Conn
from ..fuseki import FusekiClient, FusekiError
from ..rdfutil import collect, pick, truthy
from ..turtle import literal
from .model import (
    AUTH_SCHEMES,
    DEFAULT_MAX_REQUEST_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    ENDPOINT_CLASS_SUFFIX,
    ENDPOINT_STATUSES,
    HARD_MAX_REQUEST_BYTES,
    HARD_MAX_RESPONSE_BYTES,
    HB,
    MAX_TIMEOUT_SECONDS,
    Endpoint,
    EndpointLoadResult,
    clamp,
)

log = logging.getLogger("holonbridge.agents")

__all__ = [
    "InvalidEndpoint",
    "deletion_update",
    "endpoint_node",
    "load_endpoints",
    "registration_update",
    "render_endpoint_turtle",
    "status_update",
]

_XSD = "http://www.w3.org/2001/XMLSchema#"
_SAFE_IRI = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>"{}|\\^`]*$')
#: RFC 9110 token characters: what may be a header name.
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
_FORBIDDEN_HEADERS = {
    "host", "content-length", "content-type", "transfer-encoding", "connection",
    "idempotency-key", "x-holonbridge-call", "x-holonbridge-signature",
    "x-holonbridge-timestamp",
}

_FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("id", "identifier"),
    "label": ("label", "title"),
    "description": ("description", "comment"),
    "url": ("endpoint", "url"),
    "status": ("agentStatus", "status"),
    "auth_scheme": ("authScheme",),
    "auth_header": ("authHeader",),
    "credential_ref": ("credentialRef",),
    "signing_ref": ("signingRef",),
    "timeout": ("timeoutSeconds",),
    "max_response": ("maxResponseBytes",),
    "max_request": ("maxRequestBytes",),
    "call_review": ("callReview",),
    "registered_by": ("registeredBy",),
}


class InvalidEndpoint(ValueError):
    """An endpoint definition that cannot be stored as given."""


def endpoint_node(conn: Conn, endpoint_id: str) -> str:
    """``urn:{dataset}:agent:{id}`` -- the IRI an endpoint is stored under."""
    return conn.scoped("agents", endpoint_id)


# --- loading ----------------------------------------------------------------------


def _endpoints_query(graph: str) -> str:
    return f"""SELECT ?e ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?e a ?type .
    FILTER( STRENDS(STR(?type), "{ENDPOINT_CLASS_SUFFIX}") )
    ?e ?p ?o .
  }}
}}"""


def _number(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


async def load_endpoints(
    client: FusekiClient, conn: Conn, *, graph: str | None = None
) -> EndpointLoadResult:
    """Every registered endpoint. An unreachable registry yields an empty result
    with a warning, as the other registries do."""
    registry = graph or conn.graph("agents")
    try:
        rows = (await client.select(conn, _endpoints_query(registry)))["results"]["bindings"]
    except (FusekiError, KeyError) as exc:
        message = f"agent-endpoint load from <{registry}> failed: {exc}"
        log.warning(message)
        return EndpointLoadResult(endpoints=[], warnings=[message])

    warnings: list[str] = []
    endpoints: list[Endpoint] = []
    for iri, props in collect(rows, "e").items():
        url = pick(props, _FIELDS["url"])
        if not url:
            warnings.append(f"<{iri}> has no endpoint URL; skipped")
            continue
        endpoint_id = pick(props, _FIELDS["id"]) or iri.rsplit(":", 1)[-1]
        scheme = (pick(props, _FIELDS["auth_scheme"]) or "none").lower()
        if scheme not in AUTH_SCHEMES:
            warnings.append(f"endpoint {endpoint_id!r} has unknown authScheme {scheme!r}; skipped")
            continue
        review_raw = pick(props, _FIELDS["call_review"])
        endpoints.append(
            Endpoint(
                id=endpoint_id,
                iri=iri,
                url=url,
                status=pick(props, _FIELDS["status"]) or "Active",
                label=pick(props, _FIELDS["label"]) or endpoint_id,
                description=pick(props, _FIELDS["description"]) or "",
                auth_scheme=scheme,
                auth_header=pick(props, _FIELDS["auth_header"]) or "",
                credential_ref=pick(props, _FIELDS["credential_ref"]) or "",
                signing_ref=pick(props, _FIELDS["signing_ref"]) or "",
                timeout_seconds=clamp(
                    _number(pick(props, _FIELDS["timeout"]), DEFAULT_TIMEOUT_SECONDS),
                    1.0, MAX_TIMEOUT_SECONDS,
                ),
                max_response_bytes=int(clamp(
                    _number(pick(props, _FIELDS["max_response"]), DEFAULT_MAX_RESPONSE_BYTES),
                    1_024, HARD_MAX_RESPONSE_BYTES,
                )),
                max_request_bytes=int(clamp(
                    _number(pick(props, _FIELDS["max_request"]), DEFAULT_MAX_REQUEST_BYTES),
                    1_024, HARD_MAX_REQUEST_BYTES,
                )),
                # Absent means review is required: the safe reading.
                call_review=truthy(review_raw) if review_raw is not None else True,
                registered_by=pick(props, _FIELDS["registered_by"]) or "",
            )
        )
    endpoints.sort(key=lambda e: e.id)
    return EndpointLoadResult(endpoints=endpoints, warnings=warnings)


# --- writing ----------------------------------------------------------------------
#
# registration_update / deletion_update are the same three lines as in
# trigger_registry.py. They are repeated rather than imported because that
# module imports triggers.py, and triggers.py is going to import this package.


def registration_update(registry: str, node: str, turtle: str) -> str:
    """Replace everything stored about ``node`` with ``turtle``, in one request."""
    return f"""DELETE WHERE {{ GRAPH <{registry}> {{ <{node}> ?p ?o }} }} ;
INSERT DATA {{ GRAPH <{registry}> {{
{turtle}
}} }}"""


def deletion_update(registry: str, node: str) -> str:
    return f"DELETE WHERE {{ GRAPH <{registry}> {{ <{node}> ?p ?o }} }}"


def validate_header_name(name: str) -> str:
    if not _HEADER_NAME.match(name or ""):
        raise InvalidEndpoint(f"authHeader {name!r} is not a valid header name")
    if name.lower() in _FORBIDDEN_HEADERS:
        raise InvalidEndpoint(
            f"authHeader {name!r} is reserved: the bridge sets that header itself"
        )
    return name


def render_endpoint_turtle(
    *,
    node: str,
    endpoint_id: str,
    url: str,
    status: str,
    label: str | None,
    description: str | None,
    auth_scheme: str,
    auth_header: str | None,
    credential_ref: str | None,
    signing_ref: str | None,
    timeout_seconds: float,
    max_response_bytes: int,
    max_request_bytes: int,
    call_review: bool,
    registered_at: str,
    registered_by: str | None,
) -> str:
    if status not in ENDPOINT_STATUSES:
        raise InvalidEndpoint(f"status must be one of {', '.join(ENDPOINT_STATUSES)}")
    if auth_scheme not in AUTH_SCHEMES:
        raise InvalidEndpoint(f"authScheme must be one of {', '.join(AUTH_SCHEMES)}")
    if auth_scheme == "header":
        validate_header_name(auth_header or "")
    elif auth_header:
        raise InvalidEndpoint("authHeader is only meaningful with authScheme 'header'")
    if auth_scheme != "none" and not credential_ref:
        raise InvalidEndpoint(f"authScheme {auth_scheme!r} needs a credentialRef")
    if auth_scheme == "none" and credential_ref:
        raise InvalidEndpoint("a credentialRef was given but authScheme is 'none'")
    if registered_by is not None and not _SAFE_IRI.match(registered_by):
        raise InvalidEndpoint(f"registered_by {registered_by!r} is not a usable absolute IRI")

    boolean = f"<{_XSD}boolean>"
    integer = f"<{_XSD}integer>"
    decimal = f"<{_XSD}decimal>"
    pairs = [
        f"a <{HB}{ENDPOINT_CLASS_SUFFIX}>",
        f"<{HB}id> {literal(endpoint_id)}",
        f"<{HB}endpoint> {literal(url)}",
        f"<{HB}agentStatus> {literal(status)}",
        f"<{HB}authScheme> {literal(auth_scheme)}",
        f"<{HB}timeoutSeconds> {literal(repr(float(timeout_seconds)), datatype=decimal)}",
        f"<{HB}maxResponseBytes> {literal(str(int(max_response_bytes)), datatype=integer)}",
        f"<{HB}maxRequestBytes> {literal(str(int(max_request_bytes)), datatype=integer)}",
        f"<{HB}callReview> {literal('true' if call_review else 'false', datatype=boolean)}",
    ]
    if label:
        pairs.append(f"<{HB}label> {literal(label)}")
    if description:
        pairs.append(f"<{HB}description> {literal(description)}")
    if auth_header:
        pairs.append(f"<{HB}authHeader> {literal(auth_header)}")
    if credential_ref:
        pairs.append(f"<{HB}credentialRef> {literal(credential_ref)}")
    if signing_ref:
        pairs.append(f"<{HB}signingRef> {literal(signing_ref)}")
    if registered_by:
        pairs.append(f"<{HB}registeredBy> <{registered_by}>")
    pairs.append(f"<{HB}registeredAt> {literal(registered_at, datatype=f'<{_XSD}dateTime>')}")
    return f"<{node}>\n    " + " ;\n    ".join(pairs) + " ."


def status_update(registry: str, node: str, status: str, *, registered_by: str | None = None) -> str:
    """Set the status. Re-activating transfers responsibility to the activator,
    as for triggers."""
    if status not in ENDPOINT_STATUSES:
        raise InvalidEndpoint(f"status must be one of {', '.join(ENDPOINT_STATUSES)}")
    if registered_by is not None and not _SAFE_IRI.match(registered_by):
        raise InvalidEndpoint(f"registered_by {registered_by!r} is not a usable absolute IRI")
    swap = ' || STRENDS(STR(?p), "registeredBy")' if registered_by else ""
    new_owner = f"<{node}> <{HB}registeredBy> <{registered_by}> ." if registered_by else ""
    return f"""DELETE {{ GRAPH <{registry}> {{ <{node}> ?p ?o }} }}
WHERE {{ GRAPH <{registry}> {{
  <{node}> ?p ?o .
  FILTER( STRENDS(STR(?p), "agentStatus") || STRENDS(STR(?p), "/status") || STRENDS(STR(?p), "#status"){swap} )
}} }} ;
INSERT DATA {{ GRAPH <{registry}> {{
  <{node}> <{HB}agentStatus> {literal(status)} .
  {new_owner}
}} }}"""
