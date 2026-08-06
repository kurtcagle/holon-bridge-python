"""Configuration and named-bank handling.

Mirrors the Node bridge's ``.env`` + ``~/.holonbridge/config.json`` split:
process-level settings come from the environment, connection banks come
from the JSON config so a client can switch servers without a code change.

A *bank* is a named backend connection -- a server URL, a default dataset,
and optional credentials. Previously called a "profile"; renamed because a
profile connotes a person's preferences, while what this actually names is
a store: a vault the graphs live in. The old ``profiles`` key in
``config.json`` is still read, with a warning, so existing configs keep
working.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .envfile import load_shared_env

log = logging.getLogger(__name__)

load_shared_env()

DEFAULT_CONFIG_PATH = Path.home() / ".holonbridge" / "config.json"


@dataclass(frozen=True)
class Bank:
    """A named backend connection: where graphs are stored and how to reach it."""

    name: str
    url: str
    dataset: str
    auth_token: str | None = None

    # Datasets on this bank whose graphs have actually been rewritten to the
    # urn:{bank}:{dataset}:{role} naming convention. Empty by default -- the
    # convention is opt-in per dataset, never assumed from the bank alone.
    # A dataset appearing here without having actually been migrated (or
    # vice versa) is exactly the class of drift that made the SHACL gate
    # unarmable under the old urn:data:* naming; this list exists so that
    # question is answered by explicit config, not inferred from what a
    # graph happens to contain.
    bank_scoped_datasets: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_json(cls, name: str, raw: dict[str, Any]) -> "Bank":
        auth = raw.get("auth") or {}
        token = auth.get("token") if isinstance(auth, dict) else None
        scoped = raw.get("bankScopedDatasets") or []
        if not isinstance(scoped, list):
            raise ValueError(
                f"bank {name!r}: bankScopedDatasets must be a list of dataset names"
            )
        return cls(
            name=name,
            url=str(raw.get("url", "http://localhost:3030")).rstrip("/"),
            dataset=str(raw.get("dataset", "ds")),
            auth_token=token,
            bank_scoped_datasets=frozenset(str(d) for d in scoped),
        )

    def as_public(self) -> dict[str, Any]:
        """Bank view safe to hand back over the wire (no token)."""
        return {
            "name": self.name,
            "url": self.url,
            "dataset": self.dataset,
            "authenticated": bool(self.auth_token),
            "bankScopedDatasets": sorted(self.bank_scoped_datasets),
        }


@dataclass
class Settings:
    """Process-level settings, all overridable by environment variable."""

    host: str = "127.0.0.1"
    port: int = 3031
    fuseki_url: str = "http://localhost:3030"
    fuseki_dataset: str = "ds"
    bearer_token: str | None = None

    # Turtle handling. ``passthrough`` sends payloads to Jena unparsed, which
    # is the only safe mode for RDF 1.2 (rdflib cannot parse triple terms).
    # ``local`` pre-parses with rdflib for a faster syntax error, and will
    # reject valid Turtle 1.2 — use it only for 1.1 pipelines.
    parse_mode: str = "passthrough"

    # SHACL gate. When true every write is validated before it lands.
    shacl_required: bool = False
    # Delta mode reports only violations the incoming payload introduces,
    # so one pre-existing violation in a target graph cannot block all writes.
    shacl_delta: bool = True

    # Allow clients to retarget the dataset per request via X-Dataset-Override.
    allow_dataset_override: bool = True

    request_timeout: float = 60.0
    named_query_ttl: float = 60.0

    # Scheduler. Off by default: it writes on its own initiative, so starting
    # it must be a deliberate act. It always reads its config from the admin
    # dataset regardless of what a caller has selected.
    scheduler_enabled: bool = False
    scheduler_dataset: str = "admin"
    scheduler_tick_seconds: float = 30.0
    scheduler_max_firing_depth: int = 3
    config_path: Path = field(default=DEFAULT_CONFIG_PATH)

    @classmethod
    def from_env(cls) -> "Settings":
        def _flag(key: str, default: bool) -> bool:
            raw = os.getenv(key)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            host=os.getenv("HOLONBRIDGE_HOST", "127.0.0.1"),
            port=int(os.getenv("HOLONBRIDGE_PORT", "3031")),
            fuseki_url=os.getenv("FUSEKI_URL", "http://localhost:3030").rstrip("/"),
            fuseki_dataset=os.getenv("FUSEKI_DATASET", "ds"),
            bearer_token=os.getenv("BEARER_TOKEN") or None,
            parse_mode=os.getenv("PARSE_MODE", "passthrough").strip().lower(),
            shacl_required=_flag("SHACL_REQUIRED", False),
            shacl_delta=_flag("SHACL_DELTA", True),
            allow_dataset_override=_flag("ALLOW_DATASET_OVERRIDE", True),
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "60")),
            named_query_ttl=float(os.getenv("NAMED_QUERY_TTL", "60")),
            scheduler_enabled=_flag("SCHEDULER_ENABLED", False),
            scheduler_dataset=os.getenv("SCHEDULER_DATASET", "admin"),
            scheduler_tick_seconds=float(os.getenv("SCHEDULER_TICK_SECONDS", "30")),
            scheduler_max_firing_depth=int(os.getenv("SCHEDULER_MAX_FIRING_DEPTH", "3")),
            config_path=Path(os.getenv("HOLONBRIDGE_CONFIG", str(DEFAULT_CONFIG_PATH))),
        )


class BankStore:
    """Loads, lists, and switches named banks."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._banks: dict[str, Bank] = {}
        self._active: str = "local"
        self.reload()

    def reload(self) -> None:
        path = self._settings.config_path
        raw: dict[str, Any] = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}

        banks = raw.get("banks")
        if banks is None:
            legacy = raw.get("profiles")
            if legacy is not None:
                log.warning(
                    "%s uses the legacy 'profiles' key; rename it to 'banks'. "
                    "Still read for now, but 'banks' wins if both are present.",
                    path,
                )
                banks = legacy
        banks = banks or {}
        self._banks = {
            name: Bank.from_json(name, body)
            for name, body in banks.items()
            if isinstance(body, dict)
        }

        # The environment always supplies a usable ``local`` bank so the
        # bridge starts cleanly with no config file present.
        self._banks.setdefault(
            "local",
            Bank(
                name="local",
                url=self._settings.fuseki_url,
                dataset=self._settings.fuseki_dataset,
                auth_token=None,
            ),
        )

        requested = raw.get("default", "local")
        self._active = requested if requested in self._banks else "local"

    @property
    def active(self) -> Bank:
        return self._banks[self._active]

    def get(self, name: str) -> Bank:
        if name not in self._banks:
            raise KeyError(name)
        return self._banks[name]

    def list(self) -> list[dict[str, Any]]:
        return [
            {**b.as_public(), "active": name == self._active}
            for name, b in sorted(self._banks.items())
        ]

    def set_active(self, name: str) -> Bank:
        if name not in self._banks:
            raise KeyError(name)
        self._active = name
        return self._banks[name]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
