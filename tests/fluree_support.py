"""Support for the Fluree-backed test lane (``-m fluree``).

Mirrors ``fluree_support.py``. The same ACL and delegation code that the Fluree lane
runs against Fuseki runs here against Fluree 4.x, through ``FlureeClient`` and a
``Conn`` with ``store_kind="fluree"`` (nothing is monkey-patched).

Configuration (the lane is skipped unless this is set)::

    HOLONBRIDGE_TEST_FLUREE_BIN=/path/to/fluree     # the fluree binary

A throwaway project directory, on a free port (never 3030/3031/8090), is created
for the session and one ledger (``acl``) is created in it. Every test starts by
dropping all graphs. Real servers run with ``--gc-max-old-indexes 1
--gc-min-time-mins 0``; add the same flags here if the binary supports them.
SHACL is not part of this lane (phase 2): ``FlureeClient.shacl_validate`` raises.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from rdflib import URIRef

FLUREE_BIN = os.environ.get("HOLONBRIDGE_TEST_FLUREE_BIN", "").strip()
DATASET = "acl"
REFUSED_PORTS = (3030, 3031, 8090)


def configured() -> bool:
    return bool(FLUREE_BIN)


def skip_reason() -> str:
    return (
        "Fluree lane not configured: set HOLONBRIDGE_TEST_FLUREE_BIN (path to the fluree binary)"
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ping(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


class FlureeServer:
    """A throwaway Fluree 4.x server with one in-memory-ish ledger (named FlureeServer so the
    lane tests need no edits; it is Fluree)."""

    def __init__(self, workdir: Path) -> None:
        self.url = ""
        self.dataset = DATASET
        self._proc: subprocess.Popen[bytes] | None = None
        self._workdir = workdir
        self.log = workdir / "fluree.log"

    @property
    def api(self) -> str:
        return f"{self.url}/v1/fluree"

    def start(self) -> FlureeServer:
        binary = os.environ.get("HOLONBRIDGE_TEST_FLUREE_BIN", "").strip()
        if not binary:
            raise RuntimeError("HOLONBRIDGE_TEST_FLUREE_BIN not set")
        proj = self._workdir / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        subprocess.run([binary, "init"], cwd=proj, check=True, capture_output=True)
        port = _free_port()
        assert port not in REFUSED_PORTS
        with open(self.log, "wb") as log:
            self._proc = subprocess.Popen(
                [binary, "server", "run", "--listen-addr", f"127.0.0.1:{port}"],
                cwd=proj, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        self.url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"Fluree exited early:\n{self.log.read_text()[-2000:]}")
            try:
                if httpx.get(f"{self.url}/health", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            self.stop()
            raise RuntimeError("Fluree did not come up")
        r = httpx.post(f"{self.api}/create", json={"ledger": self.dataset})
        r.raise_for_status()
        return self

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            os.killpg(self._proc.pid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGKILL)

    def reset(self) -> None:
        r = httpx.post(
            f"{self.api}/update/{self.dataset}",
            content="DROP ALL",
            headers={"Content-Type": "application/sparql-update"},
        )
        r.raise_for_status()


class _Result:
    """The slice of rdflib's ``Result`` the existing tests' helpers read."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.raw = body
        self.type = "ASK" if "boolean" in body else "SELECT"
        self.askAnswer = body.get("boolean")
        self.vars = body.get("head", {}).get("vars", [])
        self._rows = body.get("results", {}).get("bindings", [])

    def __iter__(self) -> Iterator[dict[str, str]]:
        for binding in self._rows:
            yield {v: binding[v]["value"] for v in self.vars if v in binding}


class _Context:
    def __init__(self, ds: FlureeDataset, iri: str) -> None:
        self._ds, self._iri = ds, iri

    def parse(self, data: str, format: str = "turtle") -> None:
        # Through SPARQL Update, not the Graph Store Protocol: Fluree's GSP
        # rejects a graph name like ``urn:t:holons`` (one-character URN NID)
        # with "Bad URI" while SPARQL Update accepts it, and some existing
        # fixtures use such names. rdflib parses the Turtle; Fluree stores it.
        from rdflib import Graph

        assert format == "turtle"
        triples = Graph().parse(data=data, format="turtle").serialize(format="nt")
        self._ds.update(f"INSERT DATA {{ GRAPH <{self._iri}> {{\n{triples}\n}} }}")

    def objects(self, subject: URIRef, predicate: URIRef) -> list[URIRef]:
        body = self._ds.query(
            f"SELECT ?o WHERE {{ GRAPH <{self._iri}> {{ <{subject}> <{predicate}> ?o }} }}"
        )
        return [URIRef(row["o"]) for row in body]


class FlureeDataset:
    """A synchronous stand-in for the rdflib ``Dataset`` the stub tests use,
    talking to a real Fluree ledger. Just enough surface that the existing test
    classes can run against Fluree by swapping their store, assertions untouched.
    """

    def __init__(self, server: FlureeServer) -> None:
        self.server = server

    def get_context(self, iri: URIRef | str) -> _Context:
        return _Context(self, str(iri))

    graph = get_context

    def query(self, sparql: str) -> _Result:
        r = httpx.post(
            f"{self.server.api}/query/{self.server.dataset}",
            content=sparql.encode(),
            headers={"Content-Type": "application/sparql-query",
                     "Accept": "application/sparql-results+json"},
        )
        if r.status_code >= 400:
            raise FlureeRejected(r.status_code, r.text)
        return _Result(r.json())

    def update(self, sparql: str) -> None:
        r = httpx.post(
            f"{self.server.api}/update/{self.server.dataset}",
            content=sparql.encode(),
            headers={"Content-Type": "application/sparql-update"},
        )
        if r.status_code >= 400:
            raise FlureeRejected(r.status_code, r.text)


class FlureeRejected(RuntimeError):
    """Fluree answered a query or update with an error status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Fluree returned {status}: {body[:500]}")
        self.status = status
        self.body = body


def conn_for(ds: FlureeDataset):
    from holonbridge.conn import Conn

    return Conn(base_url=ds.server.url, dataset=ds.server.dataset, overridden=False,
                bank_name="local", store_kind="fluree")


def query_fn_for(ds: FlureeDataset):
    """An async ``query_fn`` that goes through the real ``FlureeClient`` code
    path (the same HTTP request and SPARQL-JSON the production ACL layer uses).
    A client per call: some callers drive it from several short-lived event
    loops, and an httpx pool must not outlive its loop."""
    from holonbridge.fluree import FlureeClient

    conn = conn_for(ds)

    async def q(sparql: str) -> dict:
        client = FlureeClient()
        try:
            return await client.select(conn, sparql)
        finally:
            await client.aclose()

    return q


