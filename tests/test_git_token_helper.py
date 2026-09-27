"""The credential helper that carries a GitHub token to one git command.

The token used to ride in the URL (`https://x-access-token:<token>@...`),
which put it in git's argv -- /proc/<pid>/cmdline, readable by every local
user -- and in ShellTimeout's message when a push hung. These run the real git
so a quoting slip in the helper shows up as a missing password, not a pass.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess

from agent.tools.git import GIT_TOKEN_ENV, _git, token_credential_args, token_env

_ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _fill(extra_args, env):
    return subprocess.run(
        ["git", *extra_args, "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=o/r.git\n\n",
        capture_output=True, text=True, timeout=30,
        env={**os.environ, **_ISOLATED, **env},
    )


def test_git_gets_the_token_from_the_environment():
    r = _fill(token_credential_args(), token_env("ghp_from_env"))
    assert r.returncode == 0, r.stderr
    assert "username=x-access-token" in r.stdout
    assert "password=ghp_from_env" in r.stdout


def test_the_token_is_not_part_of_the_arguments():
    assert not any("ghp_from_env" in a for a in token_credential_args())


def test_store_and_erase_are_no_ops():
    """git calls the helper with `store` after a successful push; echoing the
    credential back there would be harmless, but writing it anywhere is not."""
    for action in ("store", "erase"):
        r = subprocess.run(
            ["git", *token_credential_args(), "credential", "approve" if action == "store" else "reject"],
            input="protocol=https\nhost=github.com\nusername=x-access-token\npassword=p\n\n",
            capture_output=True, text=True, timeout=30,
            env={**os.environ, **_ISOLATED, **token_env("t")},
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout == ""


def test_the_helper_survives_the_shell_that__git_runs_through(tmp_path):
    """_git joins arguments into one shell string. The helper is itself shell,
    so a quoting mistake would hand git a different helper, or none."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env={**os.environ, **_ISOLATED})
    cmd = shlex.join([*token_credential_args(), "config", "--get-all", "credential.helper"])
    r = asyncio.run(_git(cmd, str(tmp_path), extra_env={**_ISOLATED, **token_env("t")}))
    assert r["ok"], r["output"]
    helpers = r["output"].splitlines()
    assert helpers[-1] == token_credential_args()[-1].split("=", 1)[1]
    assert f"${GIT_TOKEN_ENV}" in helpers[-1], "expanded by git's sh, not by ours"
