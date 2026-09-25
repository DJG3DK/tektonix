"""SWE-bench runs on the analytics page (agent/routers/swebench.py).

Read-only, admin-only, and never reads a file that is not a run's own. A run
that says it is running but whose process has gone reads as stopped, and a
sample is never labelled as a full run.
"""
import base64
import dataclasses
import json
import os
import secrets

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User
from agent.routers import swebench as sw


def _user(role):
    return User(id=1, email=f"{role}@example.com", role=role, allowed_repos=None if role == "admin" else ["p"],
                totp_enabled=True, must_change_password=False, auto_approve_commands=False,
                require_merge_review=True)


def _write(root, name, summary, **files):
    d = root / name
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps(summary))
    for rel, content in files.items():
        p = d / rel.replace("__SLASH__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))
    return d


@pytest.fixture
def client(monkeypatch, tmp_path):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    fake = dataclasses.replace(srv.config, auth_secret_key=key)
    monkeypatch.setattr(srv, "config", fake)
    monkeypatch.setattr(srv.app.state, "config", fake)
    monkeypatch.setattr(sw, "RUNS_DIR", tmp_path)
    iid = "django__django-11265"
    _write(tmp_path, "done-run", {
        "run_id": "done-run", "selection": {"sample": 2}, "started_at": "2026-09-24T12:00:00Z",
        "finished_at": "2026-09-24T13:00:00Z", "total": 2, "graded": True, "resolved": 1, "total_cost_usd": 0.5,
        "official_report": "tektonix.done-run.json",
        "instances": {
            iid: {"task_id": "t1", "outcome": "shipped", "resolved": False, "cost_usd": 0.3,
                  "models": {"agent-coder -> deepseek/x": 5}, "review_verdict": "NEEDS_FIXES",
                  "review": {"verdict": "NEEDS_FIXES", "summary": "The fix misses the nested case.",
                             "findings": [{"severity": "blocking", "file": "x.py", "issue": "nested input still raises"}],
                             "agentMessage": "Fix the nested case."}},
            "psf__requests-1142": {"task_id": "t2", "outcome": "shipped", "resolved": True, "cost_usd": 0.2,
                                   "models": {"agent-coder -> deepseek/x": 2}, "review_verdict": "READY",
                                   "harness_note": "no_tests_collected"},
        }},
        **{"tektonix.done-run.json": {"resolved_ids": ["psf__requests-1142"],
                                      "failure_reasons": {iid: "no_tests_collected"}}},
        **{"predictions.jsonl": json.dumps({"instance_id": iid, "model_patch": "--- a/x.py"}) + "\n",
           f"trajectories__SLASH__{iid}.json": {"task_id": "t1", "threads": [{"generation": 0, "namespace": "coordinator",
               "messages": [{"type": "ai", "data": {"content": "x" * 9000, "tool_calls": [{"name": "grep", "args": {"q": 1}}]}}]}]},
           f"logs__SLASH__run_evaluation__SLASH__done-run__SLASH__tektonix__SLASH__{iid}__SLASH__report.json": {iid: {
               "patch_successfully_applied": True, "tests_status": {
                   "FAIL_TO_PASS": {"success": [], "failure": ["test_with_exclude"]},
                   "PASS_TO_PASS": {"success": ["a", "b"], "failure": []}}}}})
    _write(tmp_path, "dead-run", {"run_id": "dead-run", "state": "running", "pid": 2 ** 22 + 12345,
                                  "selection": {"all": True}, "total": 500, "started_at": "2026-09-24T14:00:00Z",
                                  "instances": {}})
    _write(tmp_path, "live-run", {"run_id": "live-run", "state": "running", "pid": os.getpid(),
                                  "selection": {"sample": 50}, "total": 50, "started_at": "2026-09-24T15:00:00Z",
                                  "graded": False, "resolved_so_far": 3, "graded_so_far": 4, "instances": {}})
    _write(tmp_path, "diag-x", {"run_id": "diag-x", "selection": {"instances": ["a__b-1"]}, "total": 1,
                                "started_at": "2026-09-24T11:00:00Z", "instances": {}})
    (tmp_path / "gold-a").mkdir()
    (tmp_path / "gold-a" / "gold-check.json").write_text(json.dumps(
        {"checked_ids": ["django__django-10097", "django__django-1"], "reference_fails": ["django__django-10097"]}))
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _user("admin"))
    return TestClient(srv.app)


def test_runs_newest_first_with_what_kind_and_state_they_are(client):
    body = client.get("/api/swebench").json()
    by = {r["name"]: r for r in body["runs"]}
    assert [r["name"] for r in body["runs"]] == ["live-run", "dead-run", "done-run", "diag-x"]
    assert by["live-run"]["state"] == "running" and by["live-run"]["resolved"] == 3 and by["live-run"]["graded_count"] == 4
    assert by["dead-run"]["state"] == "stopped", "its process is gone; it cannot still be running"
    assert by["dead-run"]["kind"] == "full" and by["done-run"]["kind"] == "sample" and by["diag-x"]["kind"] == "diagnostic"
    assert by["done-run"]["resolved"] == 1 and by["done-run"]["duration_s"] == 3600
    assert by["done-run"]["models"] == {"agent-coder -> deepseek/x": 7}
    assert body["gold_check"] == {"checked": 2, "reference_fails": ["django__django-10097"]}


