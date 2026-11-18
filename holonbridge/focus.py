"""Focus: which avatar a caller reads as, and where that avatar is.

A focus is an *avatar* and, optionally, a *time*. The place is not stored: it is read from
the avatar's own focus fluents as of that time (see ``docs/architecture/holon-focus.databook.md``
for the design and the positions behind it).

* **Avatar** (``holon:Avatar``) is a role asserted of any entity that represents a point of
  view: a person, persona, account, company or character. It need not be a holon.
* **Focus property** (``holon:FocusProperty``, declared in the ontology graph) marks a fluent
  property that locates an avatar. ``holon:focusWithin`` says one is read within the holon
  that another names, so presence can come before location.
* **Default avatar** is ``?holon holon:defaultAvatar ?avatar`` in the holons graph.

An avatar holds its focus fluents the way any holder does: ``<avatar> ?link <fluent>`` in the
holons graph, with ``<fluent> holon:fluentProperty <focus property>``.

Setting a focus is **observer state, not a ledger event**. It is saved per person and dataset
in :class:`FocusStore` (the same shape as ``PersonaStore``, for the same reason: the MCP layer
is process-global and cannot tell two callers apart) and writes nothing to any graph.

Not covered: several simultaneous avatars per caller; a transaction-time bound on the read
(``inserted_at``); an avatar that holds no fluent of its own because a container holds it for
it (this reads fluents held by the avatar itself).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from .conn import Conn
from .fluent import HEVT_NS, HOLON_NS
from .fuseki import FusekiClient
from .lifecycle import HB, LIFECYCLE_PROPERTY, TOMBSTONED, read_state, state_name
from .succession import resolve_successor

log = logging.getLogger(__name__)

AVATAR = f"{HOLON_NS}Avatar"
FOCUS_PROPERTY = f"{HOLON_NS}FocusProperty"
FOCUS_WITHIN = f"{HOLON_NS}focusWithin"
DEFAULT_AVATAR = f"{HOLON_NS}defaultAvatar"

#: How many scales a focus path may descend. A longer chain is cut, not an error.
MAX_DEPTH: Final = 8

_PREFIXES = f"""PREFIX holon: <{HOLON_NS}>
PREFIX hevt:  <{HEVT_NS}>
PREFIX hb:    <{HB}>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
"""


class FocusError(RuntimeError):
    """A focus could not be set or resolved. ``code`` is machine-readable; ``status`` is the
    HTTP status a route should answer with."""

    def __init__(self, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --- saved state ---------------------------------------------------------------------------

_DEFAULT_STATE_FILE: Final = Path.home() / ".holonbridge" / "focus-state.json"


class FocusStore:
    """Each person's saved focus for each dataset. Loads once, writes through on every change.

    One instance per process, on ``app.state.focus``. It is keyed by the resolved Person and
    the dataset, so one caller can never move another's focus, nor their own focus in a
    different dataset. A failed write is logged and does not stop the change taking effect for
    this process.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path(
            os.getenv("HOLONBRIDGE_FOCUS_STATE_FILE", "").strip() or str(_DEFAULT_STATE_FILE)
        )
        self._by_person: dict[str, dict[str, dict[str, str | None]]] = self._load()

    def _load(self) -> dict[str, dict[str, dict[str, str | None]]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        by_person = raw.get("focusByPerson", {})
        if not isinstance(by_person, dict):
            return {}
        return {
            person: {ds: dict(entry) for ds, entry in datasets.items() if isinstance(entry, dict)}
            for person, datasets in by_person.items()
            if isinstance(datasets, dict)
        }

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "focusByPerson": self._by_person,
                "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning(
                "could not persist focus state to %s: %s (the change is in effect for this "
                "process but will not survive a restart)",
                self._path,
                exc,
            )

    def get(self, *, person_id: str, dataset: str) -> tuple[str | None, str | None]:
        """``(avatar, as_of)`` this person has saved for this dataset; ``None`` for each that is not set."""
        entry = self._by_person.get(person_id, {}).get(dataset, {})
        return entry.get("avatar") or None, entry.get("asOf") or None

    def set(self, *, person_id: str, dataset: str, avatar: str | None, as_of: str | None) -> None:
        """Replace this person's saved focus for this dataset. With neither set, clear it."""
        datasets = self._by_person.setdefault(person_id, {})
        if avatar or as_of:
            datasets[dataset] = {"avatar": avatar or None, "asOf": as_of or None}
        else:
            datasets.pop(dataset, None)
            if not datasets:
                self._by_person.pop(person_id, None)
        self._save()


# --- results -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One focus property and its value, as of the focus time."""

    property: str
    value: str
    is_iri: bool

    def as_dict(self) -> dict[str, Any]:
        return {"property": self.property, "value": self.value, "isIri": self.is_iri}


