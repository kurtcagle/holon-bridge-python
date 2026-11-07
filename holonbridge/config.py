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

from .egress import parse_allowlist
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


MIN_TOKEN_LENGTH = 24


def parse_identity_tokens(raw: str | None) -> dict[str, str]:
    """``IDENTITY_TOKENS="tokenA=login-a,tokenB=login-b"`` -> ``{tokenA: login-a, ...}``.

    Fails closed at startup (ValueError) on anything ambiguous -- an empty
    side, a short token, or the same token bound twice -- because a silently
    skipped entry would leave a caller unauthenticated or, worse, bound to
    the wrong identity.
    """
    out: dict[str, str] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        token, sep, identity = item.partition("=")
        token, identity = token.strip(), identity.strip()
        if not sep or not token or not identity:
            raise ValueError("IDENTITY_TOKENS entries must look like token=identity")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"IDENTITY_TOKENS: token for {identity!r} is shorter than {MIN_TOKEN_LENGTH} characters")
        if token in out:
            raise ValueError("IDENTITY_TOKENS: the same token is bound to more than one identity")
        out[token] = identity
    return out


def parse_proxy_tokens(raw: str | None) -> tuple[str, ...]:
    tokens = tuple(t.strip() for t in (raw or "").split(",") if t.strip())
    for token in tokens:
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"PROXY_TOKENS: a token is shorter than {MIN_TOKEN_LENGTH} characters")
    if len(set(tokens)) != len(tokens):
        raise ValueError("PROXY_TOKENS: duplicate token")
    return tokens


def _parse_identifier_list(raw: str | None, setting: str) -> tuple[str, ...]:
    items = tuple(i.strip() for i in (raw or "").split(",") if i.strip())
    if len(set(items)) != len(items):
        raise ValueError(f"{setting}: duplicate identifier")
    return items


def parse_dataset_claimants(raw: str | None) -> tuple[str, ...]:
    """``DATASET_CLAIMANTS="login-a,login-b"`` -> the external identifiers that
    may initialise an unclaimed dataset (become its first owner).

    Identifiers only, no tokens: authority to claim comes from who the caller
    IS (see ``claim.claimant``), and this list says which of those identities
    the operator has chosen to trust with it. Empty by default, which turns the
    in-band claim off entirely -- ``holonbridge-bootstrap-admin`` is then the
    only way in, exactly as before.
    """
    return _parse_identifier_list(raw, "DATASET_CLAIMANTS")


def parse_dataset_creators(raw: str | None) -> tuple[str, ...]:
    """``DATASET_CREATORS="login-a,login-b"`` -> the external identifiers that
    may create a brand-new dataset on the backend (``POST /datasets``).

    Same shape and same reasoning as :func:`parse_dataset_claimants`, but a
    separate list on purpose: creating a dataset is a *server-level* act (it
    allocates storage and, for an in-memory dataset, heap on a Fuseki that every
    other dataset shares), so it is not something admin on any one dataset can
    imply. Empty by default, which turns creation off entirely.
    """
    return _parse_identifier_list(raw, "DATASET_CREATORS")


