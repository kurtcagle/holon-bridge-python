"""Authentication and request-scoped dependencies.

Everything a handler needs arrives through :func:`require_conn`: the caller
is authenticated, the dataset override is resolved, and the backend client
is attached. A route that forgets to depend on it gets no ``Conn`` at all,
which is the point -- the override gap closes by construction.

CHANGED 2026-08-15: added :func:`require_animus`. ``require_auth`` is a
single shared bearer token -- it answers "is this caller allowed to talk to
the bridge at all", the same for every caller. It was never a per-person
identity, and nothing before this added one. ``require_animus`` is that:
a caller now additionally presents *who* they are (an external identity,
e.g. a GitHub login), which gets resolved against this request's own
dataset to a Person, then to whatever Roles and grants that Person holds.
The two checks are independent and both run -- a valid bearer token gets
you in the door, a resolved animus gets you access to something once
you're through it. See the ACL architecture DataBook for the model this
resolves against.

CHANGED 2026-08-17: added :func:`get_personas`. Session state for "which
persona is this person currently reading as" -- see persona_state.py for
why it's keyed by Animus.person rather than living in the MCP layer
alongside the dataset/bank overrides. One ``PersonaStore`` per process,
same lifetime as ``app.state.fuseki``.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .acl import Animus, build_animus
from .config import BankStore, Settings
from .conn import DATASET_OVERRIDE_HEADER, Conn, resolve_conn
from .cache import RegistryCache
from .fuseki import FusekiClient
from .persona_state import PersonaStore

ANIMUS_ID_HEADER = "x-holon-animus-id"
ANIMUS_TYPE_HEADER = "x-holon-animus-type"
DEFAULT_ANIMUS_TYPE = "GitHubIdentity"


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_banks(request: Request) -> BankStore:
    return request.app.state.banks


def get_client(request: Request) -> FusekiClient:
    return request.app.state.fuseki


def get_registry(request: Request) -> RegistryCache:
    return request.app.state.registry


def get_personas(request: Request) -> PersonaStore:
    return request.app.state.personas


def require_auth(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Bearer check. Runs before anything touches the backend."""
    expected = settings.bearer_token
    if not expected:
        # No token configured: local development only. Refuse to serve
        # anything if the process is listening on a non-loopback interface.
        if settings.host not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="BEARER_TOKEN must be set when binding a public interface",
            )
        return

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization.split(" ", 1)[1].strip()
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_conn(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    banks: Annotated[BankStore, Depends(get_banks)],
    _: Annotated[None, Depends(require_auth)],
) -> Conn:
    """Resolve the connection for this request."""
    override = request.headers.get(DATASET_OVERRIDE_HEADER)
    bank_name = request.query_params.get("bank")
    try:
        return resolve_conn(
            settings=settings,
            banks=banks,
            override=override,
            bank_name=bank_name,
        )
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"unknown bank: {exc.args[0]}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


async def require_animus(
    request: Request,
    conn: Annotated[Conn, Depends(require_conn)],
    client: Annotated[FusekiClient, Depends(get_client)],
) -> Animus:
    """Resolve the calling Person (and their Teams) for this request.

    A missing or unresolvable identity is a 401, not an anonymous pass-through
    -- there is no route that should run ACL-gated logic against an
    ``Animus`` with ``person=None`` and treat that as "allow everything";
    callers that need that decision explicitly available should depend on
    :func:`require_conn` alone and skip this dependency, not lean on this
    one failing to resolve.
    """
    external_id = request.headers.get(ANIMUS_ID_HEADER)
    if not external_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {ANIMUS_ID_HEADER} header",
        )
    external_id_type = request.headers.get(ANIMUS_TYPE_HEADER, DEFAULT_ANIMUS_TYPE)

    async def query_fn(query: str) -> dict:
        return await client.select(conn, query)

    animus = await build_animus(
        query_fn,
        conn.holons_graph,
        external_id=external_id,
        external_id_type=external_id_type,
    )
    if animus.person is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"no Person found for {external_id_type} identifier {external_id!r}",
        )
    return animus


ConnDep = Annotated[Conn, Depends(require_conn)]
ClientDep = Annotated[FusekiClient, Depends(get_client)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
BanksDep = Annotated[BankStore, Depends(get_banks)]
RegistryDep = Annotated[RegistryCache, Depends(get_registry)]
AnimusDep = Annotated[Animus, Depends(require_animus)]
PersonasDep = Annotated[PersonaStore, Depends(get_personas)]
