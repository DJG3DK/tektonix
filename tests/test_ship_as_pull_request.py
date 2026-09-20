"""Shipping to a repository whose base branch the agent may not write.

The review gate is unchanged and has already passed: this is only what happens
after. A person merges.
"""
from __future__ import annotations

import asyncio

import pytest

from agent import github_repos
from agent.tools import review_gate


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected {self.status_code}")


def _client(post=None, get=None):
    class C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw): return post(url, **kw)
        async def get(self, url, **kw): return get(url, **kw)
    return C


def test_a_new_pull_request_returns_its_url(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, body=kw.get("json"))
        return _Resp(201, {"number": 7, "html_url": "https://github.com/o/r/pull/7", "state": "open"})

    monkeypatch.setattr(github_repos.httpx, "AsyncClient", _client(post=post))
    pr = asyncio.run(github_repos.open_pull_request("tok", "o/r", "agent/t1", "main", "Do a thing"))

    assert pr["url"].endswith("/pull/7")
    assert seen["body"]["head"] == "agent/t1" and seen["body"]["base"] == "main"


def test_an_existing_pull_request_is_a_success_not_a_failure(monkeypatch):
    """A resumed task, or a ship step that re-ran after a blip, has already
    opened one. Treating 422 as an error would turn finished work into a
    failure at the very last step."""
    def post(url, **kw):
        return _Resp(422, {"message": "Validation Failed",
                           "errors": [{"message": "A pull request already exists for o:agent/t1."}]})

    def get(url, **kw):
        return _Resp(200, [{"number": 3, "html_url": "https://github.com/o/r/pull/3", "state": "open"}])

    monkeypatch.setattr(github_repos.httpx, "AsyncClient", _client(post=post, get=get))
    pr = asyncio.run(github_repos.open_pull_request("tok", "o/r", "agent/t1", "main", "Do a thing"))
    assert pr["number"] == "3"


def test_a_real_validation_error_is_still_an_error(monkeypatch):
    def post(url, **kw):
        return _Resp(422, {"message": "Validation Failed",
                           "errors": [{"message": "No commits between main and agent/t1"}]})

    monkeypatch.setattr(github_repos.httpx, "AsyncClient", _client(post=post))
    with pytest.raises(ValueError):
        asyncio.run(github_repos.open_pull_request("tok", "o/r", "agent/t1", "main", "t"))


# --- the ship step itself ---------------------------------------------------

def _ship(monkeypatch, *, project="demo", cfg=None, token="tok", remote="git@github.com:o/r.git",
          push_ok=True, pr=None):
    import agent.config as agent_config
    monkeypatch.setitem(agent_config.PROJECTS, project, cfg or {"live": "/tmp/live"})
    monkeypatch.setattr(agent_config, "load_config", lambda: type("C", (), {"github_token": token})())

    async def fake_git(cmd, root, timeout=30):
        if cmd.startswith("remote get-url"):
            return {"ok": bool(remote), "output": remote or ""}
        if cmd.startswith("push"):
            return {"ok": push_ok, "output": "" if push_ok else "denied"}
        return {"ok": True, "output": ""}

    import agent.tools.git as gitmod
    monkeypatch.setattr(gitmod, "_git", fake_git)

    async def fake_pr(*a, **k):
        if isinstance(pr, Exception):
            raise pr
        return pr or {"number": "9", "url": "https://github.com/o/r/pull/9", "state": "open"}

    monkeypatch.setattr(github_repos, "open_pull_request", fake_pr)
    return asyncio.run(review_gate.ship_as_pull_request(project, "agent/t1", "abc123def456", "Title"))


def test_shipping_opens_the_pull_request_and_reports_its_url(monkeypatch):
    r = _ship(monkeypatch)
    assert r["ok"] and r["shipped"] == "pull_request"
    assert r["pull_request"].endswith("/pull/9")


def test_no_token_says_which_permission_is_missing(monkeypatch):
    """The token is documented read-only elsewhere, so this is the likely
    first failure and the message has to name the scope."""
    r = _ship(monkeypatch, token=None)
    assert r["ok"] is False and "pull_requests: write" in r["error"]


