"""switch_persona and list_personas: per-person persona session state.

See persona_state.py for why this state lives here (keyed by the resolved
Person) rather than in the MCP layer alongside switch_dataset/switch_bank.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import AnimusDep, ClientDep, ConnDep, PersonasDep
from ..fuseki import FusekiError
from ..persona import has_home, persona_exists

router = APIRouter(prefix="/persona", tags=["persona"])


class SwitchPersonaRequest(BaseModel):
    name: str = Field(default="")


@router.post("/switch")
async def switch_persona(
    body: SwitchPersonaRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
) -> dict:
    """Switch the calling person's active persona for the current dataset.

    Gated on one thing only: the calling person must already hold a
    holon:Home under the named persona (has_home). persona_exists is
    checked first only so the two failure modes stay distinguishable --
    collapsing them is how this fails quietly:

    - no holon:Persona by that name at all -> not_found (the name is
      wrong)
    - the persona exists but this person has no Home under it -> refused
      (outside this person's envelope)

    There is no third "credential resolves to no Person" case to handle
    here -- AnimusDep already 401s before this handler runs, so
    ``animus.person`` is always a real, resolved Person by the time this
    code executes.

    ``name`` is never a lookup key for someone else's state: the row this
    writes is keyed by ``animus.person`` (from the caller's own
    credential, never a request field) and ``conn.dataset``, so this call
    can only ever change the caller's own override for the dataset
    they're currently pointed at.

    Pass an empty string to clear this person's override for this
    dataset -- scope falls back to ground truth only, and no membership
    check runs (there's nothing to be a member of).
    """
    person_id = animus.person
    name = body.name.strip()

    if not name:
        personas.set(person_id=person_id, dataset=conn.dataset, persona="")
        return {
            "ok": True,
            "cleared": True,
            "persona": None,
            "note": "override cleared; ground truth only",
        }

    async def query_fn(query: str) -> dict:
        return await client.select(conn, query)

    try:
        exists = await persona_exists(query_fn, conn, persona=name)
        member = exists and await has_home(query_fn, conn, persona=name, person_id=person_id)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    if not exists:
        return {
            "ok": False,
            "requested": name,
            "persona": None,
            "note": "not_found: no holon:Persona by that name in this dataset",
        }
    if not member:
        return {
            "ok": False,
            "requested": name,
            "persona": None,
            "note": "refused: no Home for this person under that persona",
        }

    personas.set(person_id=person_id, dataset=conn.dataset, persona=name)
    return {"ok": True, "persona": name}


@router.get("/list")
async def list_personas(conn: ConnDep, client: ClientDep, animus: AnimusDep) -> dict:
    """Every holon:Persona in this dataset, and whether the calling person
    holds a Home under it -- i.e. which names switch_persona would
    actually accept from them right now. Without this, the only way to
    discover a valid argument to switch_persona is a raw SPARQL query
    against a graph the caller may not be able to name.
    """
    query = f"""PREFIX holon: <https://w3id.org/holon/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?persona ?label WHERE {{
  GRAPH <{conn.graph("holons")}> {{
    ?persona a holon:Persona .
    OPTIONAL {{ ?persona rdfs:label ?label . }}
  }}
}} ORDER BY ?persona"""

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    try:
        result = await client.select(conn, query)
        rows = result.get("results", {}).get("bindings", [])
        out = []
        for row in rows:
            persona_iri = row["persona"]["value"]
            name = persona_iri.rsplit(":", 1)[-1]
            member = await has_home(query_fn, conn, persona=name, person_id=animus.person)
            out.append(
                {
                    "persona": persona_iri,
                    "name": name,
                    "label": row.get("label", {}).get("value"),
                    "member": member,
                }
            )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"dataset": conn.dataset, "personas": out}
