"""Claiming an unprovisioned dataset: the first owner, in-band.

A dataset that exists in Fuseki but has never been provisioned has no Person
who could authorise anything, so every ``AnimusDep``-gated route answers 401.
``holonbridge-bootstrap-admin`` (``bootstrap.py``) is the operator-side way in
and stays the trust anchor. This module is the in-band equivalent for an
operator who has said, in configuration, which identities may use it.

What it will and will not do
----------------------------

* It acts only for a caller whose identity is **established**: an
  identity-bound token (the credential is the identity), or a proxy token
  whose asserted identity the trusted proxy has already verified. A legacy
  shared token or an open (no-token) deployment is never eligible -- there the
  "identity" is a header anyone can write.
* The established identity must also be on ``DATASET_CLAIMANTS``. Being
  authenticated is not enough; the operator chooses who may claim.
* It requires the dataset to hold **no owner and no admin**. It cannot be
  replayed against a configured dataset, and a legacy dataset that already has
  admins is claimed from the operator side instead.
* The check and the grant are one SPARQL Update request, so two concurrent
  claims cannot both succeed.
* The claimer is the *caller*. There is no parameter that names anyone else.
* It grants ``owner``, never a bypass beyond what owner already means.

What it deliberately does not do: create the dataset (that stays outside the
bridge), seed the scene/events/sequences graphs (they are created on first
write, which is safe now that an unconditional transition no longer depends on
the scene graph existing), or install shapes (gating is a per-dataset choice).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .acl import _admin_role, _owner_role
from .config import Settings
from .conn import Conn
from .fuseki import FusekiClient
from .turtle import escape_literal

HOLON = "https://w3id.org/holon/"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"

QueryFn = Callable[[str], Awaitable[dict[str, Any]]]

_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


class ClaimError(Exception):
    """A claim that cannot proceed, with the HTTP status the route should use."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Claimant:
    """An established, allowlisted identity -- who a claim would be made for."""

    external_id: str
    external_id_type: str


def claimant(
    settings: Settings,
    token_auth: Any,
    *,
    header_id: str | None,
    header_type: str | None,
    default_type: str,
) -> Claimant | None:
    """The caller as a :class:`Claimant`, or ``None`` if they are not eligible.

    ``token_auth`` is duck-typed (``.kind``, ``.identity``) so this module does
    not import ``deps`` (which imports it). Eligibility needs both halves: the
    identity is established, and the operator has listed it.
    """
    kind = getattr(token_auth, "kind", None)
    if kind == "identity":
        external_id, external_type = getattr(token_auth, "identity", None), default_type
    elif kind == "proxy":
        external_id, external_type = header_id, header_type or default_type
    else:  # legacy shared token, open deployment, or anything unrecognised
        return None
    if not external_id or external_id not in settings.dataset_claimants:
        return None
    if not _TYPE_NAME.match(external_type):
        return None
    return Claimant(external_id, external_type)


def _unclaimed_pattern(holons_graph: str) -> str:
    return (
        f"GRAPH <{holons_graph}> {{ ?anyone holon:hasRole ?anyRole . "
        f"FILTER (?anyRole IN (<{_admin_role(holons_graph)}>, <{_owner_role(holons_graph)}>)) }}"
    )


async def is_unclaimed(query_fn: QueryFn, holons_graph: str) -> bool:
    """True when nobody holds the owner or admin Role on this dataset."""
    query = f"PREFIX holon: <{HOLON}>\nASK {{ {_unclaimed_pattern(holons_graph)} }}"
    return not (await query_fn(query)).get("boolean")


def home_iri(holons_graph: str) -> str:
    suffix = ":holons"
    if not holons_graph.endswith(suffix):
        raise ValueError(f"expected holons_graph to end with {suffix!r}, got {holons_graph!r}")
    return holons_graph[: -len(suffix)] + ":home"


def default_slug(external_id: str) -> str:
    slug = _SLUG_UNSAFE.sub("-", external_id.lower()).strip("-")
    return slug or "owner"


