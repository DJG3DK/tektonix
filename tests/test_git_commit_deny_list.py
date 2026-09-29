"""git_commit refuses build and dependency artifacts by path component.

The deny-list matched "node_modules/" as a substring, so a SYMLINK named
node_modules -- listed bare by `git status`, no trailing slash -- was
committed, and the review sandbox then followed it to whatever the agent
had pointed it at (2026-09-29). A component match refuses the link, the
directory and everything under it, and still lets a file that merely shares
the name through."""
import os
import subprocess

import pytest

from agent.tools import git as gitmod

pytestmark = pytest.mark.asyncio


def _run(args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env=env, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # git_commit takes its identity from the process environment (the
    # bundle and the desktop app set it); a fresh CI runner has none.
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@x"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@x")):
        monkeypatch.setenv(key, value)
    _run(["init", "-q", "-b", "main"], tmp_path)
    (tmp_path / "app.py").write_text("v1\n")
    _run(["add", "-A"], tmp_path)
    _run(["commit", "-q", "-m", "one"], tmp_path)
    return tmp_path


async def test_a_symlink_named_node_modules_is_refused(repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), str(repo / "node_modules"))
    (repo / "app.py").write_text("v2\n")

    res = await gitmod.git_commit(str(repo), "adds a link")

    assert res["ok"] is False
    assert "node_modules" in res["output"] and "refusing" in res["output"]
    assert _run(["log", "--oneline"], repo).stdout.count("\n") == 1, "nothing was committed"


async def test_a_nested_symlink_and_a_real_artifact_directory_are_refused(repo):
    (repo / "apps" / "web").mkdir(parents=True)
    os.symlink("/", str(repo / "apps" / "web" / "node_modules"))
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("x")

    res = await gitmod.git_commit(str(repo), "artifacts")

    assert res["ok"] is False
    assert "apps/web/node_modules" in res["output"]
    assert "dist/" in res["output"]


async def test_a_plain_file_that_shares_a_denied_name_is_committed(repo):
    """`bin/build` is a script, not build output; the component rule must not
    turn the substring match's false positives into new ones."""
    (repo / "bin").mkdir()
    (repo / "bin" / "build").write_text("#!/bin/sh\n")
    (repo / "builder.py").write_text("x\n")
    (repo / "distribution.md").write_text("x\n")

    res = await gitmod.git_commit(str(repo), "a build script")

    assert res["ok"] is True, res["output"]
    assert "bin/build" in _run(["show", "--name-only", "--format=", "HEAD"], repo).stdout


def test_the_component_rule_on_paths_alone(tmp_path):
    root = str(tmp_path)
    assert gitmod._is_denied_artifact(root, "node_modules/")
    assert gitmod._is_denied_artifact(root, "x/node_modules/y.js")
    assert gitmod._is_denied_artifact(root, "x/__pycache__/m.pyc")
    assert gitmod._is_denied_artifact(root, "server.log")
    assert not gitmod._is_denied_artifact(root, "my_node_modules_notes.md")
    assert not gitmod._is_denied_artifact(root, "bin/build")          # no such entry on disk: a file
    assert not gitmod._is_denied_artifact(root, "coverage.py")
