"""scripts/env_audit.py: names and descriptions out of a .env, never values.

The load-bearing test is the sentinel one: a .env full of recognisable secrets
goes through every output format and none of them may leak a fragment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("env_audit", ROOT / "scripts" / "env_audit.py")
env_audit = importlib.util.module_from_spec(_spec)
sys.modules["env_audit"] = env_audit  # dataclasses looks the module up by name
_spec.loader.exec_module(env_audit)

SECRETS = ["sk-live-AAAA1111", "hunter2-ZZZ", "ngrok_2abcDEF9", "p@ss w0rd with spaces", "MULTI-LINE-SECRET-77"]

ENV_TEXT = f"""\
# a comment
BEARER_TOKEN={SECRETS[0]}
export CURRENT_FLAG = "{SECRETS[1]}"
NGROK_AUTHTOKEN='{SECRETS[2]}'
OLD_LEGACY_VAR={SECRETS[3]}
EMPTY_ONE=
MULTI="line one {SECRETS[4]}
FAKE_PHANTOM=inside the quoted value
end of value"
HB_AGENT_TRIAGE_KEY=sk-agent-BBBB2222
DUP=1
DUP=2
# COMMENTED_OUT=never-defined
"""

EXAMPLE_TEXT = """\
# header

# --- section banner ---------------------------------------------------
# Generic note about the auth block, true of nothing in particular.
BEARER_TOKEN=

# Block comment that belongs to CURRENT_FLAG alone.
CURRENT_FLAG=true

#   FLAGGED_OWN   own description that
#                      continues here, and a wrapped line that starts with
#                      BEARER_TOKEN as a word.
# FLAGGED_OWN=x

NGROK_AUTHTOKEN=
DOCS_ONLY=1

