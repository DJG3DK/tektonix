"""The eval suite from the dashboard: history, a run's report, start and stop.

A run spends money and an hour, and is a detached process; these pin that the
routes are admin-only, never start a second run, never read a file that is
not a run's report, and hand the runner exactly the command it would get
from a shell.
"""
import base64
import dataclasses
import json
import os
import secrets
import signal
import time

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User
from agent.evals import status as ev_status
from agent.routers import evals as evals_routes


def _user(role):
    return User(id=1, email=f"{role}@example.com", role=role, allowed_repos=None if role == "admin" else ["p"],
                totp_enabled=True, must_change_password=False, auto_approve_commands=False,
                require_merge_review=True)


def _report(started, finished, passed, total, cost, failed=()):
    tasks = [{"id": f"t{i}", "passed": f"t{i}" not in failed, "category": "bug-fix"} for i in range(total)]
    return {"started_at": started, "finished_at": finished, "notes": "n", "tasks_total": total,
            "tasks_attempted": total, "tasks_passed": passed, "pass_rate": round(100 * passed / total, 1),
            "total_cost_usd": cost, "stopped_early": False,
            "by_category": {"bug-fix": {"tasks": total, "passed": passed}},
            "benchmarks": {"first_pass_rate": 100.0}, "tasks": tasks}


@pytest.fixture
def client(monkeypatch, tmp_path):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    fake = dataclasses.replace(srv.config, auth_secret_key=key)
    monkeypatch.setattr(srv, "config", fake)
    monkeypatch.setattr(srv.app.state, "config", fake)
    monkeypatch.setattr(srv.app.state, "store", None, raising=False)
    monkeypatch.setattr(evals_routes, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(evals_routes, "RUN_LOG", tmp_path / "last-run.log")
    monkeypatch.setattr(ev_status, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(evals_routes, "_suite", lambda: {"tasks": 4, "by_category": {"bug-fix": 4},
                                                         "ids": ["t0", "t1", "t2", "t3"]})
    (tmp_path / "2026-09-22T12-00-00Z.json").write_text(json.dumps(
        _report("2026-09-22T12:00:00Z", "2026-09-22T13:00:00Z", 3, 4, 0.20, failed=("t2",))))
    (tmp_path / "2026-09-23T12-00-00Z.json").write_text(json.dumps(
        _report("2026-09-23T12:00:00Z", "2026-09-23T12:40:00Z", 4, 4, 0.16)))
    (tmp_path / "status.json.tmp").write_text("{}")          # never mistaken for a run
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _user("admin"))
    spawned = []
    monkeypatch.setattr(evals_routes, "_spawn", lambda cmd: spawned.append(cmd))
    c = TestClient(srv.app)
    c.spawned = spawned
    return c


def test_history_newest_first_with_scorecard_numbers(client):
    body = client.get("/api/evals").json()
    names = [r["name"] for r in body["runs"]]
    assert names == ["2026-09-23T12-00-00Z", "2026-09-22T12-00-00Z"]
    latest = body["runs"][0]
    assert latest["tasks_passed"] == 4 and latest["duration_s"] == 2400 and latest["failed"] == []
    assert body["runs"][1]["failed"] == ["t2"]
    assert body["suite"]["tasks"] == 4
    # Scaled from the newest complete run: 4 tasks, $0.16, 40 minutes.
    assert body["estimate"] == {"cost_usd": 0.16, "duration_s": 2400, "from_run": "2026-09-23T12-00-00Z"}


def test_a_run_report_by_name_and_only_by_name(client):
    assert client.get("/api/evals/runs/2026-09-23T12-00-00Z").json()["summary"]["tasks_passed"] == 4
    assert client.get("/api/evals/runs/status").status_code == 400
    # A traversal never reaches a file read: straight to the handler (over
    # HTTP, the encoded slashes stop it matching the route at all).
    import asyncio
    from fastapi import HTTPException
    for bad in ("../../etc/passwd", "..", "2026-09-23T12-00-00Z/../x", "status"):
        with pytest.raises(HTTPException) as e:
            asyncio.run(evals_routes.get_eval_run(bad, user=_user("admin")))
        assert e.value.status_code == 400
    assert client.get("/api/evals/runs/2020-01-01T00-00-00Z").status_code == 404


def test_admin_only(client, monkeypatch):
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _user("user"))
    assert client.get("/api/evals").status_code == 403
    assert client.post("/api/evals/run", json={}).status_code == 403
    assert client.post("/api/evals/stop").status_code == 403


def test_starting_a_run_hands_the_runner_the_shell_command(client):
    r = client.post("/api/evals/run", json={"notes": "before the prompt change", "only": ["t1", "t3"]})
    assert r.status_code == 202 and r.json()["tasks"] == 2
    cmd = client.spawned[0]
    assert cmd[1].endswith("scripts/run_evals.py")
    assert cmd[2:] == ["--status-file", "--notes", "before the prompt change", "--only", "t1", "t3"]


def test_a_run_can_ask_for_several_tasks_at_once(client):
    r = client.post("/api/evals/run", json={"notes": "n", "parallel": 3})
    assert r.status_code == 202
    assert client.spawned[0][2:] == ["--status-file", "--notes", "n", "--parallel", "3"]
    assert client.post("/api/evals/run", json={"parallel": 9}).status_code == 422


def test_a_second_run_is_refused_while_one_is_claimed_or_running(client):
    assert client.post("/api/evals/run", json={}).status_code == 202        # claimed, no pid yet
    assert client.post("/api/evals/run", json={}).status_code == 409
    ev_status.start(None, notes="", only=None, tasks_total=4)               # the child's own record
    assert client.post("/api/evals/run", json={}).status_code == 409
    assert len(client.spawned) == 1


def test_an_unknown_task_is_refused_before_anything_starts(client):
    assert client.post("/api/evals/run", json={"only": ["nope"]}).status_code == 400
    assert client.spawned == []


def test_a_claim_that_never_became_a_process_expires(client, monkeypatch):
    ev_status.claim(notes="", only=None, tasks_total=4)
    later = time.time() + ev_status.CLAIM_GRACE_S + 60
    monkeypatch.setattr(ev_status.time, "time", lambda: later)
    assert ev_status.read()["running"] is False


def test_stop_signals_the_runs_whole_session(client, monkeypatch):
    ev_status.start(None, notes="", only=None, tasks_total=4)   # pid = this process: alive
    sent = []
    monkeypatch.setattr(evals_routes.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    assert client.post("/api/evals/stop").status_code == 200
    assert sent == [(os.getpid(), signal.SIGINT)]
    ev_status.finish(None, exit_code=130, report=None)
    assert client.post("/api/evals/stop").status_code == 409


def test_the_runner_writes_its_progress(tmp_path):
    """The file the dashboard polls, written the way the runner writes it."""
    path = tmp_path / "status.json"
    ev_status.start(path, notes="x", only=["a"], tasks_total=2)
    ev_status.task_done(path, task_id="a", passed=True, cost_usd=0.02, outcome="shipped")
    ev_status.task_done(path, task_id="b", passed=False, cost_usd=0.03, outcome="escalated")
    rec = ev_status.read(path)
    assert rec["running"] is True and rec["done"] == 2 and rec["passed"] == 1 and rec["spent_usd"] == 0.05
    ev_status.finish(path, exit_code=1, report="logs/evals/x.json")
    assert ev_status.read(path)["running"] is False