async def _existing_person(query_fn: QueryFn, conn: Conn, external_id: str) -> str | None:
    query = f"""PREFIX holon: <{HOLON}>
SELECT ?person WHERE {{
  GRAPH <{conn.holons_graph}> {{
    ?person holon:hasExternalIdentity ?identity .
    ?identity holon:identifier "{escape_literal(external_id)}" .
  }}
}} LIMIT 1"""
    bindings = (await query_fn(query)).get("results", {}).get("bindings", [])
    return bindings[0]["person"]["value"] if bindings else None


def build_claim_update(
    conn: Conn,
    who: Claimant,
    *,
    person: str,
    identity: str | None,
    full_name: str,
) -> str:
    """The whole claim as ONE SPARQL Update request.

    Two operations, each guarded by the same "no owner, no admin" pattern.
    Jena runs a request in one write transaction, so the second operation sees
    the first's effects: the Home goes first, the owner grant last, and a
    concurrent claim that lost finds the guard already false and writes neither.
    """
    holons = conn.holons_graph
    owner = _owner_role(holons)
    home = home_iri(holons)
    guard = _unclaimed_pattern(holons)
    esc_id, esc_name = escape_literal(who.external_id), escape_literal(full_name)

    person_triples = (
        f"<{person}> a holon:Person ;\n"
        f'      rdfs:label "{esc_name}" ;\n'
        f"      holon:hasExternalIdentity <{identity}> ;\n"
        f"      holon:hasRole <{owner}> .\n"
        f"    <{identity}> a holon:{who.external_id_type} ;\n"
        f'      holon:identifier "{esc_id}" .'
        if identity
        else f"<{person}> holon:hasRole <{owner}> ."
    )
    return f"""PREFIX holon: <{HOLON}>
PREFIX rdfs: <{RDFS}>
INSERT {{
  GRAPH <{holons}> {{ <{home}> a holon:Home ; rdfs:label "Home" . }}
}}
WHERE {{
  FILTER NOT EXISTS {{ {guard} }}
  FILTER NOT EXISTS {{ GRAPH <{holons}> {{ ?existingHome a holon:Home }} }}
}}
;
INSERT {{
  GRAPH <{holons}> {{
    <{owner}> a holon:Role ; rdfs:label "Owner" .
    {person_triples}
  }}
}}
WHERE {{
  FILTER NOT EXISTS {{ {guard} }}
}}"""


async def claim(
    client: FusekiClient,
    conn: Conn,
    who: Claimant,
    *,
    slug: str | None = None,
    full_name: str | None = None,
) -> dict[str, Any]:
    """Make ``who`` the owner of an unclaimed dataset. Raises :class:`ClaimError`."""

    async def query_fn(q: str) -> dict[str, Any]:
        return await client.select(conn, q)

    if not await is_unclaimed(query_fn, conn.holons_graph):
        raise ClaimError(
            409,
            "dataset_already_claimed",
            f"dataset {conn.dataset!r} already has an owner or admin; "
            "initialise only applies to an unclaimed dataset",
        )

    existing = await _existing_person(query_fn, conn, who.external_id)
    if existing:
        person, identity = existing, None
    else:
        try:
            person = conn.person_iri(slug or default_slug(who.external_id))
        except ValueError as exc:
            raise ClaimError(400, "bad_slug", str(exc)) from exc
        id_slug = who.external_id_type.removesuffix("Identity").lower() or "identity"
        identity = f"{person}:identity:{id_slug}"

    await client.update(
        conn,
        build_claim_update(conn, who, person=person, identity=identity, full_name=full_name or who.external_id),
    )

    # The update is guarded, so a lost race writes nothing: ask what is true now.
    holds = await query_fn(
        f"PREFIX holon: <{HOLON}>\nASK {{ GRAPH <{conn.holons_graph}> "
        f"{{ <{person}> holon:hasRole <{_owner_role(conn.holons_graph)}> . }} }}"
    )
    if not holds.get("boolean"):
        raise ClaimError(
            409,
            "dataset_already_claimed",
            f"dataset {conn.dataset!r} was claimed by someone else first",
        )

    return {
        "ok": True,
        "claimed": True,
        "person": person,
        "role": _owner_role(conn.holons_graph),
        "home": home_iri(conn.holons_graph),
        "existing_person": existing is not None,
    }
