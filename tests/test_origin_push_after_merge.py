"""Getting a merged base branch to GitHub when the remote is HTTPS.

The review service pushes after a merge with `git push origin` and no
credentials. That works for an SSH origin carrying a deploy key. A cloned
project has an https origin with nothing on it -- the token is deliberately
never written into .git/config -- so the merge landed locally and GitHub
silently stayed behind, because that push is best-effort by design.
"""
from __future__ import annotations

import asyncio

import pytest

from agent.tools import review_gate


def _wire(monkeypatch, *, origin, token="tok", push_ok=True, live="/tmp/live"):
    import agent.config as agent_config
    import agent.tools.git as gitmod
    from agent import github_settings

    monkeypatch.setitem(agent_config.PROJECTS, "demo", {"live": live} if live else {})
    monkeypatch.setattr(agent_config, "load_config",
                        lambda: type("C", (), {"github_token": token})())
    monkeypatch.setattr(github_settings, "current", lambda: {"tokens": {}, "projects": {}})
    monkeypatch.setattr(github_settings, "token_for", lambda s, c, r: token)

    seen = {}

    async def fake_git(cmd, root, timeout=30):
        if cmd.startswith("config --local"):
            return {"ok": True, "output": origin}
        if cmd.startswith("push"):
            seen["cmd"] = cmd
            return {"ok": push_ok,
                    "output": "" if push_ok else f"fatal: denied for {cmd.split()[1]}"}
        return {"ok": True, "output": ""}

    monkeypatch.setattr(gitmod, "_git", fake_git)
    return seen


def test_an_https_origin_is_pushed_with_the_token(monkeypatch):
    seen = _wire(monkeypatch, origin="https://github.com/o/r.git")
    r = asyncio.run(review_gate._push_https_origin_if_needed("demo"))
    assert r == {"ok": True, "pushed": "main"}
    assert "x-access-token:tok@github.com/o/r.git" in seen["cmd"]
    assert seen["cmd"].endswith("main"), "the base branch, not a task branch"


def test_an_ssh_origin_is_left_to_its_deploy_key(monkeypatch):
    """That project already pushes fine. Doing it again with a token would add
    nothing but a second way to leak one."""
    seen = _wire(monkeypatch, origin="git@github.com:o/r.git")
    assert asyncio.run(review_gate._push_https_origin_if_needed("demo")) is None
    assert "cmd" not in seen


def test_a_project_with_no_origin_is_not_an_error(monkeypatch):
    seen = _wire(monkeypatch, origin="")
    assert asyncio.run(review_gate._push_https_origin_if_needed("demo")) is None
    assert "cmd" not in seen


def test_no_token_says_the_origin_stays_behind(monkeypatch):
    """Silence here is the original bug. Saying it is the fix."""
    _wire(monkeypatch, origin="https://github.com/o/r.git", token=None)
    r = asyncio.run(review_gate._push_https_origin_if_needed("demo"))
    assert r["ok"] is False and "stays behind" in r["reason"]


def test_a_failed_push_is_reported_without_the_token(monkeypatch):
    _wire(monkeypatch, origin="https://github.com/o/r.git", token="s3cret", push_ok=False)
    r = asyncio.run(review_gate._push_https_origin_if_needed("demo"))
    assert r["ok"] is False
    assert "s3cret" not in r["reason"] and "***" in r["reason"]


def test_a_base_branch_that_is_not_main_is_honoured(monkeypatch):
    import agent.config as agent_config
    seen = _wire(monkeypatch, origin="https://github.com/o/r.git")
    monkeypatch.setitem(agent_config.PROJECTS, "demo",
                        {"live": "/tmp/live", "base_branch": "develop"})
    asyncio.run(review_gate._push_https_origin_if_needed("demo"))
    assert seen["cmd"].endswith("develop")


def test_an_https_origin_that_is_not_github_is_reported_not_guessed(monkeypatch):
    _wire(monkeypatch, origin="https://gitlab.com/o/r.git")
    r = asyncio.run(review_gate._push_https_origin_if_needed("demo"))
    assert r["ok"] is False and "not a GitHub URL" in r["reason"]
