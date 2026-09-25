"""SWE-bench Verified runs, on the analytics page: the score, each task, its
patch and the agent's whole conversation.

Read-only. A run is `scripts/run_swebench.py`, started from a shell: it pulls
gigabytes of images, runs for hours and spends real money, and what makes its
number a SWE-bench number is spelled out in evals/SWEBENCH.md. This only reads
what the runner leaves in logs/swebench/<run>/ -- summary.json (rewritten
after every task, so a run shows as it goes), predictions.jsonl, the official
harness's reports, and trajectories/<id>.json.

Admin-only, like the golden suite: the trajectories hold every tool call and
file the agent read.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from agent import auth, paths
from agent.auth import User, require_full_auth
from agent.routers.evals import _parse_ts

router = APIRouter(tags=["swebench"])

RUNS_DIR = paths.REPO_ROOT / "logs" / "swebench"
DATASET_SIZE = 500
_RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_INSTANCE = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$")
# A message's text and a tool call's arguments, cut to this for the page.
# The file on disk keeps everything; this is for reading, not for the record.
_TEXT_LIMIT = 6000


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _run_dir(name: str) -> Path:
    # Matched, not joined: the name becomes a path.
    if not _RUN_NAME.match(name):
        raise HTTPException(400, "not a run name")
    d = RUNS_DIR / name
    if not (d / "summary.json").is_file():
        raise HTTPException(404, "no such run")
    return d


def _kind(s: dict) -> str:
    sel = s.get("selection") or {}
    if s.get("diagnostic") or str(s.get("run_id", "")).startswith("diag-"):
        return "diagnostic"
    if sel.get("all") and (s.get("total") or 0) >= DATASET_SIZE:
        return "full"
    if sel.get("sample"):
        return "sample"
    return "selected"


def _state(s: dict) -> str:
    """What the run is doing. A run from before `state` was recorded is done;
    one that says running but whose process is gone died without saying so."""
    state = s.get("state") or "done"
    if state == "running" and not _alive(s.get("pid")):
        return "stopped"
    return state


def summary(name: str, s: dict) -> dict:
    instances = s.get("instances") or {}
    started, finished = _parse_ts(s.get("started_at")), _parse_ts(s.get("finished_at"))
    models: dict[str, int] = {}
    for row in instances.values():
        for model, n in (row.get("models") or {}).items():
            models[model] = models.get(model, 0) + int(n)
    done = sum(1 for r in instances.values() if r.get("task_id") or r.get("outcome") not in (None, "not_run"))
    return {
        "name": name,
        "kind": _kind(s),
        "state": _state(s),
        "notes": s.get("notes") or "",
        "started_at": s.get("started_at"),
        "finished_at": s.get("finished_at"),
        "duration_s": round(finished - started) if started and finished else None,
        "total": s.get("total") or len(instances),
        "done": done,
        "graded": bool(s.get("graded")),
        "resolved": s.get("resolved") if s.get("graded") else s.get("resolved_so_far"),
        "graded_count": len(instances) if s.get("graded") else (s.get("graded_so_far") or 0),
        "resolved_rate": s.get("resolved_rate"),
        "total_cost_usd": s.get("total_cost_usd"),
        "stopped_early": s.get("stopped_early"),
        "parallel": s.get("parallel"),
        "budget_usd": s.get("budget_usd"),
        "models": dict(sorted(models.items(), key=lambda kv: -kv[1])),
    }


_SHARD_NAME = re.compile(r"^(?P<base>.+)-s(?P<k>\d+)$")


def _shard_base(name: str, s: dict) -> str | None:
    """The run a shard belongs to: `<base>-s<k>` with a `--shard k/n` selection."""
    m = _SHARD_NAME.match(name)
    shard = (s.get("selection") or {}).get("shard")
    return m.group("base") if m and shard else None


def _all_summaries() -> dict[str, dict]:
    out = {}
    if RUNS_DIR.is_dir():
        for d in RUNS_DIR.iterdir():
            s = _load(d / "summary.json") if d.is_dir() else None
            if s is not None:
                out[d.name] = s
    return out


def _groups(all_s: dict[str, dict]) -> dict[str, list[str]]:
    """base name -> its shards' run names, for runs split with --shard."""
    groups: dict[str, list[str]] = {}
    for name, s in all_s.items():
        base = _shard_base(name, s)
        if base:
            groups.setdefault(base, []).append(name)
    return {b: sorted(ns) for b, ns in groups.items()}


