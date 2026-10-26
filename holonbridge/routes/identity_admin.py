"""Admin identity routes: creating a Person, assigning a Persona,
admin-only session impersonation for testing, and (CHANGED 2026-09-18)
standalone Role/write-grant management plus Person deactivation.

Every endpoint is admin-only (is_admin bypass -- the same break-glass
capability acl.py already defines), distinct from persona.py's
switch_persona, which is self-service and membership-gated. Creating a
new Person or minting someone else's Home is never something a caller
does to themselves the first time -- there is no bootstrap path around
that, matching how every Person creation so far in this codebase (the
8/17 sweep, the same-day persona mint on 2026-08-25) has been an admin
operation performed on someone's behalf, not a self-registration flow.

Person metadata beyond the identity-resolution essentials (full name,
external identity, role) is deliberately open-ended: ``metadata`` on
CreatePersonRequest accepts any {local_predicate: string_value} pairs
and writes each as holon:{local_predicate}, no fixed schema. The
companion shape (shapes/identity.ttl) does not set sh:closed, for the
same reason -- new metadata (organisation, location, whatever comes up)
should never need a shape or endpoint change just to start being
*recorded*, only a shape change if it should start being *validated*.
Neither route here validates against that shape by default -- pass
``shapes_graph`` yourself via push_turtle/ingest, or register it as the
dataset's own ``conn.shapes_graph`` if you want it enforced on every
write, dataset-wide, the same way fluent.ttl's shapes are meant to be
deployed.

CHANGED 2026-09-01: added ``act_as``/``cease_acting_as`` (see
``acting_as.py`` and ``deps.py``'s ``require_animus``). ``_require_admin``
below now checks ``animus.real_person`` rather than ``animus.person`` --
the two are equal for every existing caller (nothing before this ever
populated ``real_person`` differently), so this is a no-op change for
every route already using it, but it's the specific thing that keeps
these four admin routes reachable by their real admin caller even while
that caller is currently acting as someone else. Checking ``.person``
instead would make ``cease_acting_as`` uncallable the moment it was
needed: the very first thing ``require_animus`` would resolve the caller
as, mid-impersonation, is the target -- who by design isn't an admin.

CHANGED 2026-09-18: create_person and assign_persona were the only way to
*grant* anything, and there was no way to revoke, audit, or remove access
at all short of hand-written SPARQL. This adds the missing symmetric
half:

- ``list_persons`` -- the read side. Walks Person -> external identities
  -> Roles -> write/replace grants in one call, because an interface for
  granting access is only half the job if there's no interface for
  seeing what's already granted.
- ``grant_role``/``revoke_role`` -- standalone Role assignment, after the
  fact, for a Person create_person already minted. Both refuse to leave
  a dataset with zero admins (see ``_sole_remaining_admin``): acl.py's
  admin bypass has no ordinary-Role fallback under it, so a dataset with
  no admins left has no route back in through the REST API at all --
  only ``holonbridge-bootstrap-admin``, run by hand against Fuseki
  directly.
- ``grant_write``/``revoke_write`` -- the ``holon:grantsWrite``/
  ``holon:grantsReplace`` axis acl.py's ``check_write``/``check_replace``
  deliberately keep OUT of the Role/grants machinery (writes are never
  role-based, on Kurt's 2026-08-17 framing) -- this was the only axis
  create_person's own ``role`` field couldn't touch at all, at any point.
- ``deactivate_person``/``reactivate_person`` -- "remove a user" as
  retraction, not deletion. A Person referenced from provenance (a past
  act_as's real_person, a scheduler firing, an authored holon) is never
  left dangling; see ``deactivate_person``'s own docstring for the full
  shape, and ``acl.py``'s ``resolve_person`` for the identity-resolution
  half that actually locks the door.

``revoke_role``/``revoke_write`` return ``{"ok": False, "note": ...}``
rather than a 4xx when the thing being revoked was never held -- the same
convention ``assign_persona`` already uses for "no such Persona"/"no
Home here": an ordinary, expected outcome, not an error.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from .. import delegation as dg
from ..acl import is_admin, is_owner, owner_exists
from ..conn import Conn
from ..deps import ActingAsDep, AnimusDep, ClientDep, ConnDep
from ..fuseki import FusekiError
from ..persona import persona_exists

router = APIRouter(prefix="/admin", tags=["admin"])

HOLON = "https://w3id.org/holon/"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"

_METADATA_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

#: The two write axes acl.py's check_write/check_replace gate on -- see
#: their own docstrings for why holding one never implies the other.
_GRANT_PREDICATE = {"write": "grantsWrite", "replace": "grantsReplace"}


def _literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _resolve_person(conn: Conn, value: str) -> str:
    """A Person IRI from either a short local slug or a full IRI already
    presented as one. The exact disambiguation ``act_as``'s ``target``
    field used inline before this existed -- centralised here now that
    role/write grant management needs the identical rule in four more
    places."""
    return value if value.startswith("http") or ":" in value else conn.person_iri(value)


async def _require_admin(conn: ConnDep, client: ClientDep, animus: AnimusDep) -> None:
    """Checks the REAL caller's admin role (``animus.real_person``), never
    the currently-active identity (``animus.person``) -- see the module
    docstring's 2026-09-01 note for why that distinction matters here
    specifically."""

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    if not await is_admin(query_fn, conn.holons_graph, person=animus.real_person):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="admin role required on this dataset"
        )


async def _require_owner_for(
    conn: ConnDep, client: ClientDep, animus: AnimusDep, *, role_iris
) -> None:
    """Granting, revoking or stripping ``admin`` / ``owner`` is an owner's act.

    The hierarchy, once a dataset has an owner: an owner may create and remove
    admins and other owners; an admin may not touch either, so an admin can
    never promote themselves or a friend, or remove the owner. Everything else
    an admin does is unchanged.

    Two rules keep existing datasets working. (1) A dataset with NO owner keeps
    today's flat behaviour for ``admin`` -- admins may still manage admins --
    until an owner is established from the operator side
    (``holonbridge-bootstrap-admin --role owner``). (2) ``owner`` itself is
    never grantable by a non-owner, ever: with no owner there is no route that
    mints one through the REST API, by design.

    Checks the REAL caller (``animus.real_person``), like ``_require_admin``, so
    an admin cannot climb by ``act_as``-ing the owner.
    """
    admin_iri, owner_iri = conn.role_iri("admin"), conn.role_iri("owner")
    touched = {r for r in role_iris if r in (admin_iri, owner_iri)}
    if not touched:
        return

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    if await is_owner(query_fn, conn.holons_graph, person=animus.real_person):
        return
    if owner_iri not in touched and not await owner_exists(query_fn, conn.holons_graph):
        return  # no owner yet: the flat admin model still applies
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail="owner role required to grant or revoke admin or owner on this dataset",
    )


async def _require_person_exists(query_fn, holons_graph: str, *, person: str) -> None:
    """404s if ``person`` is not a known holon:Person -- the same guard
    ``act_as`` already applied inline before this existed, now shared by
    every route below that names an existing Person rather than minting
    one. Does not check ``holon:status`` -- a deactivated Person still
    exists and is a valid target for, say, re-granting a Role ahead of
    reactivation; only identity resolution (acl.py's resolve_person) and
    ``act_as`` itself treat inactive as absent."""
    query = f"""PREFIX holon: <{HOLON}>
ASK {{ GRAPH <{holons_graph}> {{ <{person}> a holon:Person . }} }}"""
    result = await query_fn(query)
    if not result.get("boolean"):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"no holon:Person found: {person}"
        )


async def _person_roles(query_fn, conn: Conn, *, person: str) -> list[dict]:
    """Every Role ``person`` currently holds, with a label where one
    exists. Shared by ``list_persons`` (read) and ``deactivate_person``
    (which needs the exact set to retract)."""
    query = f"""PREFIX holon: <{HOLON}>
PREFIX rdfs: <{RDFS}>
SELECT ?role ?label WHERE {{
  GRAPH <{conn.holons_graph}> {{
    <{person}> holon:hasRole ?role .
    OPTIONAL {{ ?role rdfs:label ?label . }}
  }}
}} ORDER BY ?role"""
    rows = (await query_fn(query)).get("results", {}).get("bindings", [])
    return [{"role": r["role"]["value"], "label": r.get("label", {}).get("value")} for r in rows]


async def _person_identities(query_fn, conn: Conn, *, person: str) -> list[dict]:
    """Every external identity linked to ``person`` -- type and
    identifier, e.g. a GitHub login."""
    query = f"""PREFIX holon: <{HOLON}>
SELECT ?identity ?type ?identifier WHERE {{
  GRAPH <{conn.holons_graph}> {{
    <{person}> holon:hasExternalIdentity ?identity .
    ?identity a ?type ; holon:identifier ?identifier .
  }}
}} ORDER BY ?identity"""
    rows = (await query_fn(query)).get("results", {}).get("bindings", [])
    return [
        {
            "identity": r["identity"]["value"],
            "type": r["type"]["value"].rsplit("/", 1)[-1],
            "identifier": r["identifier"]["value"],
        }
        for r in rows
    ]


async def _person_write_grants(query_fn, conn: Conn, *, person: str) -> list[dict]:
    """Every (target, mode) pair currently granting ``person`` write or
    replace access -- the reverse lookup neither check_write nor
    check_replace themselves need (they check one target at a time), but
    an access review or a deactivation both do. Always lives in
    holons_graph regardless of what ``target`` names, mirroring exactly
    where acl.py's check_write/check_replace look for the triple."""
    query = f"""PREFIX holon: <{HOLON}>
SELECT ?target ?mode WHERE {{
  GRAPH <{conn.holons_graph}> {{
    {{ ?target holon:grantsWrite <{person}> . BIND("write" AS ?mode) }}
    UNION
    {{ ?target holon:grantsReplace <{person}> . BIND("replace" AS ?mode) }}
  }}
}} ORDER BY ?target ?mode"""
    rows = (await query_fn(query)).get("results", {}).get("bindings", [])
    return [{"target": r["target"]["value"], "mode": r["mode"]["value"]} for r in rows]


async def _sole_remaining_admin(query_fn, conn: Conn, *, exclude: str) -> bool:
    """Whether ``exclude`` is the only Person who can still act as an admin on
    this dataset -- holding the admin Role or the owner Role, which implies it.
    The guard ``revoke_role`` and ``deactivate_person`` both need before
    removing someone's standing, since acl.py's admin bypass has no
    ordinary-Role fallback beneath it."""
    query = f"""PREFIX holon: <{HOLON}>
ASK {{
  GRAPH <{conn.holons_graph}> {{
    ?other holon:hasRole ?role .
    FILTER (?role IN (<{conn.role_iri("admin")}>, <{conn.role_iri("owner")}>))
    FILTER(?other != <{exclude}>)
  }}
}}"""
    result = await query_fn(query)
    return not result.get("boolean")


async def _admin_capacity_survives(query_fn, conn: Conn, *, person: str, removing: str) -> bool:
    """Whether someone can still act as an admin once the single grant
    (``person`` holds ``removing``) is gone. Finer than
    ``_sole_remaining_admin``: an owner who also holds admin keeps admin
    capacity when only the admin grant is revoked."""
    query = f"""PREFIX holon: <{HOLON}>
ASK {{
  GRAPH <{conn.holons_graph}> {{
    ?holder holon:hasRole ?role .
    FILTER (?role IN (<{conn.role_iri("admin")}>, <{conn.role_iri("owner")}>))
    FILTER (?holder != <{person}> || ?role != <{removing}>)
  }}
}}"""
    return bool((await query_fn(query)).get("boolean"))


async def _sole_remaining_owner(query_fn, conn: Conn, *, exclude: str) -> bool:
    """Whether ``exclude`` is the only Person holding the owner Role. A dataset
    that has had an owner never drops to none through the REST API -- ownership
    moves by granting a second owner and then revoking the first."""
    query = f"""PREFIX holon: <{HOLON}>
ASK {{
  GRAPH <{conn.holons_graph}> {{
    ?other holon:hasRole <{conn.role_iri("owner")}> .
    FILTER(?other != <{exclude}>)
  }}
}}"""
    result = await query_fn(query)
    return not result.get("boolean")


class CreatePersonRequest(BaseModel):
    slug: str = Field(
        ..., min_length=1,
        description="Short local identifier, e.g. 'kurt'. Becomes the trailing "
        "segment of the Person IRI and every graph derived from it.",
    )
    full_name: str = Field(..., min_length=1, description="rdfs:label -- display name.")
    external_id: str = Field(..., min_length=1, description="e.g. a GitHub login.")
    external_id_type: str = Field(
        default="GitHubIdentity",
        description="holon: class of the external identity, expected to be a "
        "subclass of holon:ExternalIdentity (see shapes/identity.ttl). "
        "GitHubIdentity is the only one require_animus resolves against "
        "today, but nothing here hard-codes that.",
    )
    role: str | None = Field(
        default=None,
        description="Local role slug to grant immediately, e.g. 'admin'. "
        "Omit to create the Person with no role.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description='Open-ended additional attributes, e.g. {"organisation": '
        '"Semantical", "location": "Olympia, WA"}. Each key becomes holon:{key}; '
        "values are written as plain string literals. Not validated against "
        "any fixed schema today -- see module docstring.",
    )


@router.post("/person")
async def create_person(
    body: CreatePersonRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Create a Person, its external identity, and optionally grant a Role.

    Admin-only. Merges rather than duplicates on a repeat call for the
    same slug (GSP POST) -- matches how merge is the default write mode
    everywhere else in this codebase -- but does not check for a
    pre-existing Person first, so a second call with different
    full_name/metadata adds to what's there rather than replacing it.
    """
    await _require_admin(conn, client, animus)
    if body.role:
        await _require_owner_for(conn, client, animus, role_iris=[conn.role_iri(body.role)])

    for key in body.metadata:
        if not _METADATA_KEY.match(key):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"metadata key {key!r} is not a valid holon: local name",
            )

    person = conn.person_iri(body.slug)
    id_type_slug = body.external_id_type.removesuffix("Identity").lower() or "identity"
    identity = f"{person}:identity:{id_type_slug}"

    role_iri = conn.role_iri(body.role) if body.role else None
    role_clause = f" ;\n    holon:hasRole <{role_iri}>" if role_iri else ""

    lines = [
        f"PREFIX holon: <{HOLON}>",
        f"PREFIX rdfs: <{RDFS}>",
        "",
        f"<{person}> a holon:Person ;",
        f"    rdfs:label {_literal(body.full_name)} ;",
        f"    holon:hasExternalIdentity <{identity}>{role_clause} .",
        "",
        f"<{identity}> a holon:{body.external_id_type} ;",
        f"    holon:identifier {_literal(body.external_id)} .",
    ]
    if role_iri:
        lines += [
            "",
            f"<{role_iri}> a holon:Role ;",
            f'    rdfs:label {_literal(body.role.replace("-", " ").title())} .',
        ]
    for key, value in body.metadata.items():
        lines.append(f"<{person}> holon:{key} {_literal(value)} .")

    try:
        await client.post_graph(conn, conn.holons_graph, "\n".join(lines) + "\n")
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "person": person,
        "identity": identity,
        "role": role_iri,
        "metadata": body.metadata,
    }


class AssignPersonaRequest(BaseModel):
    person: str = Field(..., min_length=1, description="Full Person IRI.")
    persona: str = Field(..., min_length=1, description="Local persona name, e.g. 'assistant'.")
    label: str = Field(default="Home", description="rdfs:label for the Home holon.")


@router.post("/persona/assign")
async def assign_persona(
    body: AssignPersonaRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Mint a holon:Home for an existing Person under a named Persona --
    the write side of switch_persona's own membership gate (has_home).

    Admin-only, unlike switch_persona itself: a Person never grants
    themselves membership under a persona they don't already have a Home
    in -- someone with standing has to place them there first.
    """
    await _require_admin(conn, client, animus)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    try:
        exists = await persona_exists(query_fn, conn, persona=body.persona)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    if not exists:
        return {
            "ok": False,
            "requested": body.persona,
            "note": "not_found: no holon:Persona by that name in this dataset",
        }

    slug = conn.person_slug(body.person)
    graph_iri = conn.persona_user_graph(body.persona, "holons", slug)
    home = f"{graph_iri}#home"

    turtle = f"""PREFIX holon: <{HOLON}>
PREFIX rdfs: <{RDFS}>

<{home}> a holon:Home ;
    rdfs:label {_literal(body.label)} ;
    holon:representsPerson <{body.person}> ."""

    try:
        await client.post_graph(conn, graph_iri, turtle)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"ok": True, "home": home, "graph": graph_iri, "persona": body.persona}


class ActAsRequest(BaseModel):
    target: str = Field(
        ..., min_length=1,
        description="Person to impersonate: either a short local slug "
        "(e.g. 'jane-doe'), resolved via conn.person_iri() the same "
        "way create_person's own slug is, or a full Person IRI.",
    )


@router.post("/act-as")
async def act_as(
    body: ActAsRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    acting_as: ActingAsDep,
) -> dict:
    """Admin-only testing tool. Every subsequent request on this same
    credential resolves as ``target`` instead of the real caller, until
    ``cease_acting_as`` is called or 30 minutes pass unrenewed.

    The point is to exercise the real, non-bypassed ACL/grant-check code
    a genuine non-admin caller would hit -- ``target`` should essentially
    always be someone who does NOT hold the admin role, or this
    accomplishes nothing. Gated on the REAL caller's admin status
    (``animus.real_person``, re-checked on every request while the
    override is active -- see ``require_animus``), never on whatever
    identity happens to be active when this is called -- so it can never
    be chained through an existing act_as to reach a target the real
    caller couldn't reach directly.

    CHANGED 2026-09-18: refuses a ``target`` carrying
    ``holon:status "inactive"``. ``build_animus_as`` (unlike
    ``acl.py``'s ``resolve_person``) never consults ``holon:status`` --
    by its own docstring it isn't meant to, since the real caller's
    identity was already fully resolved before this route runs -- so
    without this check here, acting as a deactivated Person would
    silently succeed where that Person's own real login no longer would.
    Uses its own existence+status query rather than
    ``_require_person_exists`` for exactly that reason: this is the one
    caller that needs the stricter "exists AND active" question, not the
    plain existence one every other route below asks.
    """
    await _require_admin(conn, client, animus)

    target_iri = _resolve_person(conn, body.target)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    exists_query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{
      GRAPH <{conn.holons_graph}> {{
        <{target_iri}> a holon:Person .
        FILTER NOT EXISTS {{ <{target_iri}> holon:status "inactive" . }}
      }}
    }}
    """
    result = await query_fn(exists_query)
    if not result.get("boolean"):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"no active holon:Person found: {target_iri}",
        )

    acting_as.set(real_person=animus.real_person, target_person=target_iri)
    return {"ok": True, "acting_as": target_iri, "real_person": animus.real_person}


@router.post("/cease-acting-as")
async def cease_acting_as(
    conn: ConnDep, client: ClientDep, animus: AnimusDep, acting_as: ActingAsDep
) -> dict:
    """Clear any active act_as override for the real caller. Safe to call
    with none active -- always returns ``ok: True``."""
    await _require_admin(conn, client, animus)
    acting_as.clear(real_person=animus.real_person)
    return {"ok": True, "acting_as": None, "real_person": animus.real_person}


@router.get("/persons")
async def list_persons(conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    """Every Person known to this dataset, with their external identities,
    roles, status, and which targets currently grant them write or
    replace access. Answers "who can do what" in one call instead of
    requiring hand-written SPARQL against acl.py's own model -- granting
    access already had an interface (create_person, grant_role,
    grant_write below); auditing it didn't, and that was the actual gap.

    Admin-only, like every other route in this module. One query for the
    Person/label/status roster, then small follow-up queries per person
    for identities/roles/write-grants -- the same N-query shape
    routes/persona.py's own list_personas already uses for its own
    per-persona membership check, not a premature optimisation problem
    until a dataset has enough Persons for it to matter.
    """
    await _require_admin(conn, client, animus)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    roster_query = f"""PREFIX holon: <{HOLON}>