def test_a_project_with_no_github_origin_is_refused_clearly(monkeypatch):
    r = _ship(monkeypatch, remote="")
    assert r["ok"] is False and "no GitHub origin" in r["error"]


def test_a_rejected_push_does_not_claim_a_pull_request(monkeypatch):
    r = _ship(monkeypatch, push_ok=False)
    assert r["ok"] is False and "could not push" in r["error"]
    assert "pull_request" not in r


def test_a_github_refusal_is_reported_rather_than_raised(monkeypatch):
    r = _ship(monkeypatch, pr=PermissionError("403: resource not accessible"))
    assert r["ok"] is False and "could not open a pull request" in r["error"]


# --- authenticating the push ------------------------------------------------

def _ship_capturing_push(monkeypatch, origin, token="tok"):
    """Runs the ship step and returns the push command it issued."""
    import agent.config as agent_config
    import agent.tools.git as gitmod

    monkeypatch.setitem(agent_config.PROJECTS, "demo", {"live": "/tmp/live"})
    monkeypatch.setattr(agent_config, "load_config",
                        lambda: type("C", (), {"github_token": token})())
    monkeypatch.setattr(github_repos, "open_pull_request",
                        lambda *a, **k: _async({"number": "1", "url": "u", "state": "open"}))

    seen = {}

    async def fake_git(cmd, root, timeout=30):
        if cmd.startswith("remote get-url"):
            return {"ok": True, "output": "git@github.com:o/r.git"}
        if cmd.startswith("config --local --get remote.origin.url"):
            return {"ok": True, "output": origin}
        if cmd.startswith("push"):
            seen["cmd"] = cmd
            return {"ok": True, "output": ""}
        return {"ok": True, "output": ""}

    monkeypatch.setattr(gitmod, "_git", fake_git)
    asyncio.run(review_gate.ship_as_pull_request("demo", "agent/t1", "abc123", "T"))
    return seen.get("cmd", "")


def _async(value):
    async def f(*a, **k):
        return value
    return f()


def test_an_https_origin_is_pushed_with_a_credential(monkeypatch):
    """A cloned project's origin has no credentials on it -- the token is
    deliberately never written into .git/config. Without this the push fails
    with "authentication required" at the very last step of a finished task.
    """
    cmd = _ship_capturing_push(monkeypatch, "https://github.com/o/r.git")
    assert "x-access-token:tok@github.com/o/r.git" in cmd
    assert " origin " not in cmd, "the remote name would carry no credentials"


def test_an_ssh_origin_is_pushed_by_remote_name(monkeypatch):
    """That project already has a deploy key wired through core.sshCommand.
    Putting a token on the command line would do nothing except risk logging
    it."""
    cmd = _ship_capturing_push(monkeypatch, "git@github.com:o/r.git")
    assert cmd.strip().endswith("origin agent/t1")
    assert "x-access-token" not in cmd


def test_a_failed_push_does_not_echo_the_token(monkeypatch):
    """git repeats the URL it was given, token and all, and that string goes
    into a task log an operator reads and may paste."""
    import agent.config as agent_config
    import agent.tools.git as gitmod

    monkeypatch.setitem(agent_config.PROJECTS, "demo", {"live": "/tmp/live"})
    monkeypatch.setattr(agent_config, "load_config",
                        lambda: type("C", (), {"github_token": "s3cret"})())

    async def fake_git(cmd, root, timeout=30):
        if cmd.startswith("remote get-url"):
            return {"ok": True, "output": "git@github.com:o/r.git"}
        if cmd.startswith("config --local"):
            return {"ok": True, "output": "https://github.com/o/r.git"}
        if cmd.startswith("push"):
            return {"ok": False,
                    "output": "fatal: could not read from https://x-access-token:s3cret@github.com/o/r.git"}
        return {"ok": True, "output": ""}

    monkeypatch.setattr(gitmod, "_git", fake_git)
    r = asyncio.run(review_gate.ship_as_pull_request("demo", "agent/t1", "abc", "T"))
    assert r["ok"] is False
    assert "s3cret" not in r["error"]
    assert "***" in r["error"]