def combined(base: str, parts: list[dict]) -> dict:
    """One run split into shards (--shard K/N), shown as the run it is: its
    totals summed, its state the least finished of its shards."""
    states = {p["state"] for p in parts}
    graded = all(p["graded"] for p in parts)
    models: dict[str, int] = {}
    for p in parts:
        for m, n in p["models"].items():
            models[m] = models.get(m, 0) + n
    started = [p["started_at"] for p in parts if p["started_at"]]
    finished = [p["finished_at"] for p in parts if p["finished_at"]]
    total = sum(p["total"] for p in parts)
    resolved = sum(p["resolved"] or 0 for p in parts)
    t0, t1 = _parse_ts(min(started)) if started else None, _parse_ts(max(finished)) if finished else None
    return {
        **parts[0],
        "name": base,
        "state": "running" if "running" in states else ("stopped" if "stopped" in states else "done"),
        "notes": parts[0]["notes"],
        "started_at": min(started) if started else None,
        "finished_at": max(finished) if finished else None,
        "duration_s": round(t1 - t0) if t0 and t1 and "running" not in states else None,
        "total": total,
        "done": sum(p["done"] for p in parts),
        "graded": graded,
        "resolved": resolved,
        "graded_count": sum(p["graded_count"] for p in parts),
        "resolved_rate": round(100 * resolved / total, 1) if graded and total else None,
        "total_cost_usd": round(sum(p["total_cost_usd"] or 0 for p in parts), 4),
        "stopped_early": "; ".join(p["stopped_early"] for p in parts if p["stopped_early"]) or None,
        "parallel": sum(p["parallel"] or 0 for p in parts),
        "models": dict(sorted(models.items(), key=lambda kv: -kv[1])),
        "shards": [p["name"] for p in parts],
    }


def _harness_tests(run_dir: Path, run_id: str, iid: str) -> dict | None:
    """Which graded tests failed, from the official harness's own report."""
    rep = _load(run_dir / "logs" / "run_evaluation" / run_id / "tektonix" / iid / "report.json")
    if not rep or iid not in rep:
        return None
    ts = rep[iid].get("tests_status") or {}
    return {
        "patch_applied": rep[iid].get("patch_successfully_applied"),
        "fail_to_pass_failed": (ts.get("FAIL_TO_PASS") or {}).get("failure") or [],
        "fail_to_pass_passed": len((ts.get("FAIL_TO_PASS") or {}).get("success") or []),
        "pass_to_pass_failed": (ts.get("PASS_TO_PASS") or {}).get("failure") or [],
        "pass_to_pass_passed": len((ts.get("PASS_TO_PASS") or {}).get("success") or []),
    }


def _harness_notes(run_dir: Path, s: dict, run_id: str) -> dict:
    """The harness's own reading of each unresolved task (`failure_reasons`
    in its report), for runs graded before the runner kept it in the
    summary. `no_tests_collected` on pytest's own suite is the inner
    sessions printing "collected 0 items", not our failure (2026-09-25)."""
    rep = _load(run_dir / (s.get("official_report") or f"tektonix.{run_id}.json")) or {}
    notes = rep.get("failure_reasons")
    return notes if isinstance(notes, dict) else {}


def _gold_checks() -> dict:
    """Every reference-fix check on disk, merged: the tasks whose OFFICIAL fix
    fails in the official image, which no agent can resolve."""
    checked: set[str] = set()
    fails: set[str] = set()
    for path in sorted(RUNS_DIR.glob("*/gold-check.json")):
        g = _load(path) or {}
        checked.update(g.get("checked_ids") or [])
        fails.update(g.get("reference_fails") or [])
    return {"checked": len(checked | fails), "reference_fails": sorted(fails)}


