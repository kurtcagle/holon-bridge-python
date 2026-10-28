"""Shared session plumbing for holonbridge-mcp: environment, the dataset/
bank override state and its persistence, the FastMCP instance, and the
HTTP call helper every tool goes through.

Extracted from ``server.py`` 2026-08-28 as part of decomposing that file
from a single ~1,600-line module into this plus ``tools/*.py`` (one file
per tool group). Nothing in this module changed behaviourally in the
move -- every name, docstring, and code path here is exactly what
``server.py`` used to carry, just relocated so the ~67 tool definitions
that depend on it don't all have to live in the same file to reach it.

Tool modules import ``mcp`` and ``_call`` directly (both are stable
references, never reassigned). A tool that needs to read or write the
dataset/bank override state imports this module itself
(``from .. import session``) and calls ``session.current_dataset_override()``
/ ``session.set_dataset_override(...)`` etc. (and the bank equivalents) --
never reaches into ``_dataset_overrides``/``_bank_overrides`` directly.
See ``tools/datasets.py`` and ``tools/banks.py`` for the two places that
matters, and ``tools/core.py``'s ``get_endpoint`` for a read-only case of
the same thing.

CHANGED 2026-09-23: dataset/bank override state went from one value per
process to one value per caller identity, keyed by
``identity.current_github_login``. Previously ``_dataset_override`` (and
``_bank_override``) were plain module-level strings -- genuinely global,
so a second person's client talking to the same running process saw and
was affected by the first person's ``switch_dataset`` call, with no
warning to either of them. Confirmed live as a real, not hypothetical,
risk once more than one person could reasonably be expected to share a
running holonbridge-mcp process (Kurt's framing: "I can definitely see
multiple people working on the MCP simultaneously"). Fixed by keying
everything on ``current_github_login.get()`` -- see that ContextVar's own
docstring in ``identity.py`` for why it is already scoped at exactly the
right granularity (set once per SSE connection, correctly inherited by
every tool call the session makes afterward) without this module needing
to invent its own session tracking. ``None`` -- the shared-token auth
mode, where the docstring there says plainly "there is no way to tell
callers apart" -- is a legitimate dict key like any other login string,
so that deployment mode collapses back to exactly today's shared-global
behaviour automatically, with no special-casing required: there genuinely
is no finer granularity available to it.

This does NOT fix write collisions between two people editing the same
graph -- that is a real, separate concern (see ``holonbridge.acl``'s
grant model) outside what dataset/bank *selection* can address at all.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from holonbridge.envfile import load_shared_env

from .identity import current_github_login
from .friendly import plain_message

log = logging.getLogger("holonbridge_mcp.server")

load_shared_env()

BRIDGE_URL = os.getenv("HOLONBRIDGE_URL", "http://localhost:3031").rstrip("/")

# Variables the MCP process may take its credential for the REST bridge from,
# in order. HOLONBRIDGE_MCP_TOKEN is the MCP's *own* token: once the bridge
# gives the MCP a PROXY_TOKENS entry it must differ from BEARER_TOKEN (one
# token, one role -- see holonbridge.config.Settings), and BEARER_TOKEN is also
# what the REST process reads, so the two processes cannot share one variable.
# BEARER_TOKEN stays as the fallback so every existing setup keeps working.
MCP_TOKEN_VARS = ("HOLONBRIDGE_MCP_TOKEN", "BEARER_TOKEN")


def resolve_mcp_token(environ: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """``(token, variable it came from)``, or ``("", None)`` when none is set.

    A variable that is set but blank counts as unset -- a shared ``.env`` that
    carries ``BEARER_TOKEN=`` for the REST process must not mask the MCP's own
    token, nor stop the fallback from reaching it.
    """
    env = os.environ if environ is None else environ
    for name in MCP_TOKEN_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value, name
    return "", None


BEARER, BEARER_SOURCE = resolve_mcp_token()
if BEARER_SOURCE:
    # The name only, never the value.
    log.info("authenticating to the bridge with the token from %s", BEARER_SOURCE)
else:
    log.warning(
        "no %s set: calls to the bridge carry no Authorization header (fine for an "
        "open loopback bridge; otherwise every call is a 401 'missing bearer token')",
        " or ".join(MCP_TOKEN_VARS),
    )


def _safe_filename_component(login: str) -> str:
    """A login string, made safe as a filename suffix. Logins are GitHub
    usernames in practice (alphanumeric plus hyphens), but this makes no
    assumption beyond "don't let it escape the intended directory or
    collide with the separator" -- anything else is replaced, not rejected,
    since a slightly-mangled-but-still-unique filename is fine here and a
    thrown exception on a weird identity string is not."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", login)


