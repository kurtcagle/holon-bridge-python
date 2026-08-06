"""Dataset routes -- what the backend actually hosts.

Distinct from `/endpoints`, which reports the bridge's own configured
banks. A bank is an *intention*: "connect to this server, use this
dataset." This is the observation: what the server really has. Those two
disagreeing is the normal case worth being able to see -- a bank naming
a dataset that was renamed or never created looks identical to a working
one until something reads from it and comes back empty.

Listing only. Creating and dropping datasets stays out for now: those are
destructive server-level operations, and the bridge deliberately holds one
credential shared by every caller, so "who asked for this" cannot be
answered yet. Adding them before per-caller identity reaches the REST layer
would put a `DROP` behind a shared secret.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..deps import ClientDep, ConnDep
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
