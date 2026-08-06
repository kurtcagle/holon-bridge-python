"""Authentication and request-scoped dependencies.

Everything a handler needs arrives through :func:`require_conn`: the caller
is authenticated, the dataset override is resolved, and the backend client
is attached. A route that forgets to depend on it gets no ``Conn`` at all,
which is the point — the override gap closes by construction.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .config import BankStore, Settings
from .conn import DATASET_OVERRIDE_HEADER, Conn, resolve_conn
from .cache import RegistryCache
from .fuseki import FusekiClient


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_banks(request: Request) -> BankStore:
    return request.app.state.banks


def get_client(request: Request) -> FusekiClient:
    return request.app.state.fuseki


def get_registry(request: Request) -> RegistryCache:
    return request.app.state.registry


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


ConnDep = Annotated[Conn, Depends(require_conn)]
ClientDep = Annotated[FusekiClient, Depends(get_client)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
BanksDep = Annotated[BankStore, Depends(get_banks)]
RegistryDep = Annotated[RegistryCache, Depends(get_registry)]
