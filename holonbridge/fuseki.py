"""Async client for a Jena Fuseki backend.

External clients never reach Fuseki directly. Every call here originates
from a route handler that has already authenticated the caller and resolved
a :class:`~holonbridge.conn.Conn`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from .conn import Conn

SPARQL_JSON = "application/sparql-results+json"
TURTLE = "text/turtle"


class FusekiTimeout(RuntimeError):
    """A backend call exceeded its deadline."""

    def __init__(self, seconds: float, endpoint: str = "") -> None:
        super().__init__(f"backend call exceeded {seconds:g}s")
        self.seconds = seconds
        self.endpoint = endpoint


class FusekiError(RuntimeError):
    """A backend call failed. Carries the upstream status and body."""

    def __init__(self, status: int, message: str, *, endpoint: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.endpoint = endpoint

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": "fuseki_error",
            "status": self.status,
            "endpoint": self.endpoint,
            "message": self.message,
        }


class FusekiClient:
    """Thin async wrapper. One instance per process; share the connection pool."""

    def __init__(self, timeout: float = 60.0) -> None:
        self._timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- internals -------------------------------------------------------------

    @staticmethod
    def _headers(conn: Conn, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if conn.token:
            headers["Authorization"] = f"Bearer {conn.token}"
        return headers

    async def _post(
        self,
        url: str,
        *,
        conn: Conn,
        content: str | bytes,
        content_type: str,
        accept: str,
        timeout: float | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = await self._client.post(
                url,
                content=content.encode("utf-8") if isinstance(content, str) else content,
                headers=self._headers(
                    conn, {"Content-Type": content_type, "Accept": accept}
                ),
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise FusekiTimeout(timeout or self._timeout, endpoint=url) from exc
        if response.status_code >= 400:
            raise FusekiError(response.status_code, response.text.strip(), endpoint=url)
        return response

    # --- SPARQL ----------------------------------------------------------------

    async def select(
        self, conn: Conn, query: str, *, default_graph: str | None = None
    ) -> dict[str, Any]:
        """Run a SELECT/ASK and return the SPARQL JSON results object."""
        url = conn.query_endpoint
        if default_graph:
            url = f"{url}?{urlencode({'default-graph-uri': default_graph})}"
        response = await self._post(
            url,
            conn=conn,
            content=query,
            content_type="application/sparql-query",
            accept=SPARQL_JSON,
        )
        return response.json()

    async def construct(
        self,
        conn: Conn,
        query: str,
        *,
        default_graph: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Run a CONSTRUCT/DESCRIBE and return Turtle."""
        url = conn.query_endpoint
        if default_graph:
            url = f"{url}?{urlencode({'default-graph-uri': default_graph})}"
        response = await self._post(
            url,
            conn=conn,
            content=query,
            content_type="application/sparql-query",
            accept=TURTLE,
            timeout=timeout,
        )
        return response.text

    async def update(self, conn: Conn, update: str) -> None:
        """Run a SPARQL UPDATE against the dedicated update endpoint."""
        await self._post(
            conn.update_endpoint,
            conn=conn,
            content=update,
            content_type="application/sparql-update",
            accept="*/*",
        )

    # --- Graph Store Protocol --------------------------------------------------

    async def put_graph(self, conn: Conn, graph_iri: str, turtle: str) -> None:
        """Replace a named graph (GSP PUT)."""
        await self._gsp(conn, graph_iri, turtle, method="PUT")

    async def post_graph(self, conn: Conn, graph_iri: str, turtle: str) -> None:
        """Merge into a named graph (GSP POST)."""
        await self._gsp(conn, graph_iri, turtle, method="POST")

    async def get_graph(self, conn: Conn, graph_iri: str) -> str:
        url = f"{conn.gsp_endpoint}?{urlencode({'graph': graph_iri})}"
        response = await self._client.get(
            url, headers=self._headers(conn, {"Accept": TURTLE})
        )
        if response.status_code == 404:
            return ""
        if response.status_code >= 400:
            raise FusekiError(response.status_code, response.text.strip(), endpoint=url)
        return response.text

    async def drop_graph(self, conn: Conn, graph_iri: str) -> None:
        await self.update(conn, f"DROP SILENT GRAPH <{graph_iri}>")

    async def _gsp(
        self, conn: Conn, graph_iri: str, turtle: str, *, method: str
    ) -> None:
        url = f"{conn.gsp_endpoint}?{urlencode({'graph': graph_iri})}"
        response = await self._client.request(
            method,
            url,
            content=turtle.encode("utf-8"),
            headers=self._headers(conn, {"Content-Type": TURTLE}),
        )
        if response.status_code >= 400:
            raise FusekiError(response.status_code, response.text.strip(), endpoint=url)

    # --- SHACL -----------------------------------------------------------------

    async def shacl_validate(
        self, conn: Conn, *, target_graph: str, shapes_turtle: str
    ) -> str:
        """Run Jena's SHACL processor over a graph already in the store.

        Returns the validation report as Turtle.
        """
        url = f"{conn.shacl_endpoint}?{urlencode({'graph': target_graph})}"
        response = await self._post(
            url,
            conn=conn,
            content=shapes_turtle,
            content_type=TURTLE,
            accept=TURTLE,
        )
        return response.text

    # --- server-level admin ----------------------------------------------------

    async def list_datasets(self, conn: Conn) -> list[dict[str, Any]]:
        """Ask Fuseki what datasets it actually hosts.

        This is the one call that goes to the *server* root rather than a
        dataset path -- ``/$/datasets`` is Fuseki's admin API, so none of
        ``Conn``'s endpoint properties apply and the URL is built from
        ``base_url`` directly.

        Deliberately distinct from ``list_endpoints``, which reports the
        bridge's own configured banks. A bank names a dataset it
        expects to find; this says what is really there. The two disagreeing
        is a normal and useful thing to be able to see.
        """
        url = f"{conn.base_url}/$/datasets"
        response = await self._client.get(
            url, headers=self._headers(conn, {"Accept": "application/json"})
        )
        if response.status_code >= 400:
            raise FusekiError(response.status_code, response.text.strip(), endpoint=url)

        body = response.json()
        out: list[dict[str, Any]] = []
        for entry in body.get("datasets", []):
            # Fuseki reports the name path-style ("/ds"); every other part of
            # this codebase uses the bare name, so normalise here rather than
            # making each caller strip it.
            name = str(entry.get("ds.name", "")).lstrip("/")
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "state": bool(entry.get("ds.state", True)),
                    "services": sorted(
                        s.get("srv.type", "")
                        for s in entry.get("ds.services", [])
                        if s.get("srv.type")
                    ),
                }
            )
        out.sort(key=lambda d: d["name"])
        return out

    async def ping(self, conn: Conn) -> bool:
        try:
            await self.select(conn, "ASK { ?s ?p ?o }")
            return True
        except (FusekiError, FusekiTimeout, httpx.HTTPError):
            return False
