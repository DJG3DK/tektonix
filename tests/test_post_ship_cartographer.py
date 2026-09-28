"""A successful merge+deploy must kick a codebase-map refresh -- and only a
successful one. The map is how planning sessions and build tasks orient
(agent/cartographer.py); before this, it refreshed once nightly, so a plan
started hours after a ship navigated with yesterday's structure.
"""

import httpx
import pytest

import agent.tools.review_gate as rg
from agent import paths


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _client_returning(monkeypatch, merge_body, restart_body=None):
    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            if url.endswith("/merge"):
                return _FakeResponse(merge_body)
            return _FakeResponse(restart_body or {"ok": True})
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)


@pytest.fixture
def kicked(monkeypatch):
    calls = []
    monkeypatch.setattr(rg, "_refresh_codebase_map", lambda project: calls.append(project))
    return calls


async def test_successful_ship_kicks_a_map_refresh(monkeypatch, kicked):
    _client_returning(monkeypatch, {"ok": True}, {"ok": True})
    result = await rg.merge_and_deploy("my-service")
    assert result["ok"] is True
    assert kicked == ["my-service"]


async def test_failed_merge_kicks_nothing(monkeypatch, kicked):
    _client_returning(monkeypatch, {"ok": False, "reason": "review not ready"})
    result = await rg.merge_and_deploy("my-service")
    assert result["ok"] is False
    assert kicked == []


async def test_failed_restart_kicks_nothing(monkeypatch, kicked):
    """A merge whose deploy failed is not shipped -- the map wait is the
    least of the problems, and verify_and_ship will loop back anyway."""
    _client_returning(monkeypatch, {"ok": True}, {"ok": False, "stage": "build"})
    result = await rg.merge_and_deploy("my-service")
    assert result["ok"] is False
    assert kicked == []


async def test_a_failing_kick_does_not_fail_the_ship(monkeypatch):
    """Best-effort by design: the ship succeeded; a broken map refresh must
    surface in the log, never as a ship failure."""
    _client_returning(monkeypatch, {"ok": True}, {"ok": True})

    def _boom(project):
        raise OSError("no such file")

    monkeypatch.setattr(rg.subprocess, "Popen", _boom)
    result = await rg.merge_and_deploy("my-service")  # must not raise
    assert result["ok"] is True


def test_the_kick_spawns_a_detached_cartographer(monkeypatch, tmp_path):
    spawned = {}

    class _FakeProc:
        pass

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        spawned["kw"] = kw
        return _FakeProc()

    monkeypatch.setattr(rg.subprocess, "Popen", fake_popen)
    # open() on the real log path works on this host and Popen is faked, so
    # nothing actually runs.
    rg._refresh_codebase_map("my-service")
    # Paths follow the installation (agent/paths.py), not a fixed /home/tektonix.
    assert spawned["cmd"][-1] == "my-service"
    assert spawned["cmd"][-2].endswith("scripts/run_cartographer.py")
    assert spawned["kw"]["start_new_session"] is True
    assert spawned["kw"]["cwd"] == str(paths.REPO_ROOT)
