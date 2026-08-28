"""Tool group modules.

Importing this package registers every tool with the shared FastMCP
instance in ``..session`` -- ``@mcp.tool()`` runs at import time, so each
submodule's mere presence in the list below is what makes its tools show
up. ``server.py`` imports this package (for that side effect) before
calling ``mcp.run()``.

One file per tool group, mirroring the section headers the monolithic
``server.py`` used to carry as comments (and, loosely,
``holonbridge/routes/*.py`` on the bridge side). Decomposed 2026-08-28 --
see ``server.py``'s own module docstring for why, and ``session.py`` for
where the shared plumbing (env resolution, the dataset/bank override
state, the FastMCP instance, the ``_call`` HTTP helper) ended up instead.

``candidates`` and ``triggers`` are split from each other here even
though they share one route file on the bridge side
(``holonbridge/routes/triggers.py``) -- the monolithic server.py already
drew that line as a separate comment section, and keeping tool-file
granularity independent of REST-route-file granularity is deliberate:
the goal is small, domain-coherent files on this side, not a forced 1:1
mirror.
"""

from __future__ import annotations

from . import (
    banks,
    candidates,
    core,
    datasets,
    events,
    fluents,
    identity,
    named_queries,
    named_rules,
    nl,
    pipelines,
    projections,
    scheduler,
    sequences,
    shacl,
    triggers,
)

__all__ = [
    "banks",
    "candidates",
    "core",
    "datasets",
    "events",
    "fluents",
    "identity",
    "named_queries",
    "named_rules",
    "nl",
    "pipelines",
    "projections",
    "scheduler",
    "sequences",
    "shacl",
    "triggers",
]