PREFIX rdfs: <{RDFS}>
SELECT ?person ?label ?status WHERE {{
  GRAPH <{conn.holons_graph}> {{
    ?person a holon:Person ;
            rdfs:label ?label .
    OPTIONAL {{ ?person holon:status ?status . }}
  }}
}} ORDER BY ?person"""

    try:
        rows = (await query_fn(roster_query)).get("results", {}).get("bindings", [])
        persons = []
        for row in rows:
            person = row["person"]["value"]
            persons.append(
                {
                    "person": person,
                    "label": row["label"]["value"],
                    "status": row.get("status", {}).get("value", "active"),
                    "external_identities": await _person_identities(
                        query_fn, conn, person=person
                    ),
                    "roles": await _person_roles(query_fn, conn, person=person),
                    "write_grants": await _person_write_grants(
                        query_fn, conn, person=person
                    ),
                }
            )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"dataset": conn.dataset, "count": len(persons), "persons": persons}


class RoleGrantRequest(BaseModel):
    person: str = Field(
        ..., min_length=1,
        description="Person to grant the role to: short local slug (e.g. 'jane-doe') "
        "or full Person IRI.",
    )
    role: str = Field(..., min_length=1, description="Local role slug, e.g. 'editor'.")
    label: str | None = Field(
        default=None,
        description="rdfs:label if this Role doesn't already exist. Defaults to a "
        "title-cased version of the slug, matching create_person's own convention.",
    )


@router.post("/role/grant")
async def grant_role(
    body: RoleGrantRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Grant an existing Person a Role, standalone -- the piece
    create_person's own inline ``role`` field couldn't cover: promoting
    (or adding a second role to) someone who already has a Person, after
    the fact, without hand-written SPARQL.

    Idempotent the same way create_person is: re-granting a Role the
    Person already holds is a silent no-op (GSP POST merge), not an
    error. Mints the Role resource itself (``a holon:Role ; rdfs:label``)
    if it doesn't already exist, exactly like create_person's own
    ``role`` field does -- same Turtle shape, so a Role minted here is
    indistinguishable from one minted at person-creation time.
    """
    await _require_admin(conn, client, animus)
    await _require_owner_for(conn, client, animus, role_iris=[conn.role_iri(body.role)])

    person_iri = _resolve_person(conn, body.person)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    await _require_person_exists(query_fn, conn.holons_graph, person=person_iri)

    role_iri = conn.role_iri(body.role)
    label = body.label or body.role.replace("-", " ").title()

    turtle = f"""PREFIX holon: <{HOLON}>
PREFIX rdfs: <{RDFS}>

<{role_iri}> a holon:Role ;
    rdfs:label {_literal(label)} .

<{person_iri}> holon:hasRole <{role_iri}> ."""

    try:
        await client.post_graph(conn, conn.holons_graph, turtle)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"ok": True, "person": person_iri, "role": role_iri}


