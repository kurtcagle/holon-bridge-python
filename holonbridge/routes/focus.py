"""get_focus, set_focus and list_avatars: which avatar the caller reads as, and when.

A focus is an avatar plus an optional time; the place is read from the avatar's focus
fluents (see ``focus.py``). Saved per person and dataset, like the persona override.

Setting a focus writes nothing to any graph, so it needs no write grant: authentication is
the whole gate. It can only ever change the caller's own saved focus, because the row is
keyed by ``animus.person`` and never by a request field.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import delegation as dg
from .. import focus as fc
from ..deps import AnimusDep, ClientDep, ConnDep, FocusDep
from ..fuseki import FusekiError
from ..lifecycle import TOMBSTONED, read_state, state_name

router = APIRouter(prefix="/focus", tags=["focus"])


def _safe(value: str, what: str) -> str:
    try:
        return dg.safe_iri(value)
    except dg.DelegationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{what}: {exc}") from exc


def _fail(exc: fc.FocusError) -> HTTPException:
    return HTTPException(exc.status, detail={"error": exc.code, "message": str(exc)})


def _person(animus) -> str:
    if animus.person is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"error": "no_person", "message": "this credential does not resolve to a Person"},
        )
    return animus.person


class SetFocusRequest(BaseModel):
    avatar: str = Field(default="", description="IRI of the avatar to read as; empty to use the default")
    as_of: str = Field(default="", description="ISO 8601 time to read at; empty for now")


@router.get("/avatars")
async def list_avatars(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    include_tombstoned: bool = Query(default=False),
) -> dict:
    """Every ``holon:Avatar`` in this dataset, with lifecycle state; the default is flagged."""
    _person(animus)
    try:
        found = await fc.list_avatars(client, conn, include_tombstoned=include_tombstoned)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    except fc.FocusError as exc:
        raise _fail(exc) from exc
    return {"ok": True, "count": len(found), "avatars": [a.as_dict() for a in found]}


@router.get("")
async def get_focus(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    store: FocusDep,
    avatar: str = Query(default="", description="Look through this avatar instead; not saved"),
    as_of: str = Query(default="", description="Read at this time instead; not saved"),
) -> dict:
    """Resolve the caller's focus: avatar (explicit, else saved, else the default), then the
    place that avatar is at as of the time (explicit, else saved, else now)."""
    person = _person(animus)
    saved_avatar, saved_as_of = store.get(person_id=person, dataset=conn.dataset)
    try:
        when = fc.normalise_as_of(as_of or saved_as_of)
        explicit = _safe(avatar.strip(), "avatar") if avatar.strip() else None
        who, source = await fc.pick_avatar(client, conn, explicit=explicit, saved=saved_avatar)
        focus = await fc.resolve_focus(client, conn, avatar=who, source=source, as_of=when)
    except fc.FocusError as exc:
        raise _fail(exc) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return focus.as_dict()


@router.post("")
async def set_focus(
    body: SetFocusRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    store: FocusDep,
) -> dict:
    """Save the caller's focus for this dataset, replacing whatever was saved.

    Both fields empty clears it (the dataset's default avatar, and now). Refused: an IRI that
    is not typed ``holon:Avatar``, an avatar that is Tombstoned, a focus that cannot be
    resolved. An Inactive avatar is allowed, because reading as a dormant avatar is how you
    look at what it saw. A succeeded avatar is saved as asked and followed at read time.
    """
    person = _person(animus)
    avatar = body.avatar.strip()
    try:
        when = fc.normalise_as_of(body.as_of.strip() or None)
        if not avatar and not when:
            store.set(person_id=person, dataset=conn.dataset, avatar=None, as_of=None)
            return {"ok": True, "cleared": True, "note": "focus cleared; the default avatar, now"}
        who = _safe(avatar, "avatar") if avatar else None
        if who:
            target = await fc.resolve_successor_of(client, conn, who)
            if state_name(TOMBSTONED) == (await read_state(client, conn, target)).state:
                raise fc.FocusError("avatar_tombstoned", f"<{target}> is Tombstoned and cannot be a focus")
        # Resolve before saving, so a focus that cannot work is never stored.
        picked, source = await fc.pick_avatar(client, conn, explicit=who, saved=None)
        focus = await fc.resolve_focus(client, conn, avatar=picked, source=source, as_of=when)
    except fc.FocusError as exc:
        raise _fail(exc) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    store.set(person_id=person, dataset=conn.dataset, avatar=who, as_of=when)
    return focus.as_dict()