@dataclass(frozen=True)
class Avatar:
    iri: str
    label: str | None
    state: str  # Active, Inactive or Tombstoned
    is_default: bool

    def as_dict(self) -> dict[str, Any]:
        return {"avatar": self.iri, "label": self.label, "state": self.state, "default": self.is_default}


@dataclass(frozen=True)
class Focus:
    avatar: str  # the avatar the focus follows (after any succession)
    source: str  # explicit, saved or default
    as_of: str | None  # None means now
    paths: list[list[Step]] = field(default_factory=list)
    state: str = "Active"
    requested: str | None = None  # the avatar that was asked for, when succession redirected it
    default_avatar: str | None = None

    @property
    def holon(self) -> str | None:
        """The value of the first path's first step: normally the holon the avatar is in."""
        return self.paths[0][0].value if self.paths else None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": True,
            "avatar": self.avatar,
            "source": self.source,
            "asOf": self.as_of,
            "holon": self.holon,
            "paths": [[s.as_dict() for s in path] for path in self.paths],
            "avatarState": self.state,
            "defaultAvatar": self.default_avatar,
        }
        if self.requested and self.requested != self.avatar:
            out["redirectedFrom"] = self.requested
        return out


# --- reading ---------------------------------------------------------------------------------


def _bindings(result: dict) -> list[dict]:
    return result.get("results", {}).get("bindings", [])


def normalise_as_of(value: datetime | str | None) -> str | None:
    """An ISO 8601 UTC timestamp, or ``None`` for "now". A time with no zone is read as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise FocusError("invalid_as_of", f"as_of {value!r} is not an ISO 8601 time", status=400) from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


async def default_avatar(client: FusekiClient, conn: Conn) -> str | None:
    """The dataset's default avatar, ``None`` if it names none. More than one is refused:
    guessing which holon's default applies would pick someone's point of view for the caller."""
    query = f"""{_PREFIXES}
SELECT DISTINCT ?avatar WHERE {{ GRAPH <{conn.holons_graph}> {{ ?holon <{DEFAULT_AVATAR}> ?avatar . }} }}
ORDER BY ?avatar"""
    found = [r["avatar"]["value"] for r in _bindings(await client.select(conn, query))]
    if len(found) > 1:
        raise FocusError(
            "ambiguous_default",
            f"{len(found)} different default avatars are declared ({', '.join(found)}); "
            "name one with set_focus",
        )
    return found[0] if found else None


async def resolve_successor_of(client: FusekiClient, conn: Conn, avatar: str) -> str:
    """The avatar a (possibly succeeded) avatar IRI stands for now."""
    return await resolve_successor(client, conn, avatar)


async def is_avatar(client: FusekiClient, conn: Conn, iri: str) -> bool:
    query = f"SELECT ?a WHERE {{ GRAPH <{conn.holons_graph}> {{ <{iri}> a <{AVATAR}> . BIND(1 AS ?a) }} }} LIMIT 1"
    return bool(_bindings(await client.select(conn, query)))


async def list_avatars(client: FusekiClient, conn: Conn, *, include_tombstoned: bool = False) -> list[Avatar]:
    """Every entity typed ``holon:Avatar``, with its lifecycle state and whether it is the default.
    A Tombstoned avatar is left out unless asked for, as in any discovery."""
    query = f"""{_PREFIXES}
SELECT ?avatar ?label ?state WHERE {{
  GRAPH <{conn.holons_graph}> {{
    ?avatar a <{AVATAR}> .
    OPTIONAL {{ ?avatar rdfs:label ?label . }}
    OPTIONAL {{
      ?avatar <{LIFECYCLE_PROPERTY}> ?lifecycle .
      GRAPH <{conn.scene_graph}> {{ ?lifecycle holon:currentValue ?state . }}
    }}
  }}
}} ORDER BY ?avatar"""
    default = await default_avatar(client, conn)
    seen: dict[str, Avatar] = {}
    for row in _bindings(await client.select(conn, query)):
        iri = row["avatar"]["value"]
        state = state_name(row.get("state", {}).get("value"))
        if iri in seen:
            continue
        if state == state_name(TOMBSTONED) and not include_tombstoned:
            continue
        seen[iri] = Avatar(iri, row.get("label", {}).get("value"), state, iri == default)
    return list(seen.values())


async def _focus_fluents(client: FusekiClient, conn: Conn, avatar: str) -> list[dict]:
    """The avatar's fluents whose property is a focus property, with each property's parent."""
    query = f"""{_PREFIXES}