class RoleRevokeRequest(BaseModel):
    person: str = Field(
        ..., min_length=1,
        description="Person to revoke the role from: short local slug or full Person IRI.",
    )
    role: str = Field(..., min_length=1, description="Local role slug, e.g. 'editor'.")


@router.post("/role/revoke")
async def revoke_role(
    body: RoleRevokeRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Retract a Role grant -- the write side ``list_persons`` existed to
    make visible in the first place. Removes only the ``holon:hasRole``
    edge, never the Role resource itself (another Person may still hold
    it) and never the Person -- see ``deactivate_person`` for removing a
    Person's access wholesale rather than one Role at a time.

    Refusing to revoke a dataset's only remaining admin is a deliberate
    guard, not an oversight: acl.py's admin bypass is a break-glass
    capability with no ordinary-Role fallback underneath it, so a dataset
    with zero admins left has no route back in through the REST API at
    all -- only ``holonbridge-bootstrap-admin``, run by hand against
    Fuseki directly, the same chicken-and-egg bypass bootstrap.py exists
    for. Refusing here is cheaper than needing that.
    """
    await _require_admin(conn, client, animus)
    await _require_owner_for(conn, client, animus, role_iris=[conn.role_iri(body.role)])

    person_iri = _resolve_person(conn, body.person)
    role_iri = conn.role_iri(body.role)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    await _require_person_exists(query_fn, conn.holons_graph, person=person_iri)

    holds_role = await query_fn(
        f"""PREFIX holon: <{HOLON}>
ASK {{ GRAPH <{conn.holons_graph}> {{ <{person_iri}> holon:hasRole <{role_iri}> . }} }}"""
    )
    if not holds_role.get("boolean"):
        return {
            "ok": False,
            "person": person_iri,
            "role": role_iri,
            "note": "not_found: this person does not currently hold that role",
        }

    if role_iri == conn.role_iri("owner") and await _sole_remaining_owner(
        query_fn, conn, exclude=person_iri
    ):
        return {
            "ok": False,
            "person": person_iri,
            "role": role_iri,
            "note": "refused: this is the only owner of this dataset -- grant owner to "
            "someone else first, then revoke this one",
        }
    if role_iri in (conn.role_iri("admin"), conn.role_iri("owner")) and not await _admin_capacity_survives(
        query_fn, conn, person=person_iri, removing=role_iri
    ):
        return {
            "ok": False,
            "person": person_iri,
            "role": role_iri,
            "note": "refused: this is the only Person holding the admin role on this "
            "dataset -- revoking it would leave no route back in through the REST API",
        }

    try:
        await client.update(
            conn,
            f"""PREFIX holon: <{HOLON}>
DELETE DATA {{ GRAPH <{conn.holons_graph}> {{ <{person_iri}> holon:hasRole <{role_iri}> . }} }}""",
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"ok": True, "person": person_iri, "role": role_iri, "revoked": True}


class WriteGrantRequest(BaseModel):
    person: str = Field(
        ..., min_length=1,
        description="Person to grant access to: short local slug or full Person IRI.",
    )
    target: str = Field(
        ..., min_length=1,
        description="Full IRI of the graph/resource this grant covers -- whatever "
        "check_write/check_replace's own `target` argument means for the write this "
        "is meant to authorise. Taken as-is; not resolved or validated against "
        "ground truth, since the target need not exist yet.",
    )
    mode: str = Field(
        default="write",
        description="'write' (holon:grantsWrite, additive/merge) or 'replace' "
        "(holon:grantsReplace, wholesale overwrite -- see acl.py's check_replace for "
        "why this is a deliberately separate, stricter grant that write never implies).",
    )

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, value: str) -> str:
        if value not in _GRANT_PREDICATE:
            raise ValueError(f"mode must be one of {sorted(_GRANT_PREDICATE)}")
        return value


@router.post("/write/grant")
async def grant_write(
    body: WriteGrantRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Grant a Person explicit write (or replace) access to one target --
    the one axis acl.py's check_write/check_replace deliberately keep OUT
    of the Role/grants machinery above: writes are never role-based, on
    Kurt's 2026-08-17 framing, so this is the only route that can create
    a grantsWrite/grantsReplace edge short of hand-written SPARQL.

    Idempotent: granting the same (person, target, mode) twice merges to
    the same single triple.
    """
    await _require_admin(conn, client, animus)

    person_iri = _resolve_person(conn, body.person)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    await _require_person_exists(query_fn, conn.holons_graph, person=person_iri)

    predicate = _GRANT_PREDICATE[body.mode]
    turtle = f"""PREFIX holon: <{HOLON}>

<{body.target}> holon:{predicate} <{person_iri}> ."""

    try:
        await client.post_graph(conn, conn.holons_graph, turtle)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"ok": True, "person": person_iri, "target": body.target, "mode": body.mode}


