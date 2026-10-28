"""Dataset routes -- what the backend actually hosts.

Distinct from `/endpoints`, which reports the bridge's own configured
banks. A bank is an *intention*: "connect to this server, use this
dataset." This is the observation: what the server really has. Those two
disagreeing is the normal case worth being able to see -- a bank naming
a dataset that was renamed or never created looks identical to a working
one until something reads from it and comes back empty.

Listing only, here. Creating a dataset is ``routes/dataset_create.py``
(CHANGED 2026-10-27): it became safe to add once per-caller identity reached
the REST layer, and it is gated on an *established* identity listed in
``DATASET_CREATORS`` -- never on the shared token, and never on admin of any
one dataset. Dropping a dataset stays out, deliberately and for good: it is
the destructive half, and nothing in the bridge can undo it. A test
(``tests/test_dataset_create.py``) fails if a drop route or client method
appears.

CHANGED 2026-09-27: added the text-index provisioning route. This is a
narrower, additive operation than create/drop -- it patches an *existing*
dataset's Fuseki assembler file to add a jena-text Lucene index, never
creating or dropping anything -- but it is still a filesystem write on the
Fuseki host, so it gets the same admin-only gate for the same reason: no
per-caller accountability yet for actions this sensitive. See
provisioning.py for the actual patch logic; this route is a thin
admin-check-then-call wrapper around it.
"""

from __future__ import annotations

from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, status

from .. import provisioning
from ..acl import is_admin
from ..deps import AnimusDep, ClientDep, ConnDep, SettingsDep
from ..fuseki import FusekiError

router = APIRouter(tags=["datasets"])


@router.get("/datasets")
async def list_datasets(conn: ConnDep, client: ClientDep) -> dict:
    """Every dataset the backend hosts, with the bridge's own view alongside."""
    try:
        datasets = await client.list_datasets(conn)
    except FusekiError as exc:
        # A 404 here usually means the admin API is disabled or the server is
        # not Fuseki at all -- worth saying rather than passing through a bare
        # status code, since the fix is a server configuration change.
        detail = exc.as_dict()
        if exc.status == 404:
            detail["hint"] = (
                "Fuseki's /$/datasets admin API did not respond. It may be "
                "disabled, or this may not be a Fuseki server."
            )
        raise HTTPException(exc.status or 502, detail=detail) from exc

    return {
        "server": conn.base_url,
        "active": conn.dataset,
        "overridden": conn.overridden,
        "count": len(datasets),
        "datasets": datasets,
    }


@router.get("/datasets/{name}")
async def get_dataset(name: str, conn: ConnDep, client: ClientDep) -> dict:
    """Confirm one dataset exists, and report its graph-role IRIs.

    Exists so a caller about to switch to a dataset can check it first. A
    typo that silently switches to a non-existent dataset produces empty
    reads that look like empty data, which is a slow and confusing way to
    discover a spelling mistake.
    """
    try:
        datasets = await client.list_datasets(conn)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    match = next((d for d in datasets if d["name"] == name), None)
    if match is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_dataset",
                "name": name,
                "available": [d["name"] for d in datasets],
            },
        )

    from dataclasses import replace  # noqa: PLC0415

    from ..conn import GRAPH_ROLES  # noqa: PLC0415

    scoped = replace(conn, dataset=name, overridden=True)
    return {
        **match,
        "graphs": {role: scoped.graph(role) for role in GRAPH_ROLES},
    }


class ExtraPredicate(BaseModel):
    """One additional text-index field, beyond provisioning.py's defaults."""

    field: str
    prefix: str
    local: str


class TextIndexRequest(BaseModel):
    extra_predicates: list[ExtraPredicate] | None = None
    lucene_dir: str | None = None
    commit: bool = False


@router.post("/datasets/{name}/text-index")
async def provision_text_index(
    name: str,
    body: TextIndexRequest,
    animus: AnimusDep,
    client: ClientDep,
    conn: ConnDep,
    settings: SettingsDep,
) -> dict:
    """Preview or apply a Lucene (`jena-text`) index patch to a dataset's
    Fuseki assembler file.

    Admin-only, same reasoning as dataset creation/drop in this module's
    docstring: this is a filesystem write on the Fuseki host, not yet
    something the shared bearer token can answer "who asked for this" for.

    `commit=false` (the default) returns the full proposed file content as
    `preview` without writing anything -- look it over, then call again
    with `commit=true` to actually write it. Idempotent either way: if the
    dataset's assembler file already declares a text index, this reports
    `changed=false` rather than layering a second one on top.

    Fuseki must be restarted for a written change to take effect -- this
    endpoint never restarts it, and never will; see provisioning.py's
    module docstring for why that stays a separate, human-owned step.
    """

    async def query_fn(query: str) -> dict:
        return await client.select(conn, query)

    if not await is_admin(query_fn, conn.holons_graph, person=animus.person):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin role required")

    if not settings.fuseki_config_dir:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "FUSEKI_CONFIG_DIR is not configured on this bridge process -- "
                "set it to Fuseki's assembler-config directory before using "
                "this endpoint"
            ),
        )

    extras = [(p.field, p.prefix, p.local) for p in (body.extra_predicates or [])]
    fn = provisioning.ensure_text_index if body.commit else provisioning.preview_text_index
    try:
        result = fn(
            name,
            settings.fuseki_config_dir,
            extra_predicates=extras,
            lucene_dir=body.lucene_dir,
        )
    except provisioning.ProvisioningError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {
        "dataset": result.dataset,
        "path": str(result.path),
        "changed": result.changed,
        "committed": bool(body.commit and result.changed),
        "reason": result.reason,
        "restartRequired": result.restart_required,
        "preview": None if body.commit else result.new_text,
    }