# Shared note above two names.
SHARE_A=1
SHARE_B=1
"""

PY = '''\
import os

def _flag(key, default):
    return os.getenv(key, default)

# The bearer token the bridge accepts.
TOKEN = os.getenv("BEARER_TOKEN")

class Settings:
    # CURRENT_FLAG comment written on the field.
    current_flag: bool = False

def build():
    return Settings(current_flag=_flag("CURRENT_FLAG", False))

A = os.environ.get("VIA_GET")
B = os.environ["VIA_SUBSCRIPT"]
C = "VIA_IN" in os.environ
TUPLE = ("MENTIONED_ONLY", "MULTI")
for _name in TUPLE:
    os.getenv(_name)

# Code comment that must lose to the .env.example entry.
OWNED = os.getenv("FLAGGED_OWN")

def dynamic(name):
    return os.environ.get(name.upper())
'''


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "holonbridge").mkdir()
    (tmp_path / "holonbridge" / "config.py").write_text(PY)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts" / "ctl.sh").write_text(
        'grep NGROK_AUTHTOKEN .env\necho "$OLD_LEGACY_VARIANT" "$X_OLD_LEGACY_VAR"\n'
    )
    (tmp_path / "tests" / "helper.sh").write_text("echo $OLD_LEGACY_VAR\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.yml").write_text("OLD_LEGACY_VAR: documented\n")
    (tmp_path / ".env.example").write_text(EXAMPLE_TEXT)
    (tmp_path / "tests" / "t.py").write_text('X = "OLD_LEGACY_VAR"\n')  # tests do not count as use
    return tmp_path


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "live.env"
    path.write_text(ENV_TEXT)
    return path


@pytest.fixture
def report(repo, env_file):
    return env_audit.build_report(env_file, repo)


def names(section):
    return {item["name"] for item in section}


# --- the promise: no values ----------------------------------------------------------


def test_no_fragment_of_any_value_reaches_any_output(repo, env_file, tmp_path, capsys):
    out_md, out_json = tmp_path / "r.md", tmp_path / "r.json"
    assert env_audit.main(["--env", str(env_file), "--repo", str(repo), "--out", str(out_md)]) == 0
    assert env_audit.main(["--env", str(env_file), "--repo", str(repo), "--format", "json",
                           "--out", str(out_json)]) == 0
    assert env_audit.main(["--env", str(env_file), "--repo", str(repo)]) == 0
    captured = capsys.readouterr()
    everything = out_md.read_text() + out_json.read_text() + captured.out + captured.err
    for secret in SECRETS + ["sk-agent-BBBB2222", "inside the quoted value", "end of value",
                            "line one", "hunter", "ngrok_2"]:
        assert secret not in everything, secret


def test_the_report_does_not_say_whether_a_variable_is_empty_or_quoted(report):
    allowed = {"name", "description", "description_source", "status", "read_at", "tooling_at",
               "defined_times"}
    for section in ("current", "scripts_only", "orphan_candidates", "read_by_code_not_defined",
                    "documented_but_not_read"):
        for item in report[section]:
            assert set(item) <= allowed, item
    assert next(i for i in report["orphan_candidates"] if i["name"] == "EMPTY_ONE")["status"] == "unknown"


def test_names_come_out_of_the_parser_and_values_do_not(env_file):
    parsed = env_audit.env_names(env_file)
    assert parsed["BEARER_TOKEN"] == 1 and parsed["EMPTY_ONE"] == 1
    assert all(isinstance(count, int) for count in parsed.values())


def test_a_line_inside_a_multiline_quoted_value_is_not_a_variable(env_file):
    parsed = env_audit.env_names(env_file)
    assert "FAKE_PHANTOM" not in parsed and "MULTI" in parsed


def test_commented_out_assignments_are_not_defined(env_file):
    assert "COMMENTED_OUT" not in env_audit.env_names(env_file)


def test_a_repeated_name_is_flagged(report):
    dup = next(i for i in report["orphan_candidates"] if i["name"] == "DUP")
    assert dup["defined_times"] == 2


# --- verdicts ---------------------------------------------------------------------------


def test_every_kind_of_read_makes_a_name_current(repo, tmp_path):
    env = tmp_path / "e.env"
    env.write_text("VIA_GET=1\nVIA_SUBSCRIPT=1\nVIA_IN=1\nCURRENT_FLAG=1\nBEARER_TOKEN=1\n")
    current = names(env_audit.build_report(env, repo)["current"])
    assert current == {"VIA_GET", "VIA_SUBSCRIPT", "VIA_IN", "CURRENT_FLAG", "BEARER_TOKEN"}


def test_a_name_read_through_a_helper_is_found_by_the_ast(report):
    item = next(i for i in report["current"] if i["name"] == "CURRENT_FLAG")
    assert item["status"] == "read"


def test_a_name_only_listed_in_a_tuple_is_current_not_an_orphan(report):
    item = next(i for i in report["current"] if i["name"] == "MULTI")
    assert item["status"] == "mentioned"


def test_a_name_used_only_by_scripts_is_reported_apart(report):
    assert names(report["scripts_only"]) == {"NGROK_AUTHTOKEN"}


def test_an_unreferenced_name_is_an_orphan_candidate_and_tests_do_not_save_it(report):
    assert "OLD_LEGACY_VAR" in names(report["orphan_candidates"])


def test_a_name_that_merely_contains_a_known_name_is_not_a_tooling_hit(report):
    # ctl.sh mentions OLD_LEGACY_VARIANT and X_OLD_LEGACY_VAR, tests/ and docs/ mention the
    # real name; none of that may rescue OLD_LEGACY_VAR
    assert "OLD_LEGACY_VAR" not in names(report["scripts_only"])


def test_the_agent_credential_family_is_current(report):
    item = next(i for i in report["current"] if i["name"] == "HB_AGENT_TRIAGE_KEY")
    assert item["status"] == "dynamic family"


def test_names_read_by_code_but_not_defined_are_listed(report):
    assert {"VIA_GET", "VIA_SUBSCRIPT", "VIA_IN"} <= names(report["read_by_code_not_defined"])


def test_a_documented_name_nothing_refers_to_is_reported(report):
    stale = {i["name"]: i["status"] for i in report["documented_but_not_read"]}
    assert stale["DOCS_ONLY"] == "docs only"
    assert stale["NGROK_AUTHTOKEN"] == "tooling"


def test_computed_reads_are_listed_so_an_orphan_verdict_can_be_doubted(report):
    assert any(where.startswith("holonbridge/config.py:") for where in report["dynamic_reads"])


# --- descriptions ---------------------------------------------------------------------


def test_a_names_own_entry_wins_and_includes_its_wrapped_lines(repo, tmp_path):
    env = tmp_path / "e.env"
    env.write_text("FLAGGED_OWN=1\n")
    item = env_audit.build_report(env, repo)["current"][0]
    assert item["description"].startswith("own description that continues here")
    assert "BEARER_TOKEN as a word" in item["description"]
    assert item["description_source"] == ".env.example"


def test_a_wrapped_line_that_starts_with_a_name_does_not_become_that_names_entry(repo):
    own, _ = env_audit.example_descriptions(repo / ".env.example")
    assert "BEARER_TOKEN" not in own


def test_a_comment_in_the_code_outranks_a_nearby_block(report):
    item = next(i for i in report["current"] if i["name"] == "CURRENT_FLAG")
    assert item["description"] == "CURRENT_FLAG comment written on the field."
    assert item["description_source"] == "code comment"


def test_a_banner_is_never_a_description_and_a_block_is_used_once(repo):
    _, block = env_audit.example_descriptions(repo / ".env.example")
    assert "section banner" not in " ".join(block.values())
    assert block["BEARER_TOKEN"] == "Generic note about the auth block, true of nothing in particular."
    assert block["NGROK_AUTHTOKEN"] == ""  # the block above it was already consumed
    assert block["SHARE_A"] == "Shared note above two names."
    assert block["SHARE_B"] == ""


def test_a_nearby_comment_is_marked_as_a_guess_in_the_markdown(repo, tmp_path):
    env = tmp_path / "e.env"
    env.write_text("NOT_IN_CODE=1\nBEARER_TOKEN=1\n")
    text = env_audit.render_markdown(env_audit.build_report(env, repo))
    assert "†" in text


# --- command line -----------------------------------------------------------------------


def test_a_missing_env_file_is_an_error_not_a_crash(repo, tmp_path, capsys):
    assert env_audit.main(["--env", str(tmp_path / "nope.env"), "--repo", str(repo)]) == 2
    assert "not a file" in capsys.readouterr().err


def test_a_directory_that_is_not_the_repo_is_an_error(env_file, tmp_path, capsys):
    assert env_audit.main(["--env", str(env_file), "--repo", str(tmp_path)]) == 2
    assert "--repo" in capsys.readouterr().err


def test_out_writes_the_file_and_keeps_stdout_clean(repo, env_file, tmp_path, capsys):
    target = tmp_path / "report.md"
    assert env_audit.main(["--env", str(env_file), "--repo", str(repo), "--out", str(target)]) == 0
    assert capsys.readouterr().out == ""
    assert "# Environment variable audit" in target.read_text()


# --- against this repository ------------------------------------------------------------


def test_every_variable_documented_in_env_example_is_still_referenced_somewhere():
    """Documentation that outlives its code is how orphans start: this fails when
    ``.env.example`` documents a name nothing in the repository refers to."""
    report = env_audit.build_report(ROOT / ".env.example", ROOT)
    assert not report["orphan_candidates"], names(report["orphan_candidates"])
    unread = [i["name"] for i in report["documented_but_not_read"] if i["status"] != "tooling"]
    assert not unread, unread


def test_the_real_report_marks_the_json_ld_settings_current():
    report = env_audit.build_report(ROOT / ".env.example", ROOT)
    assert "JSONLD_MAX_BYTES" in names(report["read_by_code_not_defined"])
