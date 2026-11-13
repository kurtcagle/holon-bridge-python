"""``get_holon``'s as-of path against real Jena Fuseki.

Skipped unless ``HOLONBRIDGE_TEST_FUSEKI_URL`` names a Fuseki server (for
example ``http://localhost:3031``) that has an updatable dataset called
``HOLONBRIDGE_TEST_FUSEKI_DATASET`` (default ``example``). Something like::

    fuseki-server --mem --update --port 3031 /example
    HOLONBRIDGE_TEST_FUSEKI_URL=http://localhost:3031 pytest tests/test_get_holon_asof_live.py

Why this exists alongside ``test_get_holon_asof.py``: rdflib mis-evaluates
the nested ``FILTER`` inside ``as_of_query``'s ``FILTER NOT EXISTS`` and
returns a superseded value next to the current one, so it cannot verify the
supersession logic. Jena can. The graphs this test loads are dropped again
afterwards; use a scratch dataset regardless.

Verified against Apache Jena Fuseki 5.5.0 on 2026-09-30; the transaction-time
cases at the end on 2026-11-13.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import httpx
import pytest

from holonbridge.conn import Conn
from holonbridge.fuseki import FusekiClient
from holonbridge.holon import AsOfUnsupportedError, get_holon
from holonbridge.persona_scope import resolve_scope_graphs

from .test_get_holon_asof import (
    ACCOUNT, CLUB, EVENTS, EVENTS_TTL, HOLONS, HOLONS_TTL, ONTOLOGY, ONTOLOGY_TTL, SCENE,
    SCENE_TTL,
)

URL = os.environ.get("HOLONBRIDGE_TEST_FUSEKI_URL", "").rstrip("/")
DATASET = os.environ.get("HOLONBRIDGE_TEST_FUSEKI_DATASET", "example")

pytestmark = pytest.mark.skipif(not URL, reason="HOLONBRIDGE_TEST_FUSEKI_URL not set")

GRAPHS = {HOLONS: HOLONS_TTL, ONTOLOGY: ONTOLOGY_TTL, EVENTS: EVENTS_TTL, SCENE: SCENE_TTL}


@pytest.fixture
async def live():
    conn = Conn(base_url=URL, dataset=DATASET, overridden=False, bank_name="local")
    async with httpx.AsyncClient() as http:
        for iri, ttl in GRAPHS.items():
            r = await http.put(
                f"{URL}/{DATASET}/data", params={"graph": iri}, content=ttl,
                headers={"Content-Type": "text/turtle"},
            )
            assert r.status_code in (200, 201, 204), r.text
        client = FusekiClient()
        try:
            yield client, conn
        finally:
            await client.aclose()
            for iri in GRAPHS:
                await http.delete(f"{URL}/{DATASET}/data", params={"graph": iri})


def _values(book) -> list[str]:
    body = next(b for b in book.blocks if b.id == "holon-state").body
    return sorted(re.findall(r"currentValue\s+(\d+)", body))


def _scope(conn):
    return resolve_scope_graphs(conn, person_id=None, persona=None)


async def test_supersession_returns_only_the_value_in_force(live):
    client, conn = live
    before = await get_holon(client, conn, holon_iri=ACCOUNT, scope=_scope(conn),
                             observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc))
    after = await get_holon(client, conn, holon_iri=ACCOUNT, scope=_scope(conn),
                            observed_at=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert _values(before) == ["100"]
    assert _values(after) == ["250"]  # not ["100", "250"]


@pytest.mark.parametrize("hour", [6, 13])
async def test_naive_timestamp_within_14h_is_not_silently_empty(live, hour):
    """Assertion 1 is at 2026-09-01T00:00:00+00:00. A naive timestamp within
    14 hours of it is an indeterminate SPARQL comparison, so without the UTC
    normalisation Fuseki returns no fluent value at all."""
    client, conn = live
    book = await get_holon(client, conn, holon_iri=ACCOUNT, scope=_scope(conn),
                           observed_at=datetime(2026, 9, 1, hour))
    assert _values(book) == ["100"]


async def test_list_fluent_guard_runs_on_jena(live):
    client, conn = live
    with pytest.raises(AsOfUnsupportedError):
        await get_holon(client, conn, holon_iri=CLUB, scope=_scope(conn),
                        observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc))


# --- transaction time (hevt:insertedAt) -----------------------------------------------------
#
# Added with the issue that asked for it: rdflib cannot check the "a later
# assertion was already known" clause of as_of_query (nested FILTER), so it is
# pinned here. event:1 is valid from 09-01 and event:2 (which supersedes it)
# from 09-10; event:2 was only recorded on 09-20.

INSERTED_TTL = """
@prefix hevt: <https://w3id.org/holon/event/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<urn:example:event:2> hevt:insertedAt "2026-09-20T00:00:00+00:00"^^xsd:dateTime .
"""
FIRST_INSERTED_TTL = """
@prefix hevt: <https://w3id.org/holon/event/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<urn:example:event:1> hevt:insertedAt "2026-09-01T00:00:00+00:00"^^xsd:dateTime .
"""


async def _add_to_events(extra: str) -> None:
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{URL}/{DATASET}/data", params={"graph": EVENTS}, content=extra,
            headers={"Content-Type": "text/turtle"},
        )
        assert r.status_code in (200, 201, 204), r.text


def _read(client, conn, *, observed, inserted=None):
    return get_holon(
        client, conn, holon_iri=ACCOUNT, scope=_scope(conn),
        observed_at=datetime(2026, 9, observed, tzinfo=timezone.utc),
        inserted_at=datetime(2026, 9, inserted, tzinfo=timezone.utc) if inserted else None,
    )


@pytest.mark.parametrize("first_has_insert_time", [False, True])
async def test_a_superseding_entry_not_yet_recorded_is_ignored(live, first_has_insert_time):
    """observed 09-15, so B (valid 09-10) is in force in the world. Asked as of
    09-12, the record had not yet got B, so A is still the answer."""
    client, conn = live
    await _add_to_events(INSERTED_TTL + (FIRST_INSERTED_TTL if first_has_insert_time else ""))
    assert _values(await _read(client, conn, observed=15, inserted=12)) == ["100"]


@pytest.mark.parametrize("first_has_insert_time", [False, True])
async def test_once_the_superseding_entry_is_recorded_it_wins(live, first_has_insert_time):
    client, conn = live
    await _add_to_events(INSERTED_TTL + (FIRST_INSERTED_TTL if first_has_insert_time else ""))
    assert _values(await _read(client, conn, observed=15, inserted=25)) == ["250"]


async def test_without_inserted_at_the_late_entry_still_wins(live):
    """No transaction-time bound: an ordinary valid-time read is unchanged."""
    client, conn = live
    await _add_to_events(INSERTED_TTL)
    assert _values(await _read(client, conn, observed=15)) == ["250"]


async def test_an_entry_recorded_after_the_asked_moment_is_not_shown_at_all(live):
    """The first assertion itself recorded late: nothing was known on 09-02."""
    client, conn = live
    await _add_to_events(
        INSERTED_TTL
        + """@prefix hevt: <https://w3id.org/holon/event/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<urn:example:event:1> hevt:insertedAt "2026-09-05T00:00:00+00:00"^^xsd:dateTime ."""
    )
    assert _values(await _read(client, conn, observed=3, inserted=2)) == []
    assert _values(await _read(client, conn, observed=3, inserted=6)) == ["100"]
