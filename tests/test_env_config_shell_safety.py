"""A value saved from the Settings page must never run as shell.

`agent/env_config.py` writes .env, and two cron-driven scripts used to
`. ./.env` to read one value from it. `_format_value` double-quoted
anything with `$` in it and escaped only `\\` and `"`, so a value
containing `$(...)` or a backtick executed the moment the file was
sourced, and an SMTP password with a plain `$` came back mangled. The
written line has to read back identically through python-dotenv (what
the agent uses) and through bash -- or, where no spelling satisfies both,
through dotenv, with the scripts parsing instead of sourcing."""
from __future__ import annotations

import subprocess
import sys

import pytest
from dotenv import dotenv_values

import agent.env_config as ec
from agent import paths

HOSTILE = "a$(touch {marker})b"


def _written(tmp_path, value: str) -> str:
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n")
    ec._write_env(env, {"SMTP_PASS": value})
    return env


def _sourced(env) -> str:
    """The value a shell that sources the file ends up with."""
    return subprocess.run(
        ["bash", "-c", 'set -a; . "$1"; set +a; printf %s "$SMTP_PASS"', "_", str(env)],
        capture_output=True, text=True, check=True,
    ).stdout


def test_a_command_substitution_saved_from_settings_does_not_run_when_the_file_is_sourced(tmp_path):
    marker = tmp_path / "executed"
    env = _written(tmp_path, HOSTILE.format(marker=marker))
    assert _sourced(env) == HOSTILE.format(marker=marker)
    assert not marker.exists(), "the saved value ran as a command"
    assert dotenv_values(env)["SMTP_PASS"] == HOSTILE.format(marker=marker)


@pytest.mark.parametrize("value", [
    "pa$$word", "a`id`b", "x$HOME", "50% off", "with space", 'dq"inside', "eq=sign", "hash#tag", "back\\slash",
])
def test_values_read_back_the_same_through_dotenv_and_through_bash(tmp_path, value):
    env = _written(tmp_path, value)
    assert dotenv_values(env)["SMTP_PASS"] == value
    assert _sourced(env) == value


def test_a_value_with_both_a_dollar_and_a_quote_still_reads_back_through_dotenv(tmp_path):
    """No spelling is literal to both readers here; dotenv is the one the
    agent uses, and the scripts no longer source the file."""
    value = "it's $5"
    env = _written(tmp_path, value)
    assert dotenv_values(env)["SMTP_PASS"] == value


def test_the_helper_the_scripts_use_prints_the_value_and_runs_nothing(tmp_path):
    marker = tmp_path / "executed"
    env = _written(tmp_path, HOSTILE.format(marker=marker))
    out = subprocess.run(
        [sys.executable, str(paths.REPO_ROOT / "scripts/env_value.py"), "SMTP_PASS", str(env)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert out == HOSTILE.format(marker=marker) and not marker.exists()
    missing = subprocess.run(
        [sys.executable, str(paths.REPO_ROOT / "scripts/env_value.py"), "SMTP_PASS", str(tmp_path / "none")],
        capture_output=True, text=True, check=True,
    ).stdout
    assert missing == ""


def test_no_script_sources_the_env_file_any_more():
    """The other half of the fix: the readers parse instead of sourcing."""
    import re
    for script in ("scripts/backup.sh", "scripts/verify_backup_restore.sh"):
        text = (paths.REPO_ROOT / script).read_text()
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
        assert not [ln for ln in code if re.search(r"(^|;)\s*(\.|source)\s+\S*\.env\b", ln)], script
        assert "env_value.py" in text, script
