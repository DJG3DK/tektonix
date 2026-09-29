"""A project's base_branch reaches shell commands: tools/git.py builds
`rev-list --count {base}..HEAD` and friends for create_subprocess_shell, and
the review gate does the same. Only a hand-edited projects.json sets it,
so this is hardening, not an exploit path -- but a typo there must not run
as shell (2026-09-29). Two layers: the loader drops a value that is not a
branch name, and tools/git.py quotes whatever it is handed."""
import json
import subprocess

import pytest

from agent import config as agent_config
from agent.tools.git import commits_ahead, no_diff_evidence


def _repo(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True, env=env)
    (tmp_path / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=tmp_path, check=True, env=env)
    return tmp_path


@pytest.mark.parametrize("ref", ["main; touch pwned", "main$(touch pwned)", "`touch pwned`", "main && touch pwned"])
@pytest.mark.asyncio
async def test_a_base_ref_is_one_argument_to_git_not_shell(tmp_path, ref):
    root = _repo(tmp_path)
    assert await commits_ahead(str(root), ref) == 0
    line = await no_diff_evidence(str(root), ref)
    assert "ahead=error" in line
    assert not (root / "pwned").exists(), f"{ref!r} ran as shell"


@pytest.mark.asyncio
async def test_a_real_base_ref_still_works_quoted(tmp_path):
    root = _repo(tmp_path)
    assert await commits_ahead(str(root), "main") == 0
    assert "ahead=0" in await no_diff_evidence(str(root), "main")


@pytest.mark.parametrize("bad", ["main; rm -rf /", "a b", "-x", "a..b", "x/.hidden", "y.lock", "z/", "w.", "t@{1}", "a~1", "", 7])
def test_the_loader_drops_a_base_branch_that_is_not_a_branch_name(tmp_path, monkeypatch, bad):
    cfg = tmp_path / "projects.json"
    cfg.write_text(json.dumps({"projects": {"p": {"live": "/x", "sandbox": "/x", "base_branch": bad}}}))
    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", cfg)
    data = agent_config._load_projects_config()
    assert "base_branch" not in data["projects"]["p"], f"{bad!r} was kept as a base branch"
    assert not agent_config.valid_branch_name(bad)


@pytest.mark.parametrize("good", ["main", "develop", "release/2026.09", "feature-x_1", "v1.2.3", "trunk"])
def test_ordinary_branch_names_are_kept(tmp_path, monkeypatch, good):
    cfg = tmp_path / "projects.json"
    cfg.write_text(json.dumps({"projects": {"p": {"live": "/x", "sandbox": "/x", "base_branch": good}}}))
    monkeypatch.setattr(agent_config, "_PROJECTS_CONFIG_PATH", cfg)
    assert agent_config._load_projects_config()["projects"]["p"]["base_branch"] == good
    assert agent_config.valid_branch_name(good)
