"""git_diff sees staged work. A final commit that fails after `git add -A`
leaves the change in the index; the gate read that as "no file changes" and
ended the task as needing none (2026-09-28)."""
import subprocess

import pytest

from agent.tools.git import git_diff

pytestmark = pytest.mark.asyncio


def _run(args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env=env, check=True)


@pytest.fixture
def repo(tmp_path):
    _run(["init", "-q", "-b", "main"], tmp_path)
    (tmp_path / "app.py").write_text("v1\n")
    _run(["add", "-A"], tmp_path)
    _run(["commit", "-q", "-m", "one"], tmp_path)
    return tmp_path


async def test_staged_edits_and_new_files_are_a_diff(repo):
    (repo / "app.py").write_text("v2\n")
    (repo / "new.py").write_text("added\n")
    _run(["add", "-A"], repo)
    diff = await git_diff(str(repo))
    assert "+v2" in diff and "new.py" in diff


async def test_unstaged_and_untracked_still_count_and_a_clean_tree_is_empty(repo):
    assert (await git_diff(str(repo))).strip() == ""
    (repo / "app.py").write_text("v2\n")
    (repo / "new.py").write_text("added\n")
    diff = await git_diff(str(repo))
    assert "+v2" in diff and "new.py" in diff
