"""When the ship gate finds no diff, the task record says what git saw."""
import subprocess

import pytest

from agent.tools.git import no_diff_evidence
from agent.nodes import verify_and_ship as vs


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"HOME": str(cwd), "PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.mark.asyncio
async def test_the_line_names_the_branch_the_base_the_lead_and_the_dirty_files(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-qm", "one")
    _git(tmp_path, "checkout", "-qb", "agent/t1")
    (tmp_path / "a.txt").write_text("two\n")
    _git(tmp_path, "commit", "-qam", "two")
    (tmp_path / "b.txt").write_text("new\n")

    line = await no_diff_evidence(str(tmp_path))

    assert line.startswith(f"workspace {tmp_path}: ")
    assert "branch=agent/t1" in line
    assert "ahead=1" in line
    assert "status=1 [?? b.txt]" in line
    assert "stash=0" in line


@pytest.mark.asyncio
async def test_a_missing_base_reads_as_an_error_not_as_zero(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "trunk")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-qm", "one")

    line = await no_diff_evidence(str(tmp_path), "main")

    assert "main=error" in line and "ahead=error" in line


@pytest.mark.asyncio
async def test_a_directory_that_is_not_a_repo_still_yields_a_line(tmp_path):
    line = await no_diff_evidence(str(tmp_path))
    assert line.startswith("workspace ") and "error" in line


def test_the_evidence_reaches_the_log_detail_but_not_the_model():
    state = {"iteration_count": 0}
    out = vs._loop_back("no diff", "keep going", state, no_diff_streak=1, evidence="workspace /w: ahead=0")
    assert out["pending_feedback"] == "keep going"
    assert out["execution_log"][0]["detail"] == "keep going\n\nworkspace /w: ahead=0"
    done = vs._done_no_changes(state, "workspace /w: ahead=0")
    assert done["execution_log"][0]["detail"] == "workspace /w: ahead=0"
