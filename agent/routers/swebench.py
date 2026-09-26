"""SWE-bench Verified runs, on the analytics page: the score, each task, its
patch, what the reviewer said, the box during the run, and the agent's whole
conversation.

Read-only. A run is `scripts/run_swebench.py`, started from a shell: it pulls
gigabytes of images, runs for hours and spends real money, and what makes its
number a SWE-bench number is spelled out in evals/SWEBENCH.md. This only reads
what the runner leaves in logs/swebench/<run>/ -- summary.json (rewritten
after every task, so a run shows as it goes; per task the reviewer's record
and the harness's own note), predictions.jsonl, the official harness's
reports, host.jsonl (agent/evals/host_metrics.py) and trajectories/<id>.json.
A run split with --shard is shown as one run.

Admin-only, like the golden suite: the trajectories hold every tool call and
file the agent read.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent import audit, auth, paths
from agent.evals import host_metrics
from agent.auth import User, require_full_auth
from agent.routers import audit_store
from agent.routers.evals import _parse_ts

router = APIRouter(tags=["swebench"])

RUNS_DIR = paths.REPO_ROOT / "logs" / "swebench"
RUNNER = paths.REPO_ROOT / "scripts" / "run_swebench.py"
DATASET_SIZE = 500
# One runner process per this many tasks at once: a process has one event
# loop and one SQLite file, and five per process is what the 50-task samples
# ran (evals/SWEBENCH.md, --shard).
TASKS_PER_PROCESS = 5
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
                "tasks": [t for p in parts for t in p["tasks"]],
                "host": _host_combined([p["host"] for p in parts])}
    return _run_tasks(name)


def _host(d: Path, s: dict) -> dict | None:
    """The box during the run (agent/evals/host_metrics.py): the peaks from
    the summary, the series from host.jsonl. None for a run from before it
    was recorded."""
    rec = s.get("host")
    if not rec:
        return None
    series = host_metrics.read_samples(d)
    # The summary's peaks are rewritten when a task ends; until the first one
    # does, the series is all there is.
    peaks = rec.get("peaks") or _peaks_of(series)
    return {"interval_s": rec.get("interval_s"), "samples": rec.get("samples") or len(series), "peaks": peaks,
            "series": series}


def _peaks_of(series: list[dict]) -> dict:
    peaks: dict[str, float] = {}
    for row in series:
        for k in host_metrics.FIELDS:
            if isinstance(row.get(k), (int, float)):
                peaks[k] = max(peaks.get(k, 0), row[k])
    return peaks


def _host_combined(hosts: list[dict | None]) -> dict | None:
    """Shards sample the same box: one series, and the larger of each peak."""
    hosts = [h for h in hosts if h]
    if not hosts:
        return None
    peaks: dict[str, float] = {}
    for h in hosts:
        for k, v in (h.get("peaks") or {}).items():
            if isinstance(v, (int, float)):
                peaks[k] = max(peaks.get(k, 0), v)
    return {"interval_s": hosts[0].get("interval_s"), "samples": sum(h.get("samples") or 0 for h in hosts),
            "peaks": peaks, "series": host_metrics.merge_series([h.get("series") or [] for h in hosts])}


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
    return {"summary": summary(name, s), "tasks": tasks, "host": _host(d, s)}


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


# --- starting and stopping a run from the page (2026-09-26) -----------------------
#
# The same script an operator runs from a shell, started the way the golden
# suite's runner is (agent/routers/evals.py): in its own session, parented to
# nothing here, so a restart of this server does not take the run with it.
# Ten tasks at once is two processes of five, each its own shard and run id.


class StartSwebenchRequest(BaseModel):
    sample: int = Field(default=50, ge=1, le=DATASET_SIZE)   # DATASET_SIZE: the full run
    seed: int = Field(default=1, ge=0, le=10_000)
    parallel: int = Field(default=10, ge=1, le=16)
    budget_usd: float = Field(default=3.0, ge=0.5, le=10.0)
    notes: str = Field(default="", max_length=300)


def plan_shards(sample: int, parallel: int) -> list[tuple[int, int, int]]:
    """(k, n, tasks at once) per process: `parallel` split into processes of
    at most TASKS_PER_PROCESS, never more processes than tasks."""
    processes = max(1, min(math.ceil(parallel / TASKS_PER_PROCESS), math.ceil(sample / TASKS_PER_PROCESS)))
    per = math.ceil(parallel / processes)
    return [(k, processes, per) for k in range(1, processes + 1)]


def _running_runs() -> list[str]:
    """Scored runs whose process is alive. A diagnostic run does not block
    a scored one."""
    out = []
    for name, s in _all_summaries().items():
        if _kind(s) != "diagnostic" and (s.get("state") or "done") == "running" and _alive(s.get("pid")):
            out.append(name)
    return sorted(out)


def _spawn(cmd: list[str], log_path: Path) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "w")   # noqa: SIM115 -- handed to the child, closed here after
    try:
        setsid = shutil.which("setsid")
        subprocess.Popen(([setsid, "--fork"] if setsid else []) + cmd,
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         cwd=str(paths.REPO_ROOT), env=os.environ.copy(), close_fds=True,
                         start_new_session=setsid is None)
    finally:
        log.close()


def run_commands(req: StartSwebenchRequest, base: str, notes: str) -> list[tuple[str, list[str]]]:
    """(run name, argv) per shard. One shard keeps the base name; several are
    `<base>-s<k>` with `--shard k/n`, which the page shows as one run."""
    full = req.sample >= DATASET_SIZE
    shards = plan_shards(req.sample, req.parallel)
    out = []
    for k, n, per in shards:
        name = base if n == 1 else f"{base}-s{k}"
        cmd = [sys.executable, str(RUNNER)]
        cmd += ["--all"] if full else ["--sample", str(req.sample), "--seed", str(req.seed)]
        cmd += ["--parallel", str(per), "--budget", str(req.budget_usd), "--run-id", name,
                "--notes", notes + (f" (shard {k}/{n})" if n > 1 else "")]
        if n > 1:
            cmd += ["--shard", f"{k}/{n}"]
        out.append((name, cmd))
    return out


@router.post("/api/swebench/run", status_code=202)
async def start_swebench_run(req: StartSwebenchRequest, request: Request, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    running = _running_runs()
    if running:
        raise HTTPException(409, f"a run is already in progress: {', '.join(running)}")
    if not RUNNER.is_file():
        raise HTTPException(500, "the runner script is missing")
    full = req.sample >= DATASET_SIZE
    stamp = time.strftime("%Y%m%dT%H%M", time.gmtime())
    base = f"tektonix-{'all' if full else f'sample{req.sample}'}-seed{req.seed}-{stamp}"
    if not _RUN_NAME.match(base) or (RUNS_DIR / base).exists():
        raise HTTPException(409, "a run with this name already exists; try again in a minute")
    notes = req.notes.strip() or f"started from the dashboard by {user.email}"
    names = []
    for name, cmd in run_commands(req, base, notes):
        _spawn(cmd, RUNS_DIR / f"{name}.runner.log")
        names.append(name)
    await audit.record(audit_store(request), actor=user.email, action="swebench.run", target=base,
                       detail=f"{'all 500' if full else f'{req.sample} tasks, seed {req.seed}'}, "
                              f"{req.parallel} at once in {len(names)} process(es), ${req.budget_usd} per task")
    return {"ok": True, "name": base, "shards": names}


def _run_names(name: str) -> list[str]:
    """A combined run's shards, or the run itself."""
    all_s = _all_summaries()
    shards = _groups(all_s).get(name)
    if shards:
        return shards
    if name in all_s:
        return [name]
    raise HTTPException(404, "no such run")