# --------------------------------------------------------------------------
# Dataset override -- one per caller identity, not one per process.
# --------------------------------------------------------------------------
#
# Module state rather than a per-call argument because switching is a
# session gesture ("now I am working on bridgerton"), not something to
# repeat on every tool -- see current_dataset_override()'s own note on why
# keying by current_github_login.get() gives every identity its own
# independent version of that gesture, in the same two dicts, rather than
# needing separate module-level variables per identity (which would be
# unbounded and pointless to declare in advance).
#
# Persisted, not just held in memory, same reasoning as before this
# change: a chosen dataset survives fine within a session, but a process
# restart used to silently fall back to whatever HOLONBRIDGE_DATASET (or
# nothing) said at import time -- which meant a write immediately after a
# restart could land in the wrong dataset with no error and no warning.
# Now per-identity: each person's own persisted file, loaded lazily the
# first time that identity is seen rather than all pre-loaded at import
# (the set of identities that will ever connect isn't known at import
# time). A real HOLONBRIDGE_DATASET env var still wins over any persisted
# file, for any identity, matching how .env itself behaves -- an explicit
# env var is a deliberate operator-level pin, not a per-user preference,
# so it is intentionally NOT keyed by identity at all.

_ENV_DATASET = os.getenv("HOLONBRIDGE_DATASET", "").strip()

_DATASET_STATE_FILE = Path(
    os.getenv("HOLONBRIDGE_DATASET_STATE_FILE", "")
    or (Path.home() / ".holonbridge" / "mcp-dataset-override")
)

_dataset_overrides: dict[str | None, str] = {}
_dataset_override_sources: dict[str | None, str] = {}


def _dataset_state_file_for(login: str | None) -> Path:
    """Per-identity persisted-override file. ``login`` of ``None`` (the
    shared-token auth mode -- see ``identity.py``) uses the original,
    un-suffixed filename, so a shared-token deployment's existing
    persisted choice keeps working unchanged across this change with no
    migration step. A named login gets its own ``{base}.{login}`` file --
    nothing pre-existing to migrate for that case, since no such file
    could have existed before per-identity overrides did."""
    if login is None:
        return _DATASET_STATE_FILE
    return _DATASET_STATE_FILE.with_name(
        _DATASET_STATE_FILE.name + "." + _safe_filename_component(login)
    )


def _persist_dataset(login: str | None, name: str) -> None:
    """Save (or, for an empty name, clear) one identity's dataset override
    to disk. Best-effort: a failure to persist should not stop the switch
    itself from taking effect for the rest of this session, only from
    surviving the next restart. Logged, not raised."""
    path = _dataset_state_file_for(login)
    try:
        if name:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning(
            "could not persist dataset override for %s to %s: %s "
            "(the switch is still in effect for this session, but will not "
            "survive a restart)",
            login or "(shared-token caller)",
            path,
            exc,
        )


def _ensure_dataset_override_loaded(login: str | None) -> None:
    """Lazily resolve and cache one identity's starting override the first
    time it is asked for. Mirrors what used to be a single eager
    ``_resolve_initial_dataset()`` call at import time, just done once per
    identity on first use instead of once globally at import -- the set of
    identities that will ever connect to a running process isn't known in
    advance, so there is nothing to eagerly resolve at import for any of
    them but the env var, which applies to all of them identically anyway.
    """
    if login in _dataset_overrides:
        return
    if _ENV_DATASET:
        _dataset_overrides[login] = _ENV_DATASET
        _dataset_override_sources[login] = "env"
        return
    try:
        persisted = _dataset_state_file_for(login).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        persisted = ""
    if persisted:
        log.info(
            "restored dataset override %r for %s from %s (surviving a "
            "prior restart) -- set HOLONBRIDGE_DATASET explicitly to "
            "override this for every identity",
            persisted,
            login or "(shared-token caller)",
            _dataset_state_file_for(login),
        )
        _dataset_overrides[login] = persisted
        _dataset_override_sources[login] = "persisted"
    else:
        _dataset_overrides[login] = ""
        _dataset_override_sources[login] = "none"


