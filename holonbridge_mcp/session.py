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
(``from .. import session``) and accesses ``session._dataset_override``
etc. by qualified attribute, not via ``from .session import
_dataset_override`` -- the latter would bind the value at import time and
go stale the moment ``switch_dataset`` reassigns it elsewhere. See
``tools/datasets.py`` and ``tools/banks.py`` for the two places that
matters, and ``tools/core.py``'s ``get_endpoint`` for a read-only case
of the same thing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from holonbridge.envfile import load_shared_env

from .identity import current_github_login

log = logging.getLogger("holonbridge_mcp.server")

load_shared_env()

BRIDGE_URL = os.getenv("HOLONBRIDGE_URL", "http://localhost:3031").rstrip("/")
BEARER = os.getenv("BEARER_TOKEN", "")
# Mutable: switch_dataset changes this for the life of the process. Module
# state rather than a per-call argument because switching is a session
# gesture ("now I am working on bridgerton"), not something to repeat on
# every tool. The cost is that it is genuinely global — a second client
# against the same process sees the switch too. Fine for one operator and
# their own tunnel, which is what this is; not fine for shared hosting.
#
# Persisted, not just held in memory. The failure this fixes happened live,
# twice: a chosen dataset survives fine within a session, but an MCP process
# restart used to silently fall back to whatever HOLONBRIDGE_DATASET (or
# nothing) said at import time — which meant a write immediately after a
# restart landed in the wrong dataset with no error and no warning, because
# nothing was watching for that specific kind of drift. A real env var still
# wins over the persisted file, matching how .env itself behaves — an
# explicit HOLONBRIDGE_DATASET is a deliberate pin, not a stale leftover.
_DATASET_STATE_FILE = Path(
    os.getenv("HOLONBRIDGE_DATASET_STATE_FILE", "")
    or (Path.home() / ".holonbridge" / "mcp-dataset-override")
)


def _load_persisted_dataset() -> str:
    try:
        return _DATASET_STATE_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _persist_dataset(name: str) -> None:
    """Save (or, for an empty name, clear) the dataset override to disk.

    Best-effort: a failure to persist should not stop the switch itself from
    taking effect for the rest of this session, only from surviving the next
    restart. Logged, not raised.
    """
    try:
        if name:
            _DATASET_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _DATASET_STATE_FILE.write_text(name, encoding="utf-8")
        else:
            _DATASET_STATE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log.warning(
            "could not persist dataset override to %s: %s "
            "(the switch is still in effect for this session, but will not "
            "survive a restart)",
            _DATASET_STATE_FILE,
            exc,
        )


def _resolve_initial_dataset() -> tuple[str, str]:
    """Returns (dataset, source) where source is 'env', 'persisted', or 'none'."""
    explicit = os.getenv("HOLONBRIDGE_DATASET", "").strip()
    if explicit:
        return explicit, "env"
    persisted = _load_persisted_dataset()
    if persisted:
        log.info(
            "restored dataset override %r from %s (surviving a prior "
            "restart) — set HOLONBRIDGE_DATASET explicitly to override this",
            persisted,
            _DATASET_STATE_FILE,
        )
        return persisted, "persisted"
    return "", "none"


_dataset_override, _dataset_override_source = _resolve_initial_dataset()

# The same treatment for banks. A *bank* is a named backend connection -- a
# server URL plus a default dataset -- previously called a "profile". The two
# overrides are deliberately independent: a bank selects *which store*, a
# dataset selects *which graph set within it*, and switching one should not
# silently reset the other.
#
# Kept as a parallel implementation rather than folded into one generic
# helper with the dataset functions above. The duplication is about fifteen
# lines; the alternative rewrites code that a live restart test and eight
# unit tests currently cover, to save less than it risks.
_BANK_STATE_FILE = Path(
    os.getenv("HOLONBRIDGE_BANK_STATE_FILE", "")
    or (Path.home() / ".holonbridge" / "mcp-bank-override")
)


def _load_persisted_bank() -> str:
    try:
        return _BANK_STATE_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _persist_bank(name: str) -> None:
    """Save (or, for an empty name, clear) the bank override to disk.

    Best-effort, exactly as for the dataset override: failing to persist must
    not stop the switch taking effect for this session, only from surviving a
    restart.
    """
    try:
        if name:
            _BANK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _BANK_STATE_FILE.write_text(name, encoding="utf-8")
        else:
            _BANK_STATE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log.warning(
            "could not persist bank override to %s: %s "
            "(the switch is still in effect for this session, but will not "
            "survive a restart)",
            _BANK_STATE_FILE,
            exc,
        )


def _resolve_initial_bank() -> tuple[str, str]:
    """Returns (bank, source) where source is 'env', 'persisted', or 'none'."""
    explicit = os.getenv("HOLONBRIDGE_BANK", "").strip()
    if explicit:
        return explicit, "env"
    persisted = _load_persisted_bank()
    if persisted:
        log.info(
            "restored bank override %r from %s (surviving a prior restart) "
            "— set HOLONBRIDGE_BANK explicitly to override this",
            persisted,
            _BANK_STATE_FILE,
        )
        return persisted, "persisted"
    return "", "none"


_bank_override, _bank_override_source = _resolve_initial_bank()
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
    if _dataset_override:
        headers["X-Dataset-Override"] = _dataset_override
    login = current_github_login.get()
    if login:
        headers["X-Holon-Animus-Id"] = login
        headers["X-Holon-Animus-Type"] = "GitHubIdentity"
    return headers


def _with_bank(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Apply the bank override as ``?bank=`` without discarding caller params.

    The bridge resolves the bank from the query string rather than a header
    (``deps.resolve_conn`` reads ``request.query_params["bank"]``), so this
    has to merge rather than replace -- a caller-supplied ``bank`` still wins,
    which keeps a deliberate per-call choice above the session default.
    """
    if not _bank_override:
        return params
    merged = dict(params or {})
    merged.setdefault("bank", _bank_override)
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
        return {"error": True, "status": response.status_code, "detail": detail}
    if text:
        return response.text
    try:
        return response.json()
    except ValueError:
        return response.text