@router.get("/api/swebench")
async def list_runs(user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    all_s = _all_summaries()
    groups = _groups(all_s)
    in_group = {n for ns in groups.values() for n in ns}
    runs = [summary(name, s) for name, s in all_s.items() if name not in in_group]
    runs += [combined(base, [summary(n, all_s[n]) for n in names]) for base, names in groups.items()]
    runs.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return {"runs": runs[:50], "dataset_size": DATASET_SIZE, "gold_check": _gold_checks()}


@router.get("/api/swebench/runs/{name}")
async def get_run(name: str, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if not _RUN_NAME.match(name):
        raise HTTPException(400, "not a run name")
    all_s = _all_summaries()
    shards = _groups(all_s).get(name)
    if shards and not (RUNS_DIR / name / "summary.json").is_file():
        parts = [_run_tasks(n) for n in shards]
        return {"summary": combined(name, [p["summary"] for p in parts]),
                "tasks": [t for p in parts for t in p["tasks"]]}
    return _run_tasks(name)


def _run_tasks(name: str) -> dict:
    d = _run_dir(name)
    s = _load(d / "summary.json") or {}
    run_id = s.get("run_id") or name
    fails = set(_gold_checks()["reference_fails"])
    notes = _harness_notes(d, s, run_id)
    tasks = []
    for iid, row in (s.get("instances") or {}).items():
        repo = iid.rsplit("-", 1)[0].replace("__", "/")
        tasks.append({
            "id": iid, "repo": repo,
            "outcome": row.get("outcome"), "reason": row.get("reason"),
            "resolved": row.get("resolved"),
            "cost_usd": row.get("cost_usd"), "duration_s": row.get("duration_s"),
            "patch_bytes": row.get("patch_bytes"), "review_verdict": row.get("review_verdict"),
            "models": row.get("models") or {},
            "started": bool(row.get("task_id")) or row.get("outcome") not in (None, "not_run"),
            "reference_fails": iid in fails,
            "tests": _harness_tests(d, run_id, iid),
            "harness_note": row.get("harness_note") or notes.get(iid),
            "has_trajectory": (d / "trajectories" / f"{iid}.json").is_file(),
            # The run that holds this task's files: a shard, for a split run.
            "run": name,
        })
    return {"summary": summary(name, s), "tasks": tasks}


def _text(content) -> str:
    if isinstance(content, list):
        content = "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def _cut(text: str) -> str:
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + f"\n… ({len(text) - _TEXT_LIMIT} more characters)"


def _conversation(traj: dict) -> list[dict]:
    """The saved trajectory (langchain's messages_to_dict), as rows a page can
    show: who spoke, what they said, which tools they called with what."""
    out = []
    for thread in traj.get("threads") or []:
        rows = []
        for m in thread.get("messages") or []:
            data = m.get("data") or {}
            rows.append({
                "role": m.get("type"),
                "name": data.get("name"),
                "text": _cut(_text(data.get("content"))),
                "tool_calls": [{"name": c.get("name"), "args": _cut(json.dumps(c.get("args"), indent=1))}
                               for c in data.get("tool_calls") or []],
            })
        out.append({"generation": thread.get("generation", 0), "namespace": thread.get("namespace") or "coordinator",
                    "messages": rows})
    return out


@router.get("/api/swebench/runs/{name}/tasks/{instance_id}")
async def get_task(name: str, instance_id: str, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    d = _run_dir(name)
    if not _INSTANCE.match(instance_id):
        raise HTTPException(400, "not an instance id")
    patch = None
    try:
        with (d / "predictions.jsonl").open() as fh:
            for line in fh:
                try:
                    p = json.loads(line)
                except ValueError:
                    continue
                if p.get("instance_id") == instance_id:
                    patch = p.get("model_patch") or ""
    except OSError:
        pass
    traj = _load(d / "trajectories" / f"{instance_id}.json")
    # The reviewer's whole verdict -- summary, findings, the message it sent
    # the agent -- as the runner kept it (`review` in summary.json). A run from
    # before it was kept has only the verdict word.
    row = ((_load(d / "summary.json") or {}).get("instances") or {}).get(instance_id) or {}
    review = row.get("review") or ({"verdict": row["review_verdict"]} if row.get("review_verdict") else None)
    return {"id": instance_id, "patch": patch, "review": review,
            "conversation": _conversation(traj) if traj else []}
