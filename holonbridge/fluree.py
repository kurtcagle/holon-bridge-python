"""Async client for a Fluree server (4.x), same surface as ``FusekiClient``.

Differences from Fuseki that this class owns:

* endpoints are per ledger (``Conn`` builds them when ``store_kind == "fluree"``);
* the Graph Store endpoint needs a target on every call: ``?graph=<iri>`` or
  ``?default=true`` for the default graph (``?graph=default`` is rejected);
* liveness is ``GET /health`` (``ASK {?s ?p ?o}`` is false when all data sits
  in named graphs);
* ledgers are listed at ``/v1/fluree/ledgers``;
* end-user identity is forwarded per request (``fluree-identity`` and optional
  ``fluree-policy-class``) from ``Conn.identity`` / ``Conn.policy_class``; the
  bearer token is the backend's policy-authority token;
* ``shacl_validate`` raises: Fluree has no SHACL endpoint over a graph. The
  write gate is the read-only merge check in ``fluree_shacl`` (chosen by
  ``shacl.py`` from ``conn.store_kind``).

A 200 from update or Graph Store writes does NOT prove the write applied (a
policy-filtered DELETE and a lost compare-and-set both return 200 with no
effect). Callers that need to know must confirm with a marker or re-read.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from .conn import Conn
from .fuseki import FusekiClient, FusekiError, FusekiTimeout

DEFAULT_GRAPH_NAMES = (None, "", "default")


class FlureeClient(FusekiClient):
    @staticmethod
    def _headers(conn: Conn, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = FusekiClient._headers(conn, extra)
        if conn.identity:
            headers["fluree-identity"] = conn.identity
            if conn.policy_class:
                headers["fluree-policy-class"] = conn.policy_class
        return headers

    @staticmethod
    def _gsp_url(conn: Conn, graph_iri: str | None) -> str:
        if graph_iri in DEFAULT_GRAPH_NAMES:
            return f"{conn.gsp_endpoint}?default=true"
        return f"{conn.gsp_endpoint}?{urlencode({'graph': graph_iri})}"

    async def shacl_validate(
        self, conn: Conn, *, target_graph: str, shapes_turtle: str
    ) -> str:
        raise NotImplementedError(
            "phase 2: no SHACL endpoint on Fluree; shacl.py uses the read-only merge gate (fluree_shacl)"
        )

    async def list_datasets(self, conn: Conn) -> list[dict[str, Any]]:
        """Ledgers, in the same shape as Fuseki's datasets.

        ``/v1/fluree/ledgers`` returns ``[{name, branch, type, t}]``; one entry
        per ledger name (branches are collapsed).
        """
        url = f"{conn.base_url}/v1/fluree/ledgers"
        response = await self._client.get(
            url, headers=self._headers(conn, {"Accept": "application/json"})
        )
        if response.status_code >= 400:
            raise FusekiError(response.status_code, response.text.strip(), endpoint=url)
        body = response.json()
        if isinstance(body, dict):  # tolerate {"ledgers": [...]}
            body = body.get("ledgers", [])
        names = sorted({str(e.get("name", "")).lstrip("/") for e in body if e.get("name")})
        return [
            {"name": n, "state": True, "services": ["data", "query", "update"]}
            for n in names
        ]

    async def ping(self, conn: Conn) -> bool:
        try:
            response = await self._client.get(
                f"{conn.base_url}/health", headers=self._headers(conn)
            )
            return response.status_code == 200
        except (FusekiTimeout, httpx.HTTPError):
            return False
