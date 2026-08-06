"""Bank routes -- the bridge's own configured backend connections.

Distinct from `/datasets`, which reports what a bank's server actually
hosts. A bank is the *intention*: "connect to this server, use this
dataset by default." This is where that intention is read back and,
via `POST /endpoint`, changed.

auth is applied at the router level rather than per-route. ConnDep
already pulls in require_auth transitively (see deps.require_conn), but
BanksDep does not -- it is just `request.app.state.banks`, no auth
check attached. Without the router-level dependency, /endpoints and
POST /endpoint would be reachable with no bearer token at all, while
GET /endpoint (which uses ConnDep) would not. Declaring it once here
keeps all three routes uniform instead of that being an accident of
which dependency each handler happens to need.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..deps import BanksDep, ConnDep, require_auth

router = APIRouter(tags=["banks"], dependencies=[Depends(require_auth)])


class SetEndpointRequest(BaseModel):
    name: str = Field(..., min_length=1)


@router.get("/endpoint")
async def get_endpoint(conn: ConnDep) -> dict:
    """The active bank, dataset, and canonical graph IRIs for this request.

    Reflects any per-request ``?bank=`` override or ``X-Dataset-Override``
    header the same way every other route does -- this reads the same
    ``Conn`` every handler resolves, not a separate notion of "active".
    """
    return conn.describe()


@router.post("/endpoint")
async def set_endpoint(body: SetEndpointRequest, banks: BanksDep) -> dict:
    """Switch the bridge's own active bank.

    Affects every caller against this process, not just the one that made
    the request -- a server-side default, not a per-call override. A client
    that wants its own view without moving everyone else should pass
    ``?bank=`` on individual requests instead (see ``deps.require_conn``).
    """
    try:
        bank = banks.set_active(body.name)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_bank",
                "name": body.name,
                "available": [b["name"] for b in banks.list()],
            },
        ) from exc
    return {"ok": True, "bank": bank.name, "dataset": bank.dataset}


@router.get("/endpoints")
async def list_endpoints(banks: BanksDep) -> dict:
    """Every bank the bridge has configured, and which one is active.

    Distinct from `/datasets`: a bank names a server and a default dataset
    it expects to find there; this is the bridge's own configuration, not
    an observation of what a server actually hosts.
    """
    listing = banks.list()
    return {
        "count": len(listing),
        "active": banks.active.name,
        "banks": listing,
    }