@dataclass
class Settings:
    """Process-level settings, all overridable by environment variable."""

    host: str = "127.0.0.1"
    port: int = 3031
    fuseki_url: str = "http://localhost:3030"
    fuseki_dataset: str = "ds"
    bearer_token: str | None = None

    # Identity-bound credentials. ``bearer_token`` above is a SHARED token: whoever
    # holds it can send any ``x-holon-animus-id`` and be that Person, admin
    # included (the header is trusted, the token proves nothing about who is
    # calling). These settings let a deployment replace it with credentials
    # that do prove it:
    #
    #   identity_tokens -- token -> external id. A request bearing one of these
    #     IS that identity; a conflicting animus header is refused, never
    #     honoured. Give each service/person their own.
    #   proxy_tokens    -- tokens held by a TRUSTED PROXY (e.g. the MCP remote,
    #     which has itself verified the caller via OAuth). Only these may assert
    #     an identity through the header, exactly as the shared token does today.
    #   allow_legacy_bearer -- whether ``bearer_token`` is still accepted. True
    #     by default so existing deployments are unchanged; set false once
    #     every caller has moved to one of the two kinds above.
    identity_tokens: dict[str, str] = field(default_factory=dict)
    proxy_tokens: tuple[str, ...] = ()
    # External identifiers permitted to initialise a dataset that has no owner
    # or admin yet. See claim.py and parse_dataset_claimants.
    dataset_claimants: tuple[str, ...] = ()
    # External identifiers permitted to create a new dataset on the backend.
    # Empty turns ``POST /datasets`` off. See dataset_create.py.
    dataset_creators: tuple[str, ...] = ()
    # Hosts a projection webhook may be sent to (``WEBHOOK_ALLOWLIST``). Empty
    # means the default policy: https only, public addresses only. See
    # egress.py for the entry syntax and the reasoning.
    webhook_allowlist: tuple[str, ...] = ()
    # A trigger registered through the API records who armed it, and that
    # person's access is re-checked when it fires. A trigger with no recorded
    # person was hand-authored; by default it keeps running as before. Turn this
    # on to refuse to fire any trigger that cannot be traced to a person.
    triggers_require_definer: bool = False
    # Hosts ``/ingest`` may fetch a ``source_url`` from (``FETCH_ALLOWLIST``;
    # same entry syntax). Empty means the default policy: https only, public
    # addresses only, no credentials in the URL. When set, ONLY these hosts can
    # be reached (and a listed host is exempt from the https / public rules --
    # the way to ingest from a service on your own network). Applies to every
    # redirect hop too. See fetch.py and egress.py.
    fetch_allowlist: tuple[str, ...] = ()
    # Largest response ``source_url`` will read, in bytes, counted after
    # decompression. Larger responses are abandoned and the ingest fails.
    fetch_max_bytes: int = 10 * 1024 * 1024
    # Hosts an agent endpoint may point at (``AGENT_ALLOWLIST``; same entry
    # syntax as the webhook allowlist, deliberately a separate setting: what a
    # projection may be pushed to and what an agent may be sent are different
    # trust decisions). Empty means the default policy -- https, public
    # addresses only -- and, in that case, an endpoint may not carry
    # credentials at all: a credential only travels to a host the operator
    # listed. See agents/policy.py.
    agent_allowlist: tuple[str, ...] = ()
    # Credential references (``env:NAME``) must name an environment variable
    # starting with this prefix. Without it, anyone who can register an
    # endpoint could aim ``env:DATABASE_PASSWORD`` at a host of their choosing.
    agent_credential_prefix: str = "HB_AGENT_"
    # Most calls that may be open (awaiting approval, queued, or sending) to one
    # endpoint at a time; a request beyond that is refused with 429. A trigger
    # whose condition matches thousands of nodes would otherwise write thousands
    # of calls, each carrying a request body, into the ledger. 0 = no limit.
    agent_max_open_calls: int = 200
    # Creation is refused once the backend already hosts this many datasets.
    # There is deliberately no way to drop one through the bridge, so this is
    # the backstop against a runaway loop (or a mistaken script) filling the
    # server. Counts what Fuseki reports, not just what the bridge created.
    max_datasets: int = 50

    # Where ``snapshot_dataset`` writes (and ``restore_dataset`` reads): a
    # directory on the bridge's own host, one subdirectory per dataset. Unset
    # turns both off. ``snapshot_max_bytes`` caps what one snapshot may hold,
    # since the bridge reads the whole dataset into memory to write it.
    snapshot_dir: Path | None = None
    snapshot_max_bytes: int = 50_000_000
    snapshot_keep: int = 5  # newest snapshots kept per dataset
    # Snapshots and exports older than this many days are removed after a new
    # one is written, except that the newest is always kept. 0 = no age limit.
    snapshot_max_age_days: int = 0
    # ``export_dataset`` writes one zipped TriG file under
    # ``SNAPSHOT_DIR/{dataset}/exports/``. ``export_max_bytes`` caps the
    # uncompressed TriG (checked as it is written, so a refused export leaves
    # nothing behind). Memory use is the largest single graph, not the dataset.
    export_max_bytes: int = 200_000_000
    # Signs the expiring download links for exports. Unset (or under 32
    # characters) turns links off; exports are still written. Never logged.
    export_signing_key: str | None = None
    export_link_ttl: int = 900  # seconds a link lives unless the caller asks for less/more
    # If set, a link is also returned as an absolute URL on this base
    # (for example the bridge's public ngrok address); otherwise just the path.
    export_public_base_url: str | None = None
    allow_legacy_bearer: bool = True

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

    # Filesystem path to Fuseki's own assembler-config directory (its
    # ``run/configuration/`` or equivalent), *only* meaningful when this
    # bridge process and the Fuseki server it talks to share a filesystem --
    # true for a local dev/single-box setup like Kurt's, not assumed in
    # general. None by default: provisioning.py's admin routes refuse to
    # run rather than guess a path when this isn't set. Deliberately not
    # derived from fuseki_url, since a reachable HTTP URL says nothing about
    # whether this process can see that host's disk.
    fuseki_config_dir: Path | None = None

    def __post_init__(self) -> None:
        # One token, one role. A token that is both the shared legacy token and
        # an identity token (or a proxy token) would be ambiguous about whether
        # the animus header may override it, so refuse to start.
        if not self.allow_legacy_bearer and not (self.identity_tokens or self.proxy_tokens):
            # Fails closed (every request would be a 401) but is certainly a
            # mistake -- say so at startup rather than at the first request.
            raise ValueError(
                "ALLOW_LEGACY_BEARER=false but no IDENTITY_TOKENS or PROXY_TOKENS are "
                "configured: no credential could ever authenticate"
            )
        seen: dict[str, str] = {}
        for token, role in (
            *( (t, "identity") for t in self.identity_tokens),
            *( (t, "proxy") for t in self.proxy_tokens),
            *( ((self.bearer_token, "legacy"),) if self.bearer_token else () ),
        ):
            if token in seen:
                raise ValueError(f"the same token is configured as both {seen[token]} and {role}")
            seen[token] = role

    @classmethod
    def from_env(cls) -> "Settings":
        def _flag(key: str, default: bool) -> bool:
            raw = os.getenv(key)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        fuseki_config_dir_raw = os.getenv("FUSEKI_CONFIG_DIR")

        return cls(
            host=os.getenv("HOLONBRIDGE_HOST", "127.0.0.1"),
            port=int(os.getenv("HOLONBRIDGE_PORT", "3031")),
            fuseki_url=os.getenv("FUSEKI_URL", "http://localhost:3030").rstrip("/"),
            fuseki_dataset=os.getenv("FUSEKI_DATASET", "ds"),
            bearer_token=os.getenv("BEARER_TOKEN") or None,
            identity_tokens=parse_identity_tokens(os.getenv("IDENTITY_TOKENS")),
            proxy_tokens=parse_proxy_tokens(os.getenv("PROXY_TOKENS")),
            dataset_claimants=parse_dataset_claimants(os.getenv("DATASET_CLAIMANTS")),
            dataset_creators=parse_dataset_creators(os.getenv("DATASET_CREATORS")),
            webhook_allowlist=parse_allowlist(os.getenv("WEBHOOK_ALLOWLIST")),
            fetch_allowlist=parse_allowlist(os.getenv("FETCH_ALLOWLIST")),
            fetch_max_bytes=max(1, int(os.getenv("FETCH_MAX_BYTES", str(10 * 1024 * 1024)))),
            triggers_require_definer=_flag("TRIGGERS_REQUIRE_DEFINER", False),
            agent_allowlist=parse_allowlist(os.getenv("AGENT_ALLOWLIST")),
            agent_credential_prefix=(
                os.getenv("AGENT_CREDENTIAL_PREFIX") or "HB_AGENT_"
            ).strip(),
            agent_max_open_calls=max(0, int(os.getenv("AGENT_MAX_OPEN_CALLS", "200"))),
            max_datasets=int(os.getenv("MAX_DATASETS", "50")),
            snapshot_dir=Path(os.environ["SNAPSHOT_DIR"]) if os.getenv("SNAPSHOT_DIR") else None,
            snapshot_max_bytes=int(os.getenv("SNAPSHOT_MAX_BYTES", "50000000")),
            snapshot_keep=max(1, int(os.getenv("SNAPSHOT_KEEP", "5"))),
            snapshot_max_age_days=max(0, int(os.getenv("SNAPSHOT_MAX_AGE_DAYS", "0"))),
            export_max_bytes=max(1, int(os.getenv("EXPORT_MAX_BYTES", "200000000"))),
            export_signing_key=os.getenv("EXPORT_SIGNING_KEY") or None,
            export_link_ttl=max(60, min(86_400, int(os.getenv("EXPORT_LINK_TTL", "900")))),
            export_public_base_url=os.getenv("EXPORT_PUBLIC_BASE_URL") or None,
            allow_legacy_bearer=_flag("ALLOW_LEGACY_BEARER", True),
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
            fuseki_config_dir=Path(fuseki_config_dir_raw) if fuseki_config_dir_raw else None,
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
