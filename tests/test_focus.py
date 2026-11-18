"""Focus: an avatar, an optional time, and the place read from where the avatar is.

Real resolver, store and routes against rdflib's SPARQL engine; only identity is stubbed.
The world is the Elizabeth Line scenario in miniature:

    Jane (default avatar)   --inHolon-->   F1  now: Train   (before 09:00: Platform)
                            --inCarriage-> F2  now: Carriage1   (focusWithin inHolon)
    Bob  (avatar)           --inHolon-->   F3  now: Platform
    Ghost (typed Avatar, Tombstoned);  Old (Avatar, succeeded by Jane)
    Pat  (not an avatar, holds a focus fluent)
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

pytest.importorskip("pyshacl")

from holonbridge import focus as fc
from holonbridge import lifecycle as lc
from holonbridge.routes import focus as routes

from .test_fluent_ledger_race import ALICE, HEVT, HOLON
from .test_lifecycle import Store, _add, _world  # noqa: F401  (Store used via _world)

EX = "urn:ex:"
JANE, BOB, GHOST, OLD, PAT = (f"{EX}{n}" for n in ("jane", "bob", "ghost", "old", "pat"))
TRAIN, PLATFORM, CARRIAGE = f"{EX}train", f"{EX}platform", f"{EX}carriage1"
IN_HOLON, IN_CARRIAGE, NOT_FOCUS = f"{EX}inHolon", f"{EX}inCarriage", f"{EX}mood"
F1, F2, F3, F4 = (f"{EX}f{n}" for n in range(1, 5))


def _stamp(text: str) -> Literal:
    return Literal(text, datatype=XSD.dateTime)


def _fluent(holons, scene, events, holder, link, fluent, prop, value, history=()):
    _add(holons, fluent, RDF.type, f"{HOLON}Fluent")
    _add(holons, fluent, f"{HOLON}fluentProperty", prop)
    _add(holons, holder, link, fluent)
    _add(scene, fluent, f"{HOLON}currentValue", URIRef(value))
    for n, (when, val) in enumerate([*history, ("2026-01-01T12:00:00+00:00", value)]):
        a = f"{fluent}-a{n}"
        _add(events, a, f"{HEVT}forFluent", fluent)
        _add(events, a, f"{HEVT}hasValue", URIRef(val))
        _add(events, a, f"{HEVT}assertedDateTime", _stamp(when))


def _focus_world():
    conn, store = _world()
    holons = store.ds.graph(URIRef(conn.holons_graph))
    onto = store.ds.graph(URIRef(conn.ontology_graph))
    scene = store.ds.graph(URIRef(conn.scene_graph))
    events = store.ds.graph(URIRef(conn.graph("events")))

    _add(onto, IN_HOLON, RDF.type, fc.FOCUS_PROPERTY)
    _add(onto, IN_CARRIAGE, RDF.type, fc.FOCUS_PROPERTY)
    _add(onto, IN_CARRIAGE, fc.FOCUS_WITHIN, IN_HOLON)

    for avatar, label in ((JANE, "Jane"), (BOB, "Bob"), (GHOST, "Ghost"), (OLD, "Old")):
        _add(holons, avatar, RDF.type, fc.AVATAR)
        _add(holons, avatar, RDFS.label, Literal(label))
    _add(holons, PAT, RDFS.label, Literal("Pat"))
    _add(holons, EX + "elizabeth-line", fc.DEFAULT_AVATAR, JANE)

    link = f"{EX}has"
    _fluent(holons, scene, events, JANE, link, F1, IN_HOLON, TRAIN,
            history=[("2026-01-01T08:00:00+00:00", PLATFORM)])
    _fluent(holons, scene, events, JANE, link, F2, IN_CARRIAGE, CARRIAGE)
    _fluent(holons, scene, events, BOB, link, F3, IN_HOLON, PLATFORM)
    _fluent(holons, scene, events, PAT, link, F4, IN_HOLON, TRAIN)
    return conn, store


async def _resolve(conn, store, avatar=JANE, as_of=None, source="explicit"):
    return await fc.resolve_focus(store, conn, avatar=avatar, source=source, as_of=as_of)


# --- resolution -------------------------------------------------------------------


async def test_the_place_is_read_from_the_avatars_focus_fluents_and_nests_by_focus_within():
    conn, store = _focus_world()
    focus = await _resolve(conn, store)
    assert [[(s.property, s.value) for s in path] for path in focus.paths] == [
        [(IN_HOLON, TRAIN), (IN_CARRIAGE, CARRIAGE)]
    ]
    assert focus.holon == TRAIN and focus.state == "Active"


async def test_a_property_that_is_not_a_focus_property_is_not_part_of_the_place():
    conn, store = _focus_world()
    holons = store.ds.graph(URIRef(conn.holons_graph))
    scene = store.ds.graph(URIRef(conn.scene_graph))
    _add(holons, "urn:ex:f-mood", f"{HOLON}fluentProperty", NOT_FOCUS)
    _add(holons, JANE, f"{EX}has", "urn:ex:f-mood")
    _add(scene, "urn:ex:f-mood", f"{HOLON}currentValue", URIRef(PLATFORM))
    focus = await _resolve(conn, store)
    assert PLATFORM not in {s.value for p in focus.paths for s in p}


async def test_a_child_with_no_value_leaves_the_place_at_the_parent():
    conn, store = _focus_world()
    scene = store.ds.graph(URIRef(conn.scene_graph))
    scene.remove((URIRef(F2), URIRef(f"{HOLON}currentValue"), None))
    focus = await _resolve(conn, store)
    assert [[s.value for s in p] for p in focus.paths] == [[TRAIN]]


async def test_an_avatar_with_no_root_value_has_no_focus():
    conn, store = _focus_world()
    scene = store.ds.graph(URIRef(conn.scene_graph))
    scene.remove((URIRef(F1), URIRef(f"{HOLON}currentValue"), None))
    with pytest.raises(fc.FocusError) as err:
        await _resolve(conn, store)
    assert err.value.code == "no_focus"


async def test_a_focus_within_cycle_does_not_loop():
    conn, store = _focus_world()
    onto = store.ds.graph(URIRef(conn.ontology_graph))
    _add(onto, IN_HOLON, fc.FOCUS_WITHIN, IN_CARRIAGE)  # now a cycle; no root property
    with pytest.raises(fc.FocusError) as err:
        await _resolve(conn, store)
    assert err.value.code == "no_focus"


async def test_something_not_typed_avatar_cannot_be_a_focus():
    conn, store = _focus_world()
    with pytest.raises(fc.FocusError) as err:
        await _resolve(conn, store, avatar=PAT)
    assert err.value.code == "not_an_avatar" and err.value.status == 409


# --- time ---------------------------------------------------------------------------


async def test_as_of_reads_the_ledger_and_now_reads_the_scene():
    conn, store = _focus_world()
    earlier = await _resolve(conn, store, as_of="2026-01-01T09:00:00+00:00")
    # F1 was the Platform at 09:00; F2 had no value yet, so the place stops at the parent.
    assert [[s.value for s in p] for p in earlier.paths] == [[PLATFORM]]
    assert earlier.as_of == "2026-01-01T09:00:00+00:00"

    later = await _resolve(conn, store, as_of="2026-01-01T13:00:00+00:00")
    assert [[s.value for s in p] for p in later.paths] == [[TRAIN, CARRIAGE]]

    now = await _resolve(conn, store)
    assert now.as_of is None and now.holon == TRAIN


async def test_an_invalidated_assertion_is_not_read_and_does_not_revive_the_older_one():
    """Same reading as ``holon.as_of_query``: an invalidated assertion drops out, and the
    older one stays superseded by it, so the fluent has no value at that time."""
    conn, store = _focus_world()
    events = store.ds.graph(URIRef(conn.graph("events")))
    _add(events, f"{F1}-a1", f"{HEVT}invalidatedAt", _stamp("2026-01-01T12:30:00+00:00"))
    with pytest.raises(fc.FocusError) as err:
        await _resolve(conn, store, as_of="2026-01-01T13:00:00+00:00")
    assert err.value.code == "no_focus"
    # before the invalidation took effect it still counts
    assert (await _resolve(conn, store, as_of="2026-01-01T12:15:00+00:00")).holon == TRAIN


def test_as_of_is_normalised_to_utc_and_bad_input_is_a_400():
    assert fc.normalise_as_of(None) is None and fc.normalise_as_of("") is None
    assert fc.normalise_as_of("2026-01-01T09:00:00Z") == "2026-01-01T09:00:00+00:00"
    assert fc.normalise_as_of("2026-01-01T10:00:00+01:00") == "2026-01-01T09:00:00+00:00"
    assert fc.normalise_as_of("2026-01-01T09:00:00") == "2026-01-01T09:00:00+00:00"
    with pytest.raises(fc.FocusError) as err:
        fc.normalise_as_of("tomorrow-ish")
    assert err.value.code == "invalid_as_of" and err.value.status == 400


# --- which avatar ---------------------------------------------------------------------


async def test_avatar_precedence_is_explicit_then_saved_then_default():
    conn, store = _focus_world()
    pick = lambda **kw: fc.pick_avatar(store, conn, **kw)
    assert await pick(explicit=BOB, saved=JANE) == (BOB, "explicit")
    assert await pick(explicit=None, saved=BOB) == (BOB, "saved")
    assert await pick(explicit=None, saved=None) == (JANE, "default")


async def test_with_no_default_and_nothing_named_it_refuses_rather_than_guess():
    conn, store = _focus_world()
    store.ds.graph(URIRef(conn.holons_graph)).remove((None, URIRef(fc.DEFAULT_AVATAR), None))
    with pytest.raises(fc.FocusError) as err:
        await fc.pick_avatar(store, conn, explicit=None, saved=None)
    assert err.value.code == "no_avatar"


async def test_two_different_defaults_are_refused_not_chosen_between():
    conn, store = _focus_world()
    _add(store.ds.graph(URIRef(conn.holons_graph)), EX + "other-line", fc.DEFAULT_AVATAR, BOB)
    with pytest.raises(fc.FocusError) as err:
        await fc.default_avatar(store, conn)
    assert err.value.code == "ambiguous_default"


async def test_the_same_default_declared_twice_is_not_ambiguous():
    conn, store = _focus_world()
    _add(store.ds.graph(URIRef(conn.holons_graph)), EX + "other-line", fc.DEFAULT_AVATAR, JANE)
    assert await fc.default_avatar(store, conn) == JANE


# --- avatars and lifecycle ----------------------------------------------------------------


async def test_list_avatars_flags_the_default_and_hides_the_tombstoned():
    conn, store = _focus_world()
    await lc.set_state(store, conn, holon=GHOST, state="Tombstoned", asserted_by=ALICE)
    await lc.set_state(store, conn, holon=BOB, state="Inactive", asserted_by=ALICE)

    found = {a.iri: a for a in await fc.list_avatars(store, conn)}
    assert set(found) == {JANE, BOB, OLD}  # Pat is not typed Avatar; Ghost is tombstoned
    assert found[JANE].is_default and not found[BOB].is_default
    assert found[BOB].state == "Inactive" and found[JANE].state == "Active"

    with_all = {a.iri for a in await fc.list_avatars(store, conn, include_tombstoned=True)}
    assert GHOST in with_all


async def test_a_succeeded_avatar_resolves_through_to_its_successor():
    conn, store = _focus_world()
    _add(store.ds.graph(URIRef(conn.holons_graph)), OLD, lc.SUCCEEDED_BY, JANE)
    focus = await _resolve(conn, store, avatar=OLD)
    assert focus.avatar == JANE and focus.requested == OLD
    assert focus.as_dict()["redirectedFrom"] == OLD


async def test_an_inactive_avatar_can_still_be_read_as():
    conn, store = _focus_world()
    await lc.set_state(store, conn, holon=BOB, state="Inactive", asserted_by=ALICE)
    focus = await _resolve(conn, store, avatar=BOB)
    assert focus.state == "Inactive" and focus.holon == PLATFORM


# --- saved state -----------------------------------------------------------------------------


def test_the_store_is_per_person_and_dataset_and_survives_a_restart(tmp_path):
    path = tmp_path / "focus.json"
    a = fc.FocusStore(path)
    a.set(person_id="p1", dataset="d1", avatar=JANE, as_of="2026-01-01T09:00:00+00:00")
    a.set(person_id="p2", dataset="d1", avatar=BOB, as_of=None)
    assert a.get(person_id="p1", dataset="d1") == (JANE, "2026-01-01T09:00:00+00:00")
    assert a.get(person_id="p1", dataset="d2") == (None, None)
    assert a.get(person_id="p2", dataset="d1") == (BOB, None)

    b = fc.FocusStore(path)  # a fresh process
    assert b.get(person_id="p1", dataset="d1") == (JANE, "2026-01-01T09:00:00+00:00")

    b.set(person_id="p1", dataset="d1", avatar=None, as_of=None)  # clear
    assert fc.FocusStore(path).get(person_id="p1", dataset="d1") == (None, None)
    assert "p1" not in json.loads(path.read_text())["focusByPerson"]


def test_a_corrupt_or_unwritable_store_does_not_stop_focus_working(tmp_path):
    bad = tmp_path / "focus.json"
    bad.write_text("{not json")
    store = fc.FocusStore(bad)
    assert store.get(person_id="p", dataset="d") == (None, None)

    blocked = fc.FocusStore(tmp_path / "file" / "x.json")
    (tmp_path / "file").write_text("a file where a directory should be")
    blocked.set(person_id="p", dataset="d", avatar=JANE, as_of=None)  # logs, does not raise
    assert blocked.get(person_id="p", dataset="d") == (JANE, None)


# --- routes --------------------------------------------------------------------------------------


def _ctx(store, conn, tmp_path, person=ALICE):
    return {
        "conn": conn,
        "client": store,
        "animus": SimpleNamespace(person=person),
        "store": fc.FocusStore(tmp_path / "focus.json"),
    }


async def _get(avatar="", as_of="", **ctx):
    """``routes.get_focus`` called directly, so its ``Query`` defaults are given as values."""
    return await routes.get_focus(avatar=avatar, as_of=as_of, **ctx)


async def test_get_focus_with_nothing_saved_uses_the_default_avatar(tmp_path):
    conn, store = _focus_world()
    out = await _get(**_ctx(store, conn, tmp_path))
    assert out["avatar"] == JANE and out["source"] == "default" and out["holon"] == TRAIN
    assert out["defaultAvatar"] == JANE and out["asOf"] is None


async def test_set_focus_saves_it_and_get_focus_then_follows_it(tmp_path):
    conn, store = _focus_world()
    ctx = _ctx(store, conn, tmp_path)
    saved = await routes.set_focus(routes.SetFocusRequest(avatar=BOB), **ctx)
    assert saved["avatar"] == BOB and saved["holon"] == PLATFORM

    out = await _get(**ctx)
    assert out["avatar"] == BOB and out["source"] == "saved"

    peek = await _get(avatar=JANE, **ctx)  # an override, not saved
    assert peek["avatar"] == JANE and peek["source"] == "explicit"
    assert (await _get(**ctx))["avatar"] == BOB


async def test_a_saved_time_is_read_at_each_call_and_an_explicit_one_wins(tmp_path):
    conn, store = _focus_world()
    ctx = _ctx(store, conn, tmp_path)
    await routes.set_focus(routes.SetFocusRequest(as_of="2026-01-01T09:00:00Z"), **ctx)
    assert (await _get(**ctx))["holon"] == PLATFORM
    assert (await _get(as_of="2026-01-01T13:00:00Z", **ctx))["holon"] == TRAIN


async def test_set_focus_replaces_rather_than_merges(tmp_path):
    conn, store = _focus_world()
    ctx = _ctx(store, conn, tmp_path)
    await routes.set_focus(routes.SetFocusRequest(avatar=BOB, as_of="2026-01-01T13:00:00Z"), **ctx)
    await routes.set_focus(routes.SetFocusRequest(avatar=JANE), **ctx)
    assert ctx["store"].get(person_id=ALICE, dataset=conn.dataset) == (JANE, None)


async def test_both_fields_empty_clears_the_saved_focus(tmp_path):
    conn, store = _focus_world()
    ctx = _ctx(store, conn, tmp_path)
    await routes.set_focus(routes.SetFocusRequest(avatar=BOB), **ctx)
    cleared = await routes.set_focus(routes.SetFocusRequest(), **ctx)
    assert cleared["cleared"] is True
    assert (await _get(**ctx))["avatar"] == JANE


@pytest.mark.parametrize(
    ("avatar", "code", "status"),
    [(PAT, "not_an_avatar", 409), (GHOST, "avatar_tombstoned", 409), ("not an iri", None, 400)],
)
async def test_set_focus_refuses_what_cannot_be_a_focus_and_saves_nothing(tmp_path, avatar, code, status):
    conn, store = _focus_world()
    await lc.set_state(store, conn, holon=GHOST, state="Tombstoned", asserted_by=ALICE)
    ctx = _ctx(store, conn, tmp_path)
    with pytest.raises(HTTPException) as err:
        await routes.set_focus(routes.SetFocusRequest(avatar=avatar), **ctx)
    assert err.value.status_code == status
    if code:
        assert err.value.detail["error"] == code
    assert ctx["store"].get(person_id=ALICE, dataset=conn.dataset) == (None, None)


async def test_set_focus_will_not_save_an_avatar_that_has_no_place(tmp_path):
    conn, store = _focus_world()
    store.ds.graph(URIRef(conn.scene_graph)).remove((URIRef(F3), URIRef(f"{HOLON}currentValue"), None))
    ctx = _ctx(store, conn, tmp_path)
    with pytest.raises(HTTPException) as err:
        await routes.set_focus(routes.SetFocusRequest(avatar=BOB), **ctx)
    assert err.value.detail["error"] == "no_focus"
    assert ctx["store"].get(person_id=ALICE, dataset=conn.dataset) == (None, None)


async def test_an_inactive_avatar_can_be_saved_as_the_focus(tmp_path):
    conn, store = _focus_world()
    await lc.set_state(store, conn, holon=BOB, state="Inactive", asserted_by=ALICE)
    out = await routes.set_focus(routes.SetFocusRequest(avatar=BOB), **_ctx(store, conn, tmp_path))
    assert out["avatarState"] == "Inactive"


async def test_a_credential_with_no_person_is_a_401(tmp_path):
    conn, store = _focus_world()
    with pytest.raises(HTTPException) as err:
        await _get(**_ctx(store, conn, tmp_path, person=None))
    assert err.value.status_code == 401


async def test_one_callers_focus_cannot_move_anothers(tmp_path):
    conn, store = _focus_world()
    shared = fc.FocusStore(tmp_path / "focus.json")
    mk = lambda who: {"conn": conn, "client": store, "animus": SimpleNamespace(person=who), "store": shared}
    await routes.set_focus(routes.SetFocusRequest(avatar=BOB), **mk("urn:person:a"))
    assert (await _get(**mk("urn:person:b")))["avatar"] == JANE


async def test_list_avatars_route(tmp_path):
    conn, store = _focus_world()
    out = await routes.list_avatars(
        conn=conn, client=store, animus=SimpleNamespace(person=ALICE), include_tombstoned=True
    )
    assert out["count"] == 4 and {a["avatar"] for a in out["avatars"]} == {JANE, BOB, GHOST, OLD}
    assert [a["avatar"] for a in out["avatars"] if a["default"]] == [JANE]
