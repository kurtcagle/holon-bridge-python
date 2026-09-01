"""Admin identity routes: creating a Person, assigning a Persona, and
(CHANGED 2026-09-01) admin-only session impersonation for testing.

All four endpoints are admin-only (is_admin bypass -- the same
break-glass capability acl.py already defines), distinct from persona.py's
switch_persona, which is self-service and membership-gated. Creating a
new Person or minting someone else's Home is never something a caller
does to themselves the first time -- there is no bootstrap path around
that, matching how every Person creation so far in this codebase (the
8/17 sweep, the same-day aimee mint on 2026-08-25) has been an admin
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
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..acl import is_admin
from ..deps import ActingAsDep, AnimusDep, ClientDep, ConnDep
from ..fuseki import FusekiError
from ..persona import persona_exists

router = APIRouter(prefix="/admin", tags=["admin"])

HOLON = "https://w3id.org/holon/"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"

_METADATA_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
    persona: str = Field(..., min_length=1, description="Local persona name, e.g. 'aimee'.")
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
        "(e.g. 'ctownley-cs'), resolved via conn.person_iri() the same "
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
    """
    await _require_admin(conn, client, animus)

    target_iri = (
        body.target
        if body.target.startswith("http") or ":" in body.target
        else conn.person_iri(body.target)
    )

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    exists_query = f"""
    PREFIX holon: <{HOLON}>
    ASK {{ GRAPH <{conn.holons_graph}> {{ <{target_iri}> a holon:Person }} }}
    """
    result = await query_fn(exists_query)
    if not result.get("boolean"):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"no holon:Person found: {target_iri}"
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