SELECT DISTINCT ?fluent ?prop ?up WHERE {{
  GRAPH <{conn.holons_graph}> {{
    <{avatar}> ?link ?fluent .
    ?fluent holon:fluentProperty ?prop .
  }}
  GRAPH <{conn.ontology_graph}> {{
    ?prop a <{FOCUS_PROPERTY}> .
    OPTIONAL {{ ?prop <{FOCUS_WITHIN}> ?up . }}
  }}
}} ORDER BY ?prop ?fluent"""
    return [
        {
            "fluent": r["fluent"]["value"],
            "prop": r["prop"]["value"],
            "up": r["up"]["value"] if "up" in r else None,
        }
        for r in _bindings(await client.select(conn, query))
    ]


async def _value_of(client: FusekiClient, conn: Conn, fluent: str, as_of: str | None) -> Step | None:
    """The fluent's value now (from the scene) or as of ``as_of`` (from the ledger)."""
    if as_of is None:
        query = f"SELECT ?value WHERE {{ GRAPH <{conn.scene_graph}> {{ <{fluent}> <{HOLON_NS}currentValue> ?value . }} }} LIMIT 1"
    else:
        stamp = f'"{as_of}"^^xsd:dateTime'
        query = f"""{_PREFIXES}
SELECT ?value WHERE {{
  GRAPH <{conn.graph("events")}> {{
    ?assertion hevt:forFluent <{fluent}> ; hevt:hasValue ?value ; hevt:assertedDateTime ?assertedAt .
    FILTER (?assertedAt <= {stamp})
    FILTER NOT EXISTS {{ ?assertion hevt:invalidatedAt ?invalidatedAt . FILTER (?invalidatedAt <= {stamp}) }}
    FILTER NOT EXISTS {{
      ?later hevt:forFluent <{fluent}> ; hevt:assertedDateTime ?laterAt .
      FILTER (?laterAt <= {stamp} && ?laterAt > ?assertedAt)
    }}
  }}
}} LIMIT 1"""
    rows = _bindings(await client.select(conn, query))
    if not rows:
        return None
    value = rows[0]["value"]
    return Step(property="", value=value["value"], is_iri=value.get("type") == "uri")


def _walk(by_parent: dict[str | None, list[tuple[str, Step]]], trail: list[Step], prop: str, depth: int, out: list[list[Step]]) -> None:
    children = [(p, s) for p, s in by_parent.get(prop, []) if p not in {t.property for t in trail}]
    if not children or depth >= MAX_DEPTH:
        out.append(trail)
        return
    for child_prop, step in children:
        _walk(by_parent, [*trail, step], child_prop, depth + 1, out)


async def pick_avatar(
    client: FusekiClient, conn: Conn, *, explicit: str | None, saved: str | None
) -> tuple[str, str]:
    """``(avatar, source)``: what the caller named, else what they saved, else the default."""
    if explicit:
        return explicit, "explicit"
    if saved:
        return saved, "saved"
    default = await default_avatar(client, conn)
    if default:
        return default, "default"
    raise FocusError(
        "no_avatar",
        "no avatar was named, none is saved, and the dataset declares no holon:defaultAvatar; "
        "call list_avatars, then set_focus",
    )


async def resolve_focus(
    client: FusekiClient, conn: Conn, *, avatar: str, source: str, as_of: str | None
) -> Focus:
    """Resolve ``avatar``'s focus path as of ``as_of`` (``None`` is now).

    An avatar that has been succeeded is followed to the end of its chain. A holon the avatar
    is in that is Inactive or Tombstoned is not a problem: position is read, not changed.
    """
    target = await resolve_successor(client, conn, avatar)
    if not await is_avatar(client, conn, target):
        raise FocusError("not_an_avatar", f"<{target}> is not typed holon:Avatar in this dataset")

    fluents = await _focus_fluents(client, conn, target)
    by_parent: dict[str | None, list[tuple[str, Step]]] = {}
    for item in fluents:
        step = await _value_of(client, conn, item["fluent"], as_of)
        if step is None:
            continue
        by_parent.setdefault(item["up"], []).append(
            (item["prop"], Step(property=item["prop"], value=step.value, is_iri=step.is_iri))
        )

    paths: list[list[Step]] = []
    for root_prop, root_step in by_parent.get(None, []):
        _walk(by_parent, [root_step], root_prop, 1, paths)
    if not paths:
        when = "now" if as_of is None else f"as of {as_of}"
        raise FocusError(
            "no_focus",
            f"<{target}> has no value for any root focus property {when}, so there is no place to resolve",
        )

    state = (await read_state(client, conn, target)).state
    return Focus(
        avatar=target,
        source=source,
        as_of=as_of,
        paths=paths,
        state=state,
        requested=avatar,
        default_avatar=await default_avatar(client, conn),
    )