def test_a_run_s_tasks_carry_the_harness_s_own_test_results(client):
    tasks = {t["id"]: t for t in client.get("/api/swebench/runs/done-run").json()["tasks"]}
    t = tasks["django__django-11265"]
    assert t["resolved"] is False and t["repo"] == "django/django" and t["has_trajectory"]
    assert t["tests"]["fail_to_pass_failed"] == ["test_with_exclude"] and t["tests"]["pass_to_pass_passed"] == 2
    assert tasks["psf__requests-1142"]["tests"] is None


def test_the_harness_s_own_note_on_a_task_is_shown_from_the_summary_or_its_report(client):
    """pytest's own suite prints "collected 0 items" from inner sessions, so
    the harness flags it `no_tests_collected`: its note, not our failure. A
    run graded before the runner kept it still has it, in the report."""
    tasks = {t["id"]: t for t in client.get("/api/swebench/runs/done-run").json()["tasks"]}
    assert tasks["psf__requests-1142"]["harness_note"] == "no_tests_collected", "from the summary row"
    assert tasks["django__django-11265"]["harness_note"] == "no_tests_collected", "from the official report"


def test_a_task_carries_what_the_reviewer_said_not_only_its_verdict(client):
    body = client.get("/api/swebench/runs/done-run/tasks/django__django-11265").json()
    assert body["review"]["verdict"] == "NEEDS_FIXES"
    assert body["review"]["summary"] == "The fix misses the nested case."
    assert body["review"]["findings"][0]["issue"] == "nested input still raises"
    assert body["review"]["agentMessage"] == "Fix the nested case."
    # A run from before the record was kept: the verdict word alone.
    old = client.get("/api/swebench/runs/done-run/tasks/psf__requests-1142").json()
    assert old["review"] == {"verdict": "READY"} and old["patch"] is None


def test_a_task_s_patch_and_conversation(client):
    body = client.get("/api/swebench/runs/done-run/tasks/django__django-11265").json()
    assert body["patch"] == "--- a/x.py"
    msg = body["conversation"][0]["messages"][0]
    assert msg["role"] == "ai" and len(msg["text"]) < 6100 and "more characters" in msg["text"]
    assert msg["tool_calls"][0]["name"] == "grep"


@pytest.mark.parametrize("path", [
    "/api/swebench/runs/..%2F..%2Fetc", "/api/swebench/runs/nope", "/api/swebench/runs/gold-a",
    "/api/swebench/runs/done-run/tasks/..%2Fsummary", "/api/swebench/runs/done-run/tasks/not-an-id",
])
def test_nothing_but_a_run_s_own_files(client, path):
    """An encoded ../ is normalised away before routing and lands on the
    page's HTML fallback; what matters is that no file comes back."""
    r = client.get(path)
    assert r.status_code in (400, 404) or "application/json" not in r.headers.get("content-type", "")
    assert "run_id" not in r.text and "model_patch" not in r.text


@pytest.mark.parametrize("name", ["../etc", "a/b", "", ".hidden", "x" * 200])
def test_a_run_name_is_matched_never_joined(client, name):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        sw._run_dir(name)


def test_admins_only(client, monkeypatch):
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _user("user"))
    assert client.get("/api/swebench").status_code == 403
    assert client.get("/api/swebench/runs/done-run/tasks/django__django-11265").status_code == 403


def test_a_run_split_into_shards_shows_as_one_run(client, monkeypatch, tmp_path):
    """Two runner processes of one 50-task sample showed as two 25-task runs."""
    for k, tasks in ((1, {"a__a-1": {"task_id": "t1", "outcome": "shipped", "resolved": True, "cost_usd": 0.5}}),
                     (2, {"b__b-2": {"task_id": "t2", "outcome": "shipped", "cost_usd": 0.25}, "c__c-3": {}})):
        _write(tmp_path, f"big-run-s{k}", {
            "run_id": f"big-run-s{k}", "state": "running", "pid": os.getpid(),
            "selection": {"sample": 50, "seed": 1, "shard": f"{k}/2"}, "total": len(tasks),
            "started_at": f"2026-09-25T0{k}:00:00Z", "graded": False, "resolved_so_far": k == 1 and 1 or 0,
            "graded_so_far": 1 if k == 1 else 0, "total_cost_usd": 0.5 if k == 1 else 0.25, "parallel": 3,
            "instances": tasks})
    runs = {r["name"]: r for r in client.get("/api/swebench").json()["runs"]}
    assert "big-run-s1" not in runs and "big-run-s2" not in runs
    big = runs["big-run"]
    assert big["total"] == 3 and big["done"] == 2 and big["resolved"] == 1 and big["graded_count"] == 1
    assert big["total_cost_usd"] == 0.75 and big["parallel"] == 6 and big["state"] == "running"
    assert big["shards"] == ["big-run-s1", "big-run-s2"] and big["started_at"] == "2026-09-25T01:00:00Z"
    detail = client.get("/api/swebench/runs/big-run").json()
    assert {t["id"]: t["run"] for t in detail["tasks"]} == {"a__a-1": "big-run-s1", "b__b-2": "big-run-s2",
                                                            "c__c-3": "big-run-s2"}