class WriteRevokeRequest(BaseModel):
    person: str = Field(
        ..., min_length=1,
        description="Person to revoke access from: short local slug or full Person IRI.",
    )
    target: str = Field(..., min_length=1, description="Same target IRI the grant used.")
    mode: str = Field(default="write", description="'write' or 'replace' -- must match "
        "the mode the grant was made under; the two are independent grants.")

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, value: str) -> str:
        if value not in _GRANT_PREDICATE:
            raise ValueError(f"mode must be one of {sorted(_GRANT_PREDICATE)}")
        return value


@router.post("/write/revoke")
async def revoke_write(
    body: WriteRevokeRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Retract a grantsWrite/grantsReplace edge. Same not-currently-held
    "not_found" convention as revoke_role -- this is an ordinary, expected
    outcome (the grant was already gone, or never existed under this
    exact target string), not something worth a 4xx for."""
    await _require_admin(conn, client, animus)

    person_iri = _resolve_person(conn, body.person)
    predicate = _GRANT_PREDICATE[body.mode]

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    await _require_person_exists(query_fn, conn.holons_graph, person=person_iri)

    holds_grant = await query_fn(
        f"""PREFIX holon: <{HOLON}>
ASK {{ GRAPH <{conn.holons_graph}> {{ <{body.target}> holon:{predicate} <{person_iri}> . }} }}"""
    )
    if not holds_grant.get("boolean"):
        return {
            "ok": False,
            "person": person_iri,
            "target": body.target,
            "mode": body.mode,
            "note": "not_found: no such grant currently exists",
        }

    try:
        await client.update(
            conn,
            f"""PREFIX holon: <{HOLON}>
DELETE DATA {{ GRAPH <{conn.holons_graph}> {{ <{body.target}> holon:{predicate} <{person_iri}> . }} }}""",
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "person": person_iri,
        "target": body.target,
        "mode": body.mode,
        "revoked": True,
    }


class DeactivatePersonRequest(BaseModel):
    person: str = Field(
        ..., min_length=1,
        description="Person to deactivate: short local slug or full Person IRI.",
    )


@router.post("/person/deactivate")
async def deactivate_person(
    body: DeactivatePersonRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Remove a Person's access without deleting the Person -- retraction,
    not deletion, is the answer to "remove a user" here, because a Person
    is referenced from provenance (a past act_as's real_person, a
    scheduler firing, an authored holon) that should never be left
    dangling just because someone's access ended.

    Three things happen, in one atomic SPARQL Update request (never
    several separate ``client.update()`` calls -- see the README's own
    account of the named-rule Replace-mode bug, where splitting one
    logical operation into two separate HTTP requests is exactly what let
    a concurrent execution interleave and lose data; every statement here
    goes out in a single semicolon-joined request instead, the same fix
    that bug landed on):

    1. Every ``holon:hasRole`` edge this Person holds is retracted, so
       acl.py's role-based ReadGrant/InvokeGrant checks stop passing
       immediately.
    2. Every ``holon:grantsWrite``/``holon:grantsReplace`` edge naming
       this Person as object is retracted too -- role revocation alone
       would leave these standing, since check_write/check_replace never
       consult holon:hasRole at all.
    3. ``holon:status "inactive"`` is asserted on the Person.

    Step 3 is what actually locks the door: acl.py's resolve_person
    refuses to resolve an inactive Person's identity at all, so a
    deactivated caller gets a 401 on their very next request, on ANY
    route, not just admin-gated ones -- steps 1 and 2 are
    belt-and-suspenders for anything (a currently-active act_as override,
    a future code path that reads the graph directly instead of going
    through identity resolution) that might still be consulting Roles or
    grants rather than resolved identity. The Person, their label, and
    their external identity all survive.

    CHANGED 2026-09-30: every still-live ``holon:Delegation`` this Person
    gave or received is revoked in that same request (stamped
    ``holon:revokedAt``, records retained), and the ids are returned as
    ``delegations_revoked``. ``delegation.find_delegation`` already
    ignores an inactive delegator's delegations and an inactive delegate
    cannot authenticate, so this too is belt-and-suspenders -- but it is
    what makes ``list_delegations`` tell the truth ("revoked", not
    "active" for someone locked out) and what stops a later
    ``reactivate_person`` from silently reviving delegations nobody
    re-approved.

    Two things this refuses to do, on purpose:

    - Deactivate the caller's own real identity (``animus.real_person``)
      -- self-locking an admin out mid-session, with no route back in but
      the direct-Fuseki bootstrap bypass, is not a mistake this should
      let happen silently.
    - Deactivate the dataset's last remaining admin -- the same guard
      ``revoke_role`` applies to a bare Role revocation, needed here too
      since deactivation revokes every Role including admin.
    """
    await _require_admin(conn, client, animus)

    person_iri = _resolve_person(conn, body.person)

    if person_iri == animus.real_person:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="refusing to deactivate your own authenticated identity",
        )

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    await _require_person_exists(query_fn, conn.holons_graph, person=person_iri)

    roles = await _person_roles(query_fn, conn, person=person_iri)
    role_iris = {r["role"] for r in roles}
    await _require_owner_for(conn, client, animus, role_iris=role_iris)
    admin_role = conn.role_iri("admin")
    owner_role = conn.role_iri("owner")
    if owner_role in role_iris and await _sole_remaining_owner(
        query_fn, conn, exclude=person_iri
    ):
        return {
            "ok": False,
            "person": person_iri,
            "note": "refused: this is the only owner of this dataset -- grant owner to "
            "someone else first, then deactivate this one",
        }
    if (admin_role in role_iris or owner_role in role_iris) and await _sole_remaining_admin(
        query_fn, conn, exclude=person_iri
    ):
        return {
            "ok": False,
            "person": person_iri,
            "note": "refused: this is the only Person holding the admin role on this "
            "dataset -- deactivating it would leave no route back in through the REST API",
        }

    write_grants = await _person_write_grants(query_fn, conn, person=person_iri)

    try:
        live_delegations = [
            d["delegation"]
            for d in await dg.list_delegations(query_fn, conn.holons_graph, person=person_iri)
            if d["status"] in ("active", "not_yet_valid")
        ]
    except dg.DelegationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    statements = [
        f"DELETE DATA {{ GRAPH <{conn.holons_graph}> "
        f"{{ <{person_iri}> holon:hasRole <{r['role']}> . }} }}"
        for r in roles
    ]
    statements += [
        f"DELETE DATA {{ GRAPH <{conn.holons_graph}> "
        f"{{ <{g['target']}> holon:{_GRANT_PREDICATE[g['mode']]} <{person_iri}> . }} }}"
        for g in write_grants
    ]
    statements.append(
        dg.revoke_all_update(conn.holons_graph, person_iri, datetime.now(timezone.utc))
    )
    statements.append(
        f"INSERT DATA {{ GRAPH <{conn.holons_graph}> "
        f'{{ <{person_iri}> holon:status "inactive" . }} }}'
    )
    update = f"PREFIX holon: <{HOLON}>\n" + " ;\n".join(statements)

    try:
        await client.update(conn, update)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "person": person_iri,
        "deactivated": True,
        "roles_revoked": sorted(role_iris),
        "write_grants_revoked": write_grants,
        "delegations_revoked": live_delegations,
    }


