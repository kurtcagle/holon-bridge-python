"""Per-connection registry cache.

Keyed by ``(url, dataset, kind)``. That key is why no ``conn.overridden``
guard appears at the call sites: a cache that cannot be addressed across
datasets cannot leak across them either. The guard exists for process-global
state, and this deliberately is not that.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar

from .conn import Conn
from .fuseki import FusekiClient

T = TypeVar("T")

Loader = Callable[[FusekiClient, Conn], Awaitable[T]]


@dataclass
class _Entry(Generic[T]):
    loaded_at: float
    value: T


class RegistryCache:
    """Short-TTL cache for registries loaded out of the graph."""

    def __init__(self, ttl: float = 60.0) -> None:
        self._ttl = ttl
        self._entries: dict[tuple[str, str, str], _Entry] = {}

    @staticmethod
    def _key(conn: Conn, kind: str) -> tuple[str, str, str]:
        return (conn.base_url, conn.dataset, kind)

    async def get(
        self,
        client: FusekiClient,
        conn: Conn,
        *,
        kind: str,
        loader: Loader[T],
        refresh: bool = False,
    ) -> T:
        key = self._key(conn, kind)
        entry = self._entries.get(key)
        if entry and not refresh and (time.monotonic() - entry.loaded_at) < self._ttl:
            return entry.value

        value = await loader(client, conn)
        self._entries[key] = _Entry(loaded_at=time.monotonic(), value=value)
        return value

    def invalidate(self, conn: Conn, kind: str | None = None) -> None:
        if kind is not None:
            self._entries.pop(self._key(conn, kind), None)
            return
        for key in [
            k
            for k in self._entries
            if k[0] == conn.base_url and k[1] == conn.dataset
        ]:
            self._entries.pop(key, None)
