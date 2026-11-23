"""The root helper behind ``restart_fuseki`` (``deploy/fuseki-restart/holonbridge-restart-fuseki``).

Run against a fake ``systemctl`` and ``curl``: Fuseki is "up" after a restart unless a file in
its configuration directory contains the word BAD, which is how one broken assembler file
takes the whole server down. The properties:

- a healthy restart reports ok and keeps a ``last-known-good`` copy of the configuration;
- a restart that does not come back puts the last known-good configuration back, restarts
  again, and says it rolled back;
- with no known-good copy yet it cannot roll back, and says so rather than pretending;
- a request without a valid id is ignored, and nothing in a request is ever executed.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parent.parent / "deploy" / "fuseki-restart" / "holonbridge-restart-fuseki"
ID = "0123456789abcdef0123456789abcdef"

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None, reason="needs bash"
)

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "$@" >> "$FAKE_LOG"
case "$1" in
  restart)
    if grep -rq BAD "$FUSEKI_CONFIG_DIR"; then echo down > "$FAKE_FLAG"; else echo up > "$FAKE_FLAG"; fi ;;
  is-active) [ "$(cat "$FAKE_FLAG" 2>/dev/null)" = up ] ;;
esac
"""
FAKE_CURL = """#!/usr/bin/env bash
[ "$(cat "$FAKE_FLAG" 2>/dev/null)" = up ]
"""


@pytest.fixture
def box(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("systemctl", FAKE_SYSTEMCTL), ("curl", FAKE_CURL)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod((bin_dir / name).stat().st_mode | stat.S_IEXEC)
    config = tmp_path / "configuration"
    config.mkdir()
    (config / "a.ttl").write_text("good\n")
    state = tmp_path / "state"
    state.mkdir()
    host_conf = tmp_path / "host.conf"
    host_conf.write_text(f"FUSEKI_CONFIG_DIR={config}\nFUSEKI_RESTART_DIR={state}\nHEALTH_WAIT=2\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOLONBRIDGE_HOST_CONF": str(host_conf),
        "FUSEKI_CONFIG_DIR": str(config),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_FLAG": str(tmp_path / "flag"),
    }
    return {"env": env, "config": config, "state": state, "log": tmp_path / "calls.log"}


def run(box, request: str | None = None) -> dict | None:
    if request is not None:
        (box["state"] / "restart-fuseki.request").write_text(request)
    subprocess.run(["bash", str(HELPER)], env=box["env"], check=True, capture_output=True, timeout=60)
    result = box["state"] / "restart-fuseki.result.json"
    return json.loads(result.read_text()) if result.exists() else None


def restarts(box) -> int:
    return box["log"].read_text().count("restart ") if box["log"].exists() else 0


def test_a_healthy_restart_is_reported_and_remembered_as_known_good(box):
    result = run(box, json.dumps({"id": ID, "reason": "t"}))

    assert result["ok"] is True and result["rolledBack"] is False and result["id"] == ID
    assert (box["state"] / "last-known-good" / "a.ttl").read_text() == "good\n"
    assert not (box["state"] / "restart-fuseki.request").exists()  # one request, one restart
    assert not (box["state"] / "restart-fuseki.processing").exists()
    assert restarts(box) == 1


def test_a_broken_configuration_is_rolled_back_and_fuseki_comes_back(box):
    run(box, json.dumps({"id": ID}))  # establishes last-known-good
    (box["config"] / "bad.ttl").write_text("BAD\n")
    (box["config"] / "a.ttl").write_text("edited\n")
    other = "f" * 32

    result = run(box, json.dumps({"id": other}))

    assert result["id"] == other and result["ok"] is False and result["rolledBack"] is True
    assert "restored" in result["message"] and "running again" in result["message"]
    assert not (box["config"] / "bad.ttl").exists()
    assert (box["config"] / "a.ttl").read_text() == "good\n"
    assert Path(box["env"]["FAKE_FLAG"]).read_text().strip() == "up"
    assert restarts(box) == 3  # the first, the failed one, and the one after the rollback


def test_with_no_known_good_copy_it_cannot_roll_back_and_says_so(box):
    (box["config"] / "bad.ttl").write_text("BAD\n")

    result = run(box, json.dumps({"id": ID}))

    assert result["ok"] is False and result["rolledBack"] is False
    assert "no last-known-good" in result["message"]
    assert (box["config"] / "bad.ttl").exists()  # nothing was removed on a guess
    assert restarts(box) == 1


@pytest.mark.parametrize(
    "request_body",
    ['{"id": "$(touch /tmp/pwned)"}', '{"id": "short"}', "not json", '{"reason": "no id"}'],
)
def test_a_request_without_a_valid_id_is_ignored_and_nothing_in_it_runs(box, request_body):
    result = run(box, request_body)

    assert result is None
    assert restarts(box) == 0
    assert not (box["state"] / "restart-fuseki.request").exists()
    assert not Path("/tmp/pwned").exists()


def test_no_request_means_nothing_happens(box):
    assert run(box) is None
    assert restarts(box) == 0
