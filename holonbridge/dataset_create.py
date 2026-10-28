"""Creating a new dataset on the backend: ``POST /datasets``.

This is the one place the bridge allocates storage on the Fuseki server, so the
rules are written down here rather than left to the route:

* **Server-level gate, not dataset-level.** Admin on one dataset implies nothing
  about the server, so the gate is its own list, ``DATASET_CREATORS`` (empty by
  default: creation is off). A caller qualifies only with an *established*
  identity -- an identity-bound token, or a trusted proxy asserting one -- the
  same standard :mod:`holonbridge.claim` applies. The legacy shared token, where
  the identity is a header anyone can write, never qualifies.
* **Create only. There is no drop.** Nothing in the bridge can remove a dataset
  once made; that stays a deliberate manual step in Fuseki. The cost is that
  datasets accumulate, hence ``MAX_DATASETS``.
* **Name and kind only.** No assembler file is accepted or relayed (see
  :meth:`FusekiClient.create_dataset`).
* **Strict names.** Lower-case, letters/digits/hyphen/underscore, starting with
  a letter, no dots. The name becomes a URL segment, a directory and a
  configuration file name, and Windows folds case, so two names differing only
  by case would collide on disk. Existing names are compared case-insensitively
  for the same reason.
* **The creator becomes the owner** (opt out with ``claim=false``). A new
  dataset has no owner or admin, so without this anyone on ``DATASET_CLAIMANTS``
  could take it in the gap between creation and the creator's first call.

Two kinds, and the difference matters:

``persistent``  a TDB2 dataset on disk; survives a Fuseki restart.
``memory``      an in-memory dataset; its *contents* are lost on restart. Fuseki
                keeps the dataset's configuration for dynamically created
                datasets, so it is expected to reappear empty rather than vanish
                -- verify that on your own build before depending on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from .claim import Claimant, ClaimError, claim
from .config import Settings
from .conn import GRAPH_ROLES, Conn
from .fuseki import FusekiClient, FusekiError

Kind = Literal["persistent", "memory"]

_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

#: Names the bridge itself relies on. ``settings.scheduler_dataset`` is added at
#: call time because it is configurable.
RESERVED_NAMES: frozenset[str] = frozenset({"admin"})


class CreateError(Exception):
    """A creation that cannot proceed, with the HTTP status the route should use."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


@dataclass(frozen=True)
class Created:
    dataset: str
    kind: Kind
    conn: Conn
    claim: dict[str, Any] | None
    claim_error: dict[str, Any] | None


def validate_name(name: str, settings: Settings) -> str:
    """Return ``name`` if acceptable, else raise :class:`CreateError` (400)."""
    if not _NAME.match(name or ""):
        raise CreateError(
            400,
            "bad_dataset_name",
            "dataset name must be 1-64 characters: lower-case letters, digits, hyphen or "
            "underscore, starting with a letter",
        )
    if name in RESERVED_NAMES or name == settings.scheduler_dataset:
        raise CreateError(400, "reserved_dataset_name", f"{name!r} is reserved by the bridge")
    return name


def describe_new(conn: Conn, name: str, kind: Kind) -> dict[str, Any]:
    """What the caller needs to know about a dataset that now exists."""
    scoped = replace(conn, dataset=name, overridden=True)
    bank_scoped = name in conn.bank_scoped_datasets
    out: dict[str, Any] = {
        "dataset": name,
        "kind": kind,
        "persistent": kind == "persistent",
        "volatile": kind == "memory",
        "bank": conn.bank_name,
        "bankScoped": bank_scoped,
        "graphs": {role: scoped.graph(role) for role in GRAPH_ROLES},
    }
    notes: list[str] = []
    if kind == "memory":
        notes.append(
            "in-memory: the contents are lost when Fuseki restarts; push anything worth "
            "keeping to a persistent dataset first"
        )
    if not bank_scoped:
        notes.append(
            f"graphs follow urn:{{dataset}}:{{role}}. To use urn:{conn.bank_name}:{{dataset}}:{{role}} "
            "instead, add the dataset to this bank's bankScopedDatasets in config.json and "
            "reload BEFORE its first write -- identity written under the other convention is "
            "orphaned when the setting changes. (Pass claim=false to create without writing "
            "an owner yet.)"
        )
    if notes:
        out["notes"] = notes
    return out


async def create(
    client: FusekiClient,
    conn: Conn,
    settings: Settings,
    who: Claimant,
    *,
    name: str,
    kind: Kind,
    claim_for_creator: bool = True,
) -> Created:
    """Create ``name`` as an empty dataset. Raises :class:`CreateError`.

    The caller has already been authorised as a creator (see
    :func:`holonbridge.claim.creator`); this validates the request, checks the
    server has room and does not already host the name, creates the dataset, and
    optionally makes ``who`` its owner.
    """
    validate_name(name, settings)

    try:
        hosted = await client.list_datasets(conn)
    except FusekiError as exc:
        raise CreateError(
            502,
            "admin_api_unavailable",
            "Fuseki's /$/datasets admin API did not answer, so the dataset cannot be created "
            "or checked for. It may be disabled, protected (Fuseki's default admin rule only "
            "admits requests from the same machine) or this may not be a Fuseki server.",
            upstream=exc.as_dict(),
        ) from exc

    if any(d.get("name", "").lower() == name for d in hosted):
        raise CreateError(409, "dataset_exists", f"dataset {name!r} already exists")
    if len(hosted) >= settings.max_datasets:
        raise CreateError(
            409,
            "dataset_limit_reached",
            f"this server already hosts {len(hosted)} datasets (MAX_DATASETS={settings.max_datasets}); "
            "the bridge cannot drop datasets, so remove one in Fuseki or raise the limit",
        )

    try:
        await client.create_dataset(conn, name, persistent=kind == "persistent")
    except FusekiError as exc:
        if exc.status == 409:  # lost a race with another creator
            raise CreateError(409, "dataset_exists", f"dataset {name!r} already exists") from exc
        raise CreateError(
            502, "create_failed", "Fuseki refused to create the dataset", upstream=exc.as_dict()
        ) from exc

    new_conn = replace(conn, dataset=name, overridden=True)
    claimed: dict[str, Any] | None = None
    claim_error: dict[str, Any] | None = None
    if claim_for_creator:
        # The dataset now exists whatever happens next, so a failed claim is
        # reported alongside the success rather than turning the call into an error.
        try:
            claimed = await claim(client, new_conn, who)
        except ClaimError as exc:
            claim_error = {"code": exc.code, "message": exc.message}
        except FusekiError as exc:
            claim_error = {"code": "claim_failed", "message": exc.message, "status": exc.status}

    return Created(dataset=name, kind=kind, conn=new_conn, claim=claimed, claim_error=claim_error)
