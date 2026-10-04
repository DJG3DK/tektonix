"""On a Linux host a sandbox's root-made files go back to the agent's user
(2026-10-04, the Linux desktop app). Docker Desktop ignores ownership, so
without PUID nothing changes."""

import subprocess

import pytest

from agent import review_sandbox as rs
from agent.tools import sandbox as sb
from tests.test_review_sandbox import bundle  # noqa: F401 -- the fixture


@pytest.mark.parametrize("uid,gid,want", [
    ("1000", "1000", "1000:1000"),
    ("", "", None),
    ("0", "0", None),
    ("1000", "", None),
    ("1000; rm -rf /", "1000", None),
])
def test_the_owner_comes_from_puid_and_pgid_only_when_they_are_plain_ids(monkeypatch, uid, gid, want):
    monkeypatch.setenv("PUID", uid)
    monkeypatch.setenv("PGID", gid)
    assert sb.sandbox_owner() == want


def test_without_an_owner_the_command_is_execd_exactly_as_before():
    argv = sb._shell("echo hi")
    assert argv == ["sh", "-c",
                    'if command -v bash >/dev/null 2>&1; then exec bash -c "$0"; else exec sh -c "$0"; fi',
                    "echo hi"]
    assert sb.owner_args(None) == []


def test_with_an_owner_files_are_handed_back_and_the_commands_exit_code_kept(tmp_path):
    argv = sb._shell("exit 7", "1000:1000")
    assert "chown -h 1000:1000" in argv[2] and "exec bash" not in argv[2]
    assert sb.owner_args("1000:1000") == [
        "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER"]
    # The wrapper itself, run here with the hand-back pointed at an empty
    # directory: the command's own code comes out.
    script = argv[2].replace("/workspace", str(tmp_path))
    assert subprocess.run(["sh", "-c", script, "exit 7"]).returncode == 7
    assert subprocess.run(["sh", "-c", script, "true"]).returncode == 0


def test_the_hand_back_touches_only_roots_files():
    line = sb.give_back_line("1000:1000")
    assert "-uid 0 -o -gid 0" in line and line.startswith("find /workspace -xdev")


def test_a_review_check_runs_under_sh_with_the_hand_back(monkeypatch, bundle):  # noqa: F811
    monkeypatch.setenv("PUID", "1000")
    monkeypatch.setenv("PGID", "1000")
    argv = rs.build_docker_argv(
        rs.CheckRequest(project="shop", worktree=bundle["wt"], cmd="npm", args=["test"]), "rvw-test")[0]
    assert argv[argv.index("--entrypoint") + 1] == "sh"
    assert argv[argv.index("--cap-add") + 1] == "CHOWN"
    tail = argv[-4:]
    assert tail[0] == "-c" and "chown -h 1000:1000" in tail[1] and tail[2:] == ["npm", "test"]


def test_a_review_check_without_an_owner_is_unchanged(monkeypatch, bundle):  # noqa: F811
    monkeypatch.delenv("PUID", raising=False)
    argv = rs.build_docker_argv(
        rs.CheckRequest(project="shop", worktree=bundle["wt"], cmd="npm", args=["test"]), "rvw-test")[0]
    assert argv[argv.index("--entrypoint") + 1] == "npm" and argv[-1] == "test"
    assert "--cap-add" not in argv
