"""Per-fluent write grants: let one Person write named fluents, and nothing else.

Why (context for anyone opening this cold): ``POST /fluent/update`` needs a
``holon:grantsWrite`` on three whole graphs (events, scene, sequences). That
is all-or-nothing: someone who can write one process's fluents can write
every fluent on the dataset, and through the generic graph routes much more.
A shared process needs a participant who may write only that process's
fluents.

A ``holon:FluentGrant`` is a stored, revocable record in the ``holons`` graph:

    <grant> a holon:FluentGrant ;
        holon:grantee <person> ;
        holon:coversFluent <fluent-iri> ;            # zero or more
        holon:coversFluentPrefix "urn:proc:x:" ;     # zero or more
        holon:grantedBy <admin> ; holon:grantedAt "..."^^xsd:dateTime ;
        holon:validUntil "..."^^xsd:dateTime .       # optional

The fluent routes accept a write when the caller holds the three graph grants
(unchanged) OR a live FluentGrant covers every fluent in the request. A
FluentGrant is honoured only by the fluent routes, which confine the write to
fluent mechanics (portal locks, lifecycle and shape checks still apply). It
gives no access to any graph route. Revocation keeps the record
(``holon:revokedAt``); it is never deleted.

No import of FastAPI or ``acl``: it builds SPARQL and Turtle and reads
SPARQL-JSON, so it tests against an in-memory graph.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable

from .delegation import DelegationError, _bindings, _iso, _literal, _parse_dt, _run, safe_iri

HOLON = "https://w3id.org/holon/"
XSD = "http://www.w3.org/2001/XMLSchema#"
QueryFn = Callable[[str], "dict | Awaitable[dict]"]

#: A prefix must be specific enough that it cannot cover a whole namespace by accident.
MIN_PREFIX_LENGTH = 8
MAX_ENTRIES = 50


# DelegationError is re-exported: safe_iri raises it, and routes catch it for both.
class FluentGrantError(DelegationError):
    """A grant request that cannot be honoured; message is user-facing."""


def grant_iri(holons_graph: str, slug: str) -> str:
    suffix = ":holons"
    if not holons_graph.endswith(suffix):
        raise ValueError(f"expected holons_graph to end with {suffix!r}, got {holons_graph!r}")
    if not slug or any(ch in slug for ch in '<> "{}|^`:'):
        raise ValueError(f"{slug!r} cannot be used in a FluentGrant IRI")
    return f"{holons_graph[: -len(suffix)]}:fluentgrant:{slug}"


def new_slug() -> str:
    return uuid.uuid4().hex[:12]


def validate(*, fluents: Iterable[str], prefixes: Iterable[str]) -> tuple[list[str], list[str]]:
    """Cleaned (fluents, prefixes); raises :class:`FluentGrantError`."""
    fl = [safe_iri(f) for f in fluents]
    px = [safe_iri(p) for p in prefixes]
    if not fl and not px:
        raise FluentGrantError("name at least one fluent or one fluent prefix")
    if len(fl) + len(px) > MAX_ENTRIES:
        raise FluentGrantError(f"at most {MAX_ENTRIES} fluents and prefixes per grant")
    for p in px:
        if len(p) < MIN_PREFIX_LENGTH or ":" not in p:
            raise FluentGrantError(f"prefix {p!r} is too broad; it must name a specific namespace")
    return fl, px


def build_grant(
    *, iri: str, grantee: str, granted_by: str, fluents: list[str], prefixes: list[str],
    granted_at: datetime, valid_until: datetime | None, note: str | None = None,
) -> str:
    lines = [
        f"@prefix holon: <{HOLON}> .", f"@prefix xsd: <{XSD}> .",
        f"@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        f"<{iri}> a holon:FluentGrant ;",
        f"    holon:grantee <{grantee}> ;",
        f"    holon:grantedBy <{granted_by}> ;",
        f'    holon:grantedAt "{_iso(granted_at)}"^^xsd:dateTime ;',
    ]
    lines += [f"    holon:coversFluent <{f}> ;" for f in fluents]
    lines += [f"    holon:coversFluentPrefix {_literal(p)} ;" for p in prefixes]
    if valid_until:
        lines.append(f'    holon:validUntil "{_iso(valid_until)}"^^xsd:dateTime ;')
    if note:
        lines.append(f"    rdfs:comment {_literal(note)} ;")
    lines[-1] = lines[-1][:-1] + "."
    return "\n".join(lines) + "\n"


def revoke_update(holons_graph: str, iri: str, when: datetime) -> str:
    return (
        f"PREFIX holon: <{HOLON}> PREFIX xsd: <{XSD}>\n"
        f'INSERT DATA {{ GRAPH <{holons_graph}> {{ <{iri}> holon:revokedAt "{_iso(when)}"^^xsd:dateTime }} }}'
    )


async def list_grants(query_fn: QueryFn, holons_graph: str, *, grantee: str | None = None) -> list[dict]:
    who = f"<{grantee}>" if grantee else "?grantee"
    result = await _run(query_fn, f"""PREFIX holon: <{HOLON}>
SELECT ?grant ?grantee ?fluent ?prefix ?until ?revoked WHERE {{
  GRAPH <{holons_graph}> {{
    ?grant a holon:FluentGrant ; holon:grantee {who} .
    BIND({who} AS ?grantee)
    OPTIONAL {{ ?grant holon:coversFluent ?fluent }}
    OPTIONAL {{ ?grant holon:coversFluentPrefix ?prefix }}
    OPTIONAL {{ ?grant holon:validUntil ?until }}
    OPTIONAL {{ ?grant holon:revokedAt ?revoked }}
  }}
}}""")
    grants: dict[str, dict] = {}
    for b in _bindings(result):
        g = grants.setdefault(b["grant"]["value"], {
            "grant": b["grant"]["value"], "grantee": b.get("grantee", {}).get("value", grantee),
            "fluents": set(), "prefixes": set(), "until": None, "revoked": False})
        if "fluent" in b:
            g["fluents"].add(b["fluent"]["value"])
        if "prefix" in b:
            g["prefixes"].add(b["prefix"]["value"])
        if "until" in b:
            g["until"] = b["until"]["value"]
        if "revoked" in b:
            g["revoked"] = True
    return list(grants.values())


def _live(g: dict, now: datetime) -> bool:
    if g["revoked"]:
        return False
    if g["until"] is not None:
        end = _parse_dt(g["until"])
        if end is None or end <= now:  # unparseable bound: fail closed
            return False
    return True


def _covers(g: dict, fluent: str) -> bool:
    return fluent in g["fluents"] or any(fluent.startswith(p) for p in g["prefixes"])


async def find_cover(
    query_fn: QueryFn, holons_graph: str, *, person: str, fluents: Iterable[str],
    now: datetime | None = None,
) -> dict[str, str] | None:
    """{fluent: grant IRI} if a live grant covers EVERY fluent, else None."""
    now = now or datetime.now(timezone.utc)
    live = [g for g in await list_grants(query_fn, holons_graph, grantee=person) if _live(g, now)]
    out: dict[str, str] = {}
    for f in fluents:
        match = next((g for g in live if _covers(g, f)), None)
        if match is None:
            return None
        out[f] = match["grant"]
    return out or None
