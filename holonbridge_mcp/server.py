"""holonbridge-mcp (Python) — stdio MCP server over the HolonBridge REST API.

This layer holds no backend logic. It calls the bridge exactly as any other
HTTP client would, which keeps one authorisation path and one validation
path rather than two that drift.

Run::

    python -m holonbridge_mcp.server        # stdio, for Claude Desktop
    python -m holonbridge_mcp --help        # full CLI, including remote transports

Environment:
    HOLONBRIDGE_URL      default http://localhost:3031
    BEARER_TOKEN         bearer token for the bridge
    HOLONBRIDGE_DATASET  optional X-Dataset-Override applied to every call
    HOLONBRIDGE_BANK     optional bank (named backend connection) for every call
    ANTHROPIC_API_KEY    required for nl_query
    ANTHROPIC_MODEL      default claude-sonnet-4-6

CHANGED 2026-08-28: decomposed from a single ~1,600-line file (67 tools,
59.7KB) into this thin entry point plus ``session.py`` (environment
resolution, the dataset/bank override state and its persistence, the
FastMCP instance, and the ``_call``/``_headers``/``_with_bank`` HTTP
helper every tool goes through) and ``tools/*.py`` (one file per tool
group -- identity, core, shacl, nl, named_queries, named_rules, triggers,
candidates, pipelines, events, scheduler, projections, datasets, banks,
sequences, fluents -- each importing ``session.mcp`` and decorating its
own functions onto it).

Reason: this file had grown to be the largest code file in the repository
by a wide margin, and had already cost one deferred change earlier the
same day (a set of new tools was added to the REST bridge but not wired
in here yet, specifically because the file was large enough that editing
it without first reading it in full felt risky). The section boundaries
below were already legible as comments in the old file and already
largely mirrored ``holonbridge/routes/*.py`` on the bridge side; this
just makes those boundaries real module boundaries instead of comments.

Nothing about *what* any tool does changed in this move -- every name,
signature, docstring, and code path was relocated verbatim. See
``tools/__init__.py`` for the full module list and
``python -m holonbridge_mcp.server`` / ``__main__.py`` for how this still
runs exactly as it did before: ``from .server import mcp`` still resolves,
because this module re-exports ``mcp`` from ``session``.

CHANGED 2026-08-15 (carried over from the pre-decomposition file):
outbound calls to the REST bridge add ``X-Holon-Animus-Id`` /
``X-Holon-Animus-Type`` when :data:`holonbridge_mcp.identity.current_github_login`
carries a verified identity — the REST bridge's ACL layer resolves that
header to a Person and checks their Role grants before allowing a read,
write, or named-query invocation. Absent (stdio transport, or the remote
transport's static-token credential, which by design has no per-user
identity) means no animus header is sent, and the bridge's own
``require_animus`` dependency is what turns that into a clean 401 rather
than a silent bypass. See ``session.py`` for where this actually lives now.
"""

from __future__ import annotations

from . import tools  # noqa: F401 -- imported for its @mcp.tool() registration side effect
from .session import mcp

__all__ = ["mcp", "main"]


def main() -> None:  # pragma: no cover
    """Run the stdio transport. For the full CLI see ``__main__.py``."""
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
