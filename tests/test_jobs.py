"""The daily jobs, scheduled by the agent itself (agent/jobs.py).

Consolidation and the cartographer were host crons: the compose bundle has
no cron, and a desktop app closed at night never reaches one, so in both
they never ran (2026-09-28). These pin the schedule: due a day after the
last run, run only when the agent is quiet, one at a time, with the same
marker file the crons wrote, and a manual run from the dashboard."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent import jobs, live_state, paths


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    jobs._locks.clear()
    jobs._running.clear()
    jobs._waiting.clear()
    live_state.running_tasks.clear()
    live_state.running_planning_turns.clear()
    return tmp_path


def _fake_jobs(monkeypatch, outcomes: dict):
    calls = []

    def make(name):
        async def run(state):
            calls.append(name)
            await asyncio.sleep(0)
            result = outcomes.get(name, {"shop": {"ok": 1}})
            if isinstance(result, Exception):
                raise result
            return result
        return run

    monkeypatch.setattr(jobs, "JOBS", {
        n: jobs.Job(n, j.title, j.marker, j.log, make(n)) for n, j in jobs.JOBS.items()})
    return calls


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_job_is_due_when_never_run_and_a_day_after_its_last_run(data_dir):
    job = jobs.JOBS["consolidation"]
    assert jobs.is_due(job) and jobs.due_at(job) is None
    jobs._write_marker(job, {"ran_at": _stamp(datetime.now(UTC) - timedelta(hours=23)), "ok": True})
    assert not jobs.is_due(job)
    jobs._write_marker(job, {"ran_at": _stamp(datetime.now(UTC) - timedelta(hours=25)), "ok": True})
    assert jobs.is_due(job)


def test_a_crons_marker_counts_the_same(data_dir):
    """A host that still runs the cron wrapper writes the same file; the
    scheduler then simply is not due for a day."""
    (data_dir / "last_cartography.json").write_text(json.dumps({
        "ran_at": _stamp(datetime.now(UTC) - timedelta(hours=1)), "exit_code": 0, "ok": True}))
    assert not jobs.is_due(jobs.JOBS["cartography"])
    assert jobs.status("cartography")["ok"] is True


def test_a_tick_runs_every_due_job_only_when_the_agent_is_quiet(data_dir, monkeypatch):
    calls = _fake_jobs(monkeypatch, {})
    state = SimpleNamespace()
    live_state.running_tasks["t1"] = object()
    assert asyncio.run(jobs.run_due(state)) == [] and calls == []
    assert jobs.status("consolidation")["waiting"] == "due, waiting for the agent to be idle"
    live_state.running_tasks.clear()
    live_state.running_planning_turns["p1"] = object()
    assert asyncio.run(jobs.run_due(state)) == []
    live_state.running_planning_turns.clear()
    assert asyncio.run(jobs.run_due(state)) == ["consolidation", "cartography"]
    assert calls == ["consolidation", "cartography"]
    rec = json.loads((data_dir / "last_consolidation.json").read_text())
    assert rec["ok"] is True and rec["exit_code"] == 0 and rec["trigger"] == "scheduled" and rec["summary"] == {"shop": {"ok": 1}}
    assert "shop: {'ok': 1}" in (data_dir / "consolidation.log").read_text()
    assert jobs.status("consolidation")["waiting"] is None
    assert asyncio.run(jobs.run_due(state)) == [], "not due again for a day"


def test_a_failed_job_writes_a_failed_marker_and_the_reason(data_dir, monkeypatch):
    _fake_jobs(monkeypatch, {"consolidation": jobs.JobFailed("shop: provider said no", {"shop": {"error": "provider said no"}})})
    rec = asyncio.run(jobs.run_job("consolidation", SimpleNamespace(), trigger="scheduled"))
    assert rec["ok"] is False and rec["exit_code"] == 1 and "provider said no" in rec["error"]
    assert "FAILED" in (data_dir / "consolidation.log").read_text()
    _fake_jobs(monkeypatch, {"cartography": RuntimeError("boom")})
    rec = asyncio.run(jobs.run_job("cartography", SimpleNamespace(), trigger="scheduled"))
    assert rec["ok"] is False and rec["error"].startswith("RuntimeError: boom")


def test_a_job_runs_one_at_a_time(data_dir, monkeypatch):
    gate = asyncio.Event()

    async def slow(state):
        await gate.wait()
        return {"shop": {}}

    monkeypatch.setattr(jobs, "JOBS", {"consolidation": jobs.Job("consolidation", "Memory consolidation",
                                                                  "last_consolidation.json", "consolidation.log", slow)})

    async def go():
        first = asyncio.create_task(jobs.run_job("consolidation", SimpleNamespace(), trigger="manual"))
        await asyncio.sleep(0.01)
        assert jobs.status("consolidation")["running"] is True
        with pytest.raises(jobs.JobBusy):
            await jobs.run_job("consolidation", SimpleNamespace(), trigger="manual")
        gate.set()
        return await first

    rec = asyncio.run(go())
    assert rec["ok"] is True and jobs.status("consolidation")["running"] is False


def test_the_dashboard_can_read_and_run_a_job(data_dir, monkeypatch):
    import agent.server as srv
    from agent.auth import User

    calls = _fake_jobs(monkeypatch, {})
    me = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)

    async def no_audit(*a, **k):
        return None

    from agent import audit as audit_module

    monkeypatch.setattr(audit_module, "record", no_audit)
    c = TestClient(srv.app)
    r = c.get("/api/jobs")
    assert r.status_code == 200 and [j["name"] for j in r.json()["jobs"]] == ["consolidation", "cartography"]
    assert r.json()["jobs"][0]["due"] is True and r.json()["jobs"][0]["ran_at"] is None
    assert c.post("/api/jobs/nope/run").status_code == 404
    r = c.post("/api/jobs/consolidation/run")
    assert r.status_code == 202 and r.json() == {"ok": True, "started": "consolidation"}
    for _ in range(50):
        if (data_dir / "last_consolidation.json").exists():
            break
        asyncio.run(asyncio.sleep(0.02))
    rec = json.loads((data_dir / "last_consolidation.json").read_text())
    assert calls == ["consolidation"] and rec["trigger"] == "manual by a@b.co"
    r = c.get("/api/consolidation/status")
    assert r.status_code == 200 and r.json()["trigger"] == "manual by a@b.co" and r.json()["due"] is False
    assert r.json()["due_at"] is not None

    user = User(id=2, email="u@b.co", role="user", allowed_repos=[], totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: user)
    assert c.post("/api/jobs/consolidation/run").status_code == 403


def test_the_loop_settles_then_ticks(data_dir, monkeypatch):
    calls = _fake_jobs(monkeypatch, {})

    async def go():
        task = asyncio.create_task(jobs.run_forever(SimpleNamespace(), settle_s=0.01, check_every_s=0.01))
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(go())
    assert calls == ["consolidation", "cartography"], "ran once each, then not due"
