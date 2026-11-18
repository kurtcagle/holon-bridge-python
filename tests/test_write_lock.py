"""Tests for the validate-then-write TOCTOU fix flagged by Ben Wortley,
2026-09-15 -- see ``holonbridge/write_lock.py`` and the ``CHANGED
2026-09-15`` note in ``holonbridge/ingest.py``.

``write_turtle_to_graph`` used to run its SHACL validate-then-write
sequence with no lock between the two: two concurrent calls against the
same target graph could each validate against a baseline that predates
the other's write, and then both land. These tests don't reconstruct the
whole SHACL/Fuseki round trip -- ``test_shacl.py`` already covers that in
isolation -- they prove the seam that matters here: once ACL clears, the
validate-and-write block for one call cannot interleave with another
call's validate-and-write block against the same (base_url, dataset)
target.
"""

from __future__ import annotations

import asyncio

from holonbridge import ingest, write_lock
from holonbridge import shacl as shacl_mod
from holonbridge.acl import AclDecision, Animus
from holonbridge.conn import Conn
from holonbridge.shacl import ValidationReport


def _conn(*, base_url: str = "http://fuseki.test", dataset: str = "ds") -> Conn:
    return Conn(base_url=base_url, dataset=dataset, overridden=False, bank_name="local")


def _animus() -> Animus:
    return Animus(
        external_id="test", external_id_type="GitHubIdentity", person="urn:ds:person:kurt"
    )


class RecordingClient:
    """Only ``post_graph``/``put_graph`` matter here -- the ACL check and
    the SHACL validate step are monkeypatched per test so this stays
    focused on the lock's mutual-exclusion guarantee, not on reproducing
    real SHACL/Fuseki behaviour."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str]] = []
        self.put: list[tuple[str, str]] = []

    async def post_graph(self, conn, graph_iri, turtle):
        self.posted.append((graph_iri, turtle))

    async def put_graph(self, conn, graph_iri, turtle):
        self.put.append((graph_iri, turtle))

    async def select(self, conn, query):
        # The structural-freeze check reads the dataset; nothing is inactive here.
        return {"boolean": False, "results": {"bindings": []}}


def _bypass_acl(monkeypatch) -> None:
    async def _always_allowed(query_fn, holons_graph, *, person, target):
        return AclDecision(True, "test bypass", person)

    monkeypatch.setattr(ingest, "check_write", _always_allowed)
    monkeypatch.setattr(ingest, "check_replace", _always_allowed)


def test_for_conn_is_stable_per_base_url_and_dataset():
    a1 = write_lock.for_conn(_conn(base_url="http://a", dataset="x"))
    a2 = write_lock.for_conn(_conn(base_url="http://a", dataset="x"))
    b = write_lock.for_conn(_conn(base_url="http://a", dataset="y"))
    c = write_lock.for_conn(_conn(base_url="http://b", dataset="x"))

    assert a1 is a2  # same (base_url, dataset) -> same lock, not a new one each call
    assert a1 is not b  # different dataset, same bank -- must not share a lock
    assert a1 is not c  # different bank/base_url, same dataset name -- ditto


async def test_concurrent_writes_to_same_target_never_interleave(monkeypatch):
    """Two concurrent write_turtle_to_graph calls against the same
    (base_url, dataset) must not have their validate-and-write critical
    sections overlap, even though both do real ``await``s in between."""
    _bypass_acl(monkeypatch)
    conn = _conn()
    client = RecordingClient()
    events: list[str] = []

    async def fake_validate_delta(client_, conn_, *, turtle, shapes_graph,
                                   target_graph, write_mode, reduction_rule_id):
        events.append(f"{turtle}-validate-start")
        await asyncio.sleep(0)  # a real validate does real awaits against Fuseki
        events.append(f"{turtle}-validate-end")
        return ValidationReport(conforms=True, mode="delta")

    monkeypatch.setattr(shacl_mod, "validate_delta", fake_validate_delta)

    orig_post_graph = client.post_graph

    async def post_graph(conn_, graph_iri, turtle):
        events.append(f"{turtle}-write-start")
        await asyncio.sleep(0)
        await orig_post_graph(conn_, graph_iri, turtle)
        events.append(f"{turtle}-write-end")

    client.post_graph = post_graph

    async def call(label: str) -> None:
        await ingest.write_turtle_to_graph(
            turtle=label,
            graph_iri="urn:ds:holons",
            mode="merge",
            shapes_graph="urn:ds:shacl",
            reduction_rule_id=None,
            conn=conn,
            client=client,
            shacl_required=True,
            shacl_delta=True,
            animus=_animus(),
        )

    await asyncio.gather(call("A"), call("B"))

    # Each label's four markers (validate-start/end, write-start/end) must
    # sit together with no interleaving from the other call.
    for label in ("A", "B"):
        idxs = [i for i, e in enumerate(events) if e.startswith(f"{label}-")]
        assert idxs == list(range(idxs[0], idxs[0] + len(idxs))), events

    assert len(client.posted) == 2


async def test_concurrent_writes_to_different_datasets_are_not_serialised(monkeypatch):
    """The lock is per (base_url, dataset) -- two calls against genuinely
    different targets must not block on each other."""
    _bypass_acl(monkeypatch)
    client = RecordingClient()
    events: list[str] = []
    started = asyncio.Event()

    async def fake_validate_delta(client_, conn_, *, turtle, shapes_graph,
                                   target_graph, write_mode, reduction_rule_id):
        events.append(f"{conn_.dataset}-start")
        if conn_.dataset == "x":
            started.set()
        else:
            # "y" waits for "x" to have started -- if the two datasets were
            # (wrongly) sharing a lock, this would deadlock the test, which
            # is exactly the signal we want: an unrelated-dataset write must
            # never be blocked by one already in progress elsewhere.
            await asyncio.wait_for(started.wait(), timeout=1)
        events.append(f"{conn_.dataset}-end")
        return ValidationReport(conforms=True, mode="delta")

    monkeypatch.setattr(shacl_mod, "validate_delta", fake_validate_delta)

    async def call(dataset: str) -> None:
        await ingest.write_turtle_to_graph(
            turtle="t",
            graph_iri=f"urn:{dataset}:holons",
            mode="merge",
            shapes_graph=f"urn:{dataset}:shacl",
            reduction_rule_id=None,
            conn=_conn(dataset=dataset),
            client=client,
            shacl_required=True,
            shacl_delta=True,
            animus=_animus(),
        )

    await asyncio.wait_for(asyncio.gather(call("y"), call("x")), timeout=2)

    assert {"x-start", "x-end", "y-start", "y-end"} == set(events)