@router.post("/api/swebench/runs/{name}/stop")
async def stop_swebench_run(name: str, request: Request, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if not _RUN_NAME.match(name):
        raise HTTPException(400, "not a run name")
    all_s = _all_summaries()
    stopped = []
    for run in _run_names(name):
        s = all_s.get(run) or {}
        pid = s.get("pid")
        if (s.get("state") or "done") != "running" or not _alive(pid):
            continue
        # SIGTERM to the run's whole session: the runner turns it into an
        # orderly stop (summary marked stopped, reviewer pair down, temporary
        # repositories removed), and the harness's own grading processes,
        # which it may be in the middle of, go with it instead of outliving
        # it (2026-09-25: four grading containers were left running).
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
        stopped.append(run)
    if not stopped:
        raise HTTPException(409, "this run is not running")
    for run in stopped:
        # Best effort: the harness names its containers `sweb.eval.<task>.<run>`.
        subprocess.Popen(f"docker ps -aq --filter name=.{run} | xargs -r docker rm -f", shell=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    await audit.record(audit_store(request), actor=user.email, action="swebench.stop", target=name,
                       detail=", ".join(stopped))
    return {"ok": True, "stopped": stopped}


@router.get("/api/swebench/runs/{name}/log")
async def swebench_run_log(name: str, lines: int = 80, user: User = Depends(require_full_auth)):
    """The runner's own output, last `lines` per shard."""
    auth.require_admin(user)
    if not _RUN_NAME.match(name):
        raise HTTPException(400, "not a run name")
    lines = max(1, min(int(lines), 500))
    out: list[str] = []
    for run in _run_names(name):
        path = RUNS_DIR / f"{run}.runner.log"
        try:
            tail = path.read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            tail = []
        if len(_run_names(name)) > 1:
            out.append(f"== {run}")
        out.extend(tail)
    return {"lines": out}