def current_dataset_override() -> str:
    """The calling identity's own dataset override, or ``""`` if they have
    none. "Calling identity" is ``current_github_login.get()`` -- see that
    ContextVar's docstring in ``identity.py`` for why this is already
    scoped correctly per MCP session (set once per SSE connection,
    inherited by every tool call the session makes afterward) without
    this function needing to track sessions itself. Two different
    identities calling this at the same moment get two independent
    answers; the same identity calling it from two different tabs/sessions
    shares one answer, which is the intended granularity -- a dataset
    choice is a property of *who you are*, not of which browser tab
    happens to be open."""
    login = current_github_login.get()
    _ensure_dataset_override_loaded(login)
    return _dataset_overrides[login]


def current_dataset_override_source() -> str:
    """``"env"``, ``"persisted"``, ``"explicit"``, or ``"none"`` -- see
    ``current_dataset_override``'s docstring for the identity scoping this
    shares."""
    login = current_github_login.get()
    _ensure_dataset_override_loaded(login)
    return _dataset_override_sources[login]


def set_dataset_override(name: str, source: str) -> None:
    """Set (or, for an empty ``name``, clear) the calling identity's own
    dataset override, and persist it under that identity's own file.
    Never touches any other identity's override."""
    login = current_github_login.get()
    if name:
        _dataset_overrides[login] = name
        _dataset_override_sources[login] = source
    else:
        _dataset_overrides[login] = ""
        _dataset_override_sources[login] = "none"
    _persist_dataset(login, name)


# --------------------------------------------------------------------------
# Bank override -- same shape as the dataset override above, same reasoning
# throughout. Kept as a parallel implementation rather than folded into one
# generic helper with the dataset functions above, same call as before this
# change: the duplication is modest and the alternative rewrites code that
# a live restart test and several unit tests already cover, to save less
# than it risks.
# --------------------------------------------------------------------------

_ENV_BANK = os.getenv("HOLONBRIDGE_BANK", "").strip()

_BANK_STATE_FILE = Path(
    os.getenv("HOLONBRIDGE_BANK_STATE_FILE", "")
    or (Path.home() / ".holonbridge" / "mcp-bank-override")
)

_bank_overrides: dict[str | None, str] = {}
_bank_override_sources: dict[str | None, str] = {}


def _bank_state_file_for(login: str | None) -> Path:
    """See ``_dataset_state_file_for`` -- identical reasoning, including
    the ``None``-uses-the-unsuffixed-file backward-compatibility case."""
    if login is None:
        return _BANK_STATE_FILE
    return _BANK_STATE_FILE.with_name(
        _BANK_STATE_FILE.name + "." + _safe_filename_component(login)
    )


def _persist_bank(login: str | None, name: str) -> None:
    """See ``_persist_dataset`` -- identical reasoning."""
    path = _bank_state_file_for(login)
    try:
        if name:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning(
            "could not persist bank override for %s to %s: %s "
            "(the switch is still in effect for this session, but will not "
            "survive a restart)",
            login or "(shared-token caller)",
            path,
            exc,
        )


def _ensure_bank_override_loaded(login: str | None) -> None:
    """See ``_ensure_dataset_override_loaded`` -- identical reasoning."""
    if login in _bank_overrides:
        return
    if _ENV_BANK:
        _bank_overrides[login] = _ENV_BANK
        _bank_override_sources[login] = "env"
        return
    try:
        persisted = _bank_state_file_for(login).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        persisted = ""
    if persisted:
        log.info(
            "restored bank override %r for %s from %s (surviving a prior "
            "restart) -- set HOLONBRIDGE_BANK explicitly to override this "
            "for every identity",
            persisted,
            login or "(shared-token caller)",
            _bank_state_file_for(login),
        )
        _bank_overrides[login] = persisted
        _bank_override_sources[login] = "persisted"
    else:
        _bank_overrides[login] = ""
        _bank_override_sources[login] = "none"