class ReactivatePersonRequest(BaseModel):
    person: str = Field(
        ..., min_length=1,
        description="Person to reactivate: short local slug or full Person IRI.",
    )


@router.post("/person/reactivate")
async def reactivate_person(
    body: ReactivatePersonRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Clear the ``holon:status "inactive"`` marker ``deactivate_person``
    sets, restoring identity resolution -- nothing else. Deliberately
    does NOT restore whatever Roles or write/replace grants deactivation
    revoked: those were a specific, visible set at the moment of
    deactivation (returned in that call's own response) and re-granting
    them is a separate, deliberate decision, not something this should do
    on a caller's behalf. A reactivated Person has no access again,
    exactly like a fresh create_person call with no ``role`` -- grant_role
    / grant_write, called explicitly, is how they get any back.

    Idempotent: reactivating a Person who was never deactivated (or
    already reactivated) is a safe no-op -- DELETE DATA against a triple
    that isn't there removes nothing, per SPARQL 1.1 Update semantics,
    without raising.
    """
    await _require_admin(conn, client, animus)

    person_iri = _resolve_person(conn, body.person)

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    await _require_person_exists(query_fn, conn.holons_graph, person=person_iri)

    try:
        await client.update(
            conn,
            f"""PREFIX holon: <{HOLON}>
DELETE DATA {{ GRAPH <{conn.holons_graph}> {{ <{person_iri}> holon:status "inactive" . }} }}""",
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "person": person_iri,
        "reactivated": True,
        "note": "identity resolution restored; no Role or write/replace grants were "
        "reimplied -- re-grant access explicitly via /admin/role/grant or /admin/write/grant",
    }
