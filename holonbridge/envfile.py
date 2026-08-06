"""Loads one shared ``.env`` file for both entry points.

Two processes, `holonbridge` and `holonbridge_mcp`, and previously two
separate mental models of where their configuration came from — neither
actually loaded a `.env` file at all; both just read `os.getenv()` against
whatever was already in the process environment. A `.env` sitting next to
either of them did nothing, silently.

One file now, findable two ways:

- ``HOLONBRIDGE_ENV_FILE``, an explicit absolute path. This is what a
  launcher that does not run from the project directory — Claude Desktop's
  `claude_desktop_config.json`, a Windows service, a scheduled task — should
  set, as a single line, instead of duplicating every variable into that
  launcher's own config.
- ``.env`` in the current working directory, otherwise. This is what
  "``cd`` into the project, run ``holonbridge`` or ``holonbridge_mcp``"
  gives you for free, with nothing further to configure.

Real environment variables always win over the file. `python-dotenv`'s
default (``override=False``) already gives us this, which is the right
precedence: a shell override for one run, or a launcher's own explicit
``env`` block, should beat whatever the shared file says.

Called once, idempotently, at the top of `holonbridge/config.py` and
`holonbridge_mcp/server.py` — the first place each package reads an
environment variable — so the file is loaded before anything it might
configure has already been read. Importing `holonbridge_mcp/server.py`
alone (`python -m holonbridge_mcp.server`, still a supported direct
invocation) goes through the same call, so there is exactly one code path
regardless of which entry point started the process.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("holonbridge.envfile")

_loaded = False


def load_shared_env() -> Path | None:
    """Load the shared ``.env`` file, if any. Returns the path used, if one was."""
    global _loaded
    if _loaded:
        return None
    _loaded = True

    from dotenv import load_dotenv  # noqa: PLC0415 - deferred; not every caller needs it

    explicit = os.getenv("HOLONBRIDGE_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            # An explicit path that does not resolve is almost always a typo,
            # and a silent no-op here just relocates the failure to a
            # confusing "why isn't my token set" downstream instead.
            raise SystemExit(
                f"HOLONBRIDGE_ENV_FILE is set to {explicit!r}, but that file "
                "does not exist. Fix the path, or unset HOLONBRIDGE_ENV_FILE "
                "to fall back to .env in the current directory."
            )
        load_dotenv(path, override=False)
        log.info("loaded environment from %s (HOLONBRIDGE_ENV_FILE)", path)
        return path

    implicit = Path.cwd() / ".env"
    if implicit.is_file():
        load_dotenv(implicit, override=False)
        log.info("loaded environment from %s", implicit)
        return implicit

    # Absence is normal, not an error: plenty of setups (containers, a
    # process manager with its own env block) supply real environment
    # variables directly and have no .env file at all.
    return None


def reset_for_testing() -> None:
    """Clear the loaded-once guard. Test-only — never call this from app code."""
    global _loaded
    _loaded = False
