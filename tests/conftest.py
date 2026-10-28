"""Suite-wide test hygiene.

The one thing here is an autouse scrub of the environment variables that
configure the MCP/OAuth layer. ``holonbridge_mcp.session`` calls
``load_shared_env()`` at import time, which copies a developer's real ``.env``
into ``os.environ``. A test that asserts "nothing configured" (for example
``test_unset_client_id_means_oauth_is_off``) then passes on a clean CI box and
fails on a machine with OAuth actually set up. Scrubbing the prefixes before
every test makes the suite give the same answer everywhere.

Tests that need one of these set use ``monkeypatch.setenv`` as usual; autouse
fixtures run first, so that still works.
"""

from __future__ import annotations

import os

import pytest

_SCRUBBED_PREFIXES = ("GITHUB_OAUTH_", "MCP_")


@pytest.fixture(autouse=True)
def _scrub_mcp_and_oauth_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(_SCRUBBED_PREFIXES):
            monkeypatch.delenv(key, raising=False)


def pytest_collection_modifyitems(config, items):
    """Skip ``@pytest.mark.jena`` tests unless a real Jena is configured.

    The default run is unchanged: those tests collect, report as skipped, and
    never start a server. See ``tests/jena_support.py`` to enable the lane.
    """
    from . import jena_support

    if jena_support.configured():
        return
    skip = pytest.mark.skip(reason=jena_support.skip_reason())
    for item in items:
        if "jena" in item.keywords:
            item.add_marker(skip)
