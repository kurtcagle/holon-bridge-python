"""Support for the Jena-backed test lane (``-m jena``).

The ordinary suite backs its ACL and delegation tests with an in-memory rdflib
``Dataset`` or a stub Fuseki. rdflib is not Jena: it accepts SPARQL Jena
rejects and evaluates some of it differently (PR #43: ``find_delegation``'s
correlated ``FILTER NOT EXISTS`` crashed real Jena with an
``ARQInternalErrorException`` that no stub could show). This lane runs the same
code against a real Fuseki.

Configuration (the lane is skipped unless one of these is set)::

    # Start a throwaway server for the session (preferred). FUSEKI_HOME is the
    # directory holding the ``fuseki-server`` launcher script and jar; with
    # Homebrew that is ``$(brew --prefix fuseki)/libexec``.
    HOLONBRIDGE_TEST_FUSEKI_HOME=/opt/homebrew/opt/fuseki/libexec
    HOLONBRIDGE_TEST_JAVA_HOME=...      # optional; defaults to the environment

    # Or use a scratch Fuseki you already run. Every graph in it is DROPPED
    # before each test, so never point this at data you care about. Port 3030
    # is refused outright.
    HOLONBRIDGE_TEST_JENA_URL=http://127.0.0.1:3039
    HOLONBRIDGE_TEST_JENA_DATASET=acl     # default

Why the launcher is called directly, not the ``fuseki-server`` on PATH: the
Homebrew wrapper hardcodes the live ``FUSEKI_BASE``. The launcher honours the
``FUSEKI_BASE``/``FUSEKI_LOGS``/``FUSEKI_RUN`` this module gives it, all under a
temporary directory, on a free port, with an in-memory dataset. Nothing under
the real base is read or written.
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

FUSEKI_HOME = os.environ.get("HOLONBRIDGE_TEST_FUSEKI_HOME", "").strip()
EXTERNAL_URL = os.environ.get("HOLONBRIDGE_TEST_JENA_URL", "").rstrip("/")
DATASET = os.environ.get("HOLONBRIDGE_TEST_JENA_DATASET", "acl").strip("/") or "acl"
LIVE_PORT = 3030


def configured() -> bool:
    return bool(FUSEKI_HOME or EXTERNAL_URL)


def skip_reason() -> str:
    return (
        "Jena lane not configured: set HOLONBRIDGE_TEST_FUSEKI_HOME (starts a throwaway "
        "Fuseki) or HOLONBRIDGE_TEST_JENA_URL (an existing scratch server)"
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ping(url: str) -> bool:
    try:
        return httpx.get(f"{url}/$/ping", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


class JenaServer:
    """A throwaway Fuseki with one in-memory, updatable dataset."""

    def __init__(self, workdir: Path) -> None:
        self.url = ""
        self.dataset = DATASET
        self._proc: subprocess.Popen[bytes] | None = None
        self._workdir = workdir
        self.log = workdir / "fuseki.log"

    def start(self) -> JenaServer:
        if EXTERNAL_URL:
            port = int(EXTERNAL_URL.rsplit(":", 1)[-1].split("/")[0])
            if port == LIVE_PORT:
                raise RuntimeError("refusing to run the Jena lane against port 3030 (live)")
            self.url = EXTERNAL_URL
            if not _ping(self.url):
                raise RuntimeError(f"no Fuseki answering at {self.url}")
            return self

        launcher = Path(FUSEKI_HOME) / "fuseki-server"
        if not launcher.is_file():
            raise RuntimeError(f"no fuseki-server launcher at {launcher}")
        base = self._workdir / "base"
        env = {
            **os.environ,
            "FUSEKI_HOME": FUSEKI_HOME,
            "FUSEKI_BASE": str(base),
            "FUSEKI_LOGS": str(self._workdir / "logs"),
            "FUSEKI_RUN": str(self._workdir / "run"),
        }
        java_home = os.environ.get("HOLONBRIDGE_TEST_JAVA_HOME", "").strip()
        if java_home:
            env["JAVA_HOME"] = java_home
        port = _free_port()
        assert port != LIVE_PORT
        with open(self.log, "wb") as log:
            self._proc = subprocess.Popen(
                [str(launcher), "--mem", "--update", "--localhost", "--port", str(port),
                 f"/{self.dataset}"],
                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        self.url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"Fuseki exited early:\n{self.log.read_text()[-2000:]}")
            if _ping(self.url):
                return self
            time.sleep(0.25)
        self.stop()
        raise RuntimeError(f"Fuseki did not come up:\n{self.log.read_text()[-2000:]}")

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            os.killpg(self._proc.pid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self._proc.pid, signal.SIGKILL)
        shutil.rmtree(self._workdir / "base", ignore_errors=True)

    # -- raw access --------------------------------------------------------

    def reset(self) -> None:
        r = httpx.post(
            f"{self.url}/{self.dataset}/update",
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
    def __init__(self, ds: JenaDataset, iri: str) -> None:
        self._ds, self._iri = ds, iri

    def parse(self, data: str, format: str = "turtle") -> None:
        # Through SPARQL Update, not the Graph Store Protocol: Jena's GSP
        # rejects a graph name like ``urn:t:holons`` (one-character URN NID)
        # with "Bad URI" while SPARQL Update accepts it, and some existing
        # fixtures use such names. rdflib parses the Turtle; Jena stores it.
        from rdflib import Graph

        assert format == "turtle"
        triples = Graph().parse(data=data, format="turtle").serialize(format="nt")
        self._ds.update(f"INSERT DATA {{ GRAPH <{self._iri}> {{\n{triples}\n}} }}")

    def objects(self, subject: URIRef, predicate: URIRef) -> list[URIRef]:
        body = self._ds.query(
            f"SELECT ?o WHERE {{ GRAPH <{self._iri}> {{ <{subject}> <{predicate}> ?o }} }}"
        )
        return [URIRef(row["o"]) for row in body]


class JenaDataset:
    """A synchronous stand-in for the rdflib ``Dataset`` the stub tests use,
    talking to a real Fuseki. Just enough surface that the existing test
    classes can run against Jena by swapping their store, assertions untouched.
    """

    def __init__(self, server: JenaServer) -> None:
        self.server = server

    def get_context(self, iri: URIRef | str) -> _Context:
        return _Context(self, str(iri))

    graph = get_context

    def query(self, sparql: str) -> _Result:
        r = httpx.post(
            f"{self.server.url}/{self.server.dataset}/query",
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
        )
        if r.status_code >= 400:
            raise JenaRejected(r.status_code, r.text)
        return _Result(r.json())

    def update(self, sparql: str) -> None:
        r = httpx.post(
            f"{self.server.url}/{self.server.dataset}/update",
            content=sparql.encode(),
            headers={"Content-Type": "application/sparql-update"},
        )
        if r.status_code >= 400:
            raise JenaRejected(r.status_code, r.text)


class JenaRejected(RuntimeError):
    """Jena answered a query or update with an error status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Jena returned {status}: {body[:500]}")
        self.status = status
        self.body = body


def conn_for(ds: JenaDataset):
    from holonbridge.conn import Conn

    return Conn(base_url=ds.server.url, dataset=ds.server.dataset, overridden=False,
                bank_name="local")


def query_fn_for(ds: JenaDataset):
    """An async ``query_fn`` that goes through the real ``FusekiClient`` code
    path (the same HTTP request and SPARQL-JSON the production ACL layer uses).
    A client per call: some callers drive it from several short-lived event
    loops, and an httpx pool must not outlive its loop."""
    from holonbridge.fuseki import FusekiClient

    conn = conn_for(ds)

    async def q(sparql: str) -> dict:
        client = FusekiClient()
        try:
            return await client.select(conn, sparql)
        finally:
            await client.aclose()

    return q