def current_bank_override() -> str:
    """See ``current_dataset_override`` -- identical identity scoping."""
    login = current_github_login.get()
    _ensure_bank_override_loaded(login)
    return _bank_overrides[login]


def current_bank_override_source() -> str:
    """See ``current_dataset_override_source`` -- identical identity scoping."""
    login = current_github_login.get()
    _ensure_bank_override_loaded(login)
    return _bank_override_sources[login]


def set_bank_override(name: str, source: str) -> None:
    """See ``set_dataset_override`` -- identical identity scoping."""
    login = current_github_login.get()
    if name:
        _bank_overrides[login] = name
        _bank_override_sources[login] = source
    else:
        _bank_overrides[login] = ""
        _bank_override_sources[login] = "none"
    _persist_bank(login, name)


ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _transport_security():
    """Allow the tunnel's own hostname past the SDK's DNS-rebinding check.

    The MCP SDK enables DNS-rebinding protection by default with an empty
    ``allowed_hosts``, which means it accepts only localhost. That is the
    right default for a server bound to 127.0.0.1 and reached directly, and
    exactly wrong behind a tunnel: ngrok forwards with the public hostname in
    ``Host``, so every request fails the check with a 421 and a
    ``ValueError: Request validation failed`` raised from deep inside the SSE
    handler — after OAuth has already succeeded, which makes it look like an
    auth problem when it is not one.

    The public hostname is already known: it is ``MCP_PUBLIC_URL``, which the
    OAuth layer requires anyway. Deriving the allowlist from it means one less
    thing to configure and one less thing to get inconsistent.
    ``MCP_ALLOWED_HOSTS`` is there for the cases this cannot infer - a second
    tunnel, a reverse proxy in front, a custom domain.
    """
    from mcp.server.transport_security import (  # noqa: PLC0415
        TransportSecuritySettings,
    )

    hosts: list[str] = ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*"]
    origins: list[str] = []

    public_url = os.getenv("MCP_PUBLIC_URL", "").strip()
    if public_url:
        from urllib.parse import urlparse  # noqa: PLC0415

        parsed = urlparse(public_url)
        if parsed.hostname:
            hosts.append(parsed.hostname)
            # A tunnel terminates TLS on the public side and forwards plain
            # HTTP, so Host carries no port; include both shapes rather than
            # guess which one arrives.
            if parsed.port:
                hosts.append(f"{parsed.hostname}:{parsed.port}")
        if parsed.scheme and parsed.hostname:
            origins.append(f"{parsed.scheme}://{parsed.netloc}")

    extra = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    if extra:
        hosts.extend(h.strip() for h in extra.split(",") if h.strip())

    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


mcp = FastMCP("holonbridge", transport_security=_transport_security())


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if BEARER:
        headers["Authorization"] = f"Bearer {BEARER}"
    override = current_dataset_override()
    if override:
        headers["X-Dataset-Override"] = override
    login = current_github_login.get()
    if login:
        headers["X-Holon-Animus-Id"] = login
        headers["X-Holon-Animus-Type"] = "GitHubIdentity"
    return headers


def _with_bank(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Apply the calling identity's bank override as ``?bank=`` without
    discarding caller params.

    The bridge resolves the bank from the query string rather than a header
    (``deps.resolve_conn`` reads ``request.query_params["bank"]``), so this
    has to merge rather than replace -- a caller-supplied ``bank`` still wins,
    which keeps a deliberate per-call choice above the session default.
    """
    override = current_bank_override()
    if not override:
        return params
    merged = dict(params or {})
    merged.setdefault("bank", override)
    return merged


async def _call(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    text: bool = False,
) -> Any:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.request(
            method,
            f"{BRIDGE_URL}{path}",
            json=json_body,
            params=_with_bank(params),
            headers=_headers(),
        )
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text.strip()
        failure: dict[str, Any] = {"error": True, "status": response.status_code, "detail": detail}
        message = plain_message(response.status_code, path, detail)
        if message:
            failure["message"] = message
        return failure
    if text:
        return response.text
    try:
        return response.json()
    except ValueError:
        return response.text
