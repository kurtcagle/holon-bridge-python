"""The store seam: what the bridge needs from a triple store.

``Store`` is a structural :class:`typing.Protocol`, so ``FusekiClient`` conforms
without inheriting from it and test doubles need no base class. The default
kind is ``fuseki``; ``fluree`` selects :class:`~holonbridge.fluree.FlureeClient`.
Set it with the ``STORE_KIND`` environment variable (``Settings.store_kind``).
``Conn.store_kind`` decides the endpoint URLs; the client class decides the
wire behaviour. ``make_client`` keeps the two in step.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .conn import Conn

STORE_KINDS = ("fuseki", "fluree")
DEFAULT_STORE_KIND = "fuseki"


@runtime_checkable
class Store(Protocol):
    async def aclose(self) -> None: ...

    async def select(
        self, conn: Conn, query: str, *, default_graph: str | None = None
    ) -> dict[str, Any]: ...

    async def construct(
        self,
        conn: Conn,
        query: str,
        *,
        default_graph: str | None = None,
        timeout: float | None = None,
    ) -> str: ...

    async def update(self, conn: Conn, update: str) -> None: ...

    async def put_graph(self, conn: Conn, graph_iri: str, turtle: str) -> None: ...

    async def post_graph(self, conn: Conn, graph_iri: str, turtle: str) -> None: ...

    async def get_graph(self, conn: Conn, graph_iri: str) -> str: ...

    async def drop_graph(self, conn: Conn, graph_iri: str) -> None: ...

    async def shacl_validate(
        self, conn: Conn, *, target_graph: str, shapes_turtle: str
    ) -> str: ...

    async def list_datasets(self, conn: Conn) -> list[dict[str, Any]]: ...

    async def ping(self, conn: Conn) -> bool: ...


def make_client(kind: str = DEFAULT_STORE_KIND, *, timeout: float = 60.0) -> Store:
    """Build the client for ``kind``. Unknown kinds fail at startup, not per request."""
    if kind == "fuseki":
        from .fuseki import FusekiClient

        return FusekiClient(timeout=timeout)
    if kind == "fluree":
        from .fluree import FlureeClient

        return FlureeClient(timeout=timeout)
    raise ValueError(f"unknown store_kind {kind!r}; expected one of {STORE_KINDS}")
