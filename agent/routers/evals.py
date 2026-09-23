"""The golden-task eval suite, from the dashboard: its history, a run's full
report, and starting or stopping a run.

Admin-only, all of it: a run spends real money (cents, but real) and an hour
of the model router, and the reports hold the agent's diffs.

A run is `scripts/run_evals.py` -- the same one an operator runs from a shell
(evals/README.md) -- started DETACHED. It takes about an hour, and a deploy
restarts tektonix; pm2 kills a restarting process's whole tree, so a run that
was merely a child of this server would die with every deploy. `setsid
--fork` puts it in its own session with no parent here. It writes its progress
to logs/evals/status.json (agent/evals/status.py), which is how this router
knows what it is doing without being able to ask it.
"""
from __future__ import annotations

import calendar
import json
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
from agent.auth import User, require_full_auth
from agent.evals import status as ev_status
from agent.routers import audit_store

router = APIRouter(tags=["evals"])

REPORT_DIR = paths.REPO_ROOT / "logs" / "evals"
RUNNER = paths.REPO_ROOT / "scripts" / "run_evals.py"
RUN_LOG = REPORT_DIR / "last-run.log"
_REPORT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _parse_ts(value) -> float | None:
    """A report's UTC timestamp as epoch seconds (timegm: no local zone, no DST)."""
    if not value:
        return None
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def summary(name: str, report: dict) -> dict:
    """What the history list and the scorecard need; no diffs, no assertions."""
    started, finished = _parse_ts(report.get("started_at")), _parse_ts(report.get("finished_at"))
    tasks = report.get("tasks") or []
    return {
        "name": name,
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "duration_s": round(finished - started) if started and finished else None,
        "notes": report.get("notes") or "",
        "tasks_total": report.get("tasks_total"),
        "tasks_attempted": report.get("tasks_attempted"),
        "tasks_passed": report.get("tasks_passed"),
        "pass_rate": report.get("pass_rate"),
        "total_cost_usd": report.get("total_cost_usd"),
        "stopped_early": bool(report.get("stopped_early")),
        "by_category": report.get("by_category") or {},
        "benchmarks": report.get("benchmarks") or {},
        # A full run is one that asked for every task. Reports from before
        # `only` was recorded were full when they ran the original twelve.
        "full": (not report["only"]) if "only" in report else (report.get("tasks_total") or 0) >= 12,
        "parallel": int(report.get("parallel") or 1),
        "failed": [t["id"] for t in tasks if not t.get("passed")],
        "results": {t["id"]: bool(t.get("passed")) for t in tasks},
    }


def _reports() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(REPORT_DIR.glob("*.json"), reverse=True):
        if not _REPORT_NAME.match(path.stem):
            continue          # status.json and anything else that is not a run
        rep = _load(path)
        if rep is not None:
            out.append((path.stem, rep))
    return out


def _suite() -> dict:
    """The suite as it stands on disk: how many tasks, in which categories."""
    from agent.evals.spec import load_suite   # noqa: PLC0415 -- light, but only here
    try:
        tasks = load_suite()
    except Exception as e:  # noqa: BLE001 -- a bad spec is an answer, not a 500
        return {"tasks": 0, "by_category": {}, "ids": [], "error": str(e)[:300]}
    cats: dict[str, int] = {}
    for t in tasks:
        cats[t.category] = cats.get(t.category, 0) + 1
    return {"tasks": len(tasks), "by_category": cats, "ids": sorted(t.id for t in tasks)}


def _estimate(runs: list[dict], n_tasks: int) -> dict:
    """Cost and time for a full run, scaled from the most recent run that got
    through at least half its tasks. None when there is nothing to go on."""
    for r in runs:
        attempted = r.get("tasks_attempted") or 0
        if attempted and attempted >= (r.get("tasks_total") or 0) / 2 and r.get("duration_s"):
            per_cost = (r.get("total_cost_usd") or 0) / attempted
            per_time = r["duration_s"] / attempted
            return {"cost_usd": round(per_cost * n_tasks, 2), "duration_s": round(per_time * n_tasks),
                    "from_run": r["name"]}
    return {"cost_usd": None, "duration_s": None, "from_run": None}


@router.get("/api/evals")
async def list_evals(user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    runs = [summary(name, rep) for name, rep in _reports()[:50]]
    suite = _suite()
    return {"runs": runs, "status": ev_status.read(), "suite": suite,
            "estimate": _estimate(runs, suite["tasks"])}


@router.get("/api/evals/runs/{name}")
async def get_eval_run(name: str, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    # The name is matched, not joined: it becomes a path, and nothing but a
    # run's own timestamp may name one.
    if not _REPORT_NAME.match(name):
        raise HTTPException(400, "not a run name")
    rep = _load(REPORT_DIR / f"{name}.json")
    if rep is None:
        raise HTTPException(404, "no such run")
    return {**rep, "summary": summary(name, rep)}


class StartEvalRequest(BaseModel):
    notes: str = Field(default="", max_length=300)
    only: list[str] | None = None
    # Tasks at once, each in its own workspace (scripts/run_evals.py --parallel).
    parallel: int = Field(default=1, ge=1, le=4)


def _spawn(cmd: list[str]) -> None:
    """Start the run in its own session, parented to nothing here."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    log = open(RUN_LOG, "w")   # noqa: SIM115 -- handed to the child, closed here after
    try:
        setsid = shutil.which("setsid")
        subprocess.Popen(([setsid, "--fork"] if setsid else []) + cmd,
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         cwd=str(paths.REPO_ROOT), env=os.environ.copy(), close_fds=True,
                         start_new_session=setsid is None)
    finally:
        log.close()


@router.post("/api/evals/run", status_code=202)
async def start_eval_run(req: StartEvalRequest, request: Request, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    current = ev_status.read()
    if current and current.get("running"):
        raise HTTPException(409, "an eval run is already in progress")
    suite = _suite()
    only = [t for t in (req.only or []) if t]
    unknown = sorted(set(only) - set(suite["ids"]))
    if unknown:
        raise HTTPException(400, f"no such task(s): {', '.join(unknown)}")
    notes = req.notes.strip() or f"started from the dashboard by {user.email}"
    cmd = [sys.executable, str(RUNNER), "--status-file", "--notes", notes]
    if req.parallel > 1:
        cmd += ["--parallel", str(req.parallel)]
    if only:
        cmd += ["--only", *only]
    # Claimed BEFORE the child exists, so a second click in the second or two
    # it takes to write its own record cannot start a second run.
    ev_status.claim(notes=notes, only=only, tasks_total=len(only) or suite["tasks"])
    _spawn(cmd)
    await audit.record(audit_store(request), actor=user.email, action="evals.run",
                       target="golden suite", detail=(", ".join(only) or "all tasks")[:200])
    return {"ok": True, "tasks": len(only) or suite["tasks"]}


@router.post("/api/evals/stop")
async def stop_eval_run(request: Request, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    current = ev_status.read()
    if not current or not current.get("running") or not current.get("pid"):
        raise HTTPException(409, "no eval run is in progress")
    # SIGINT, to the run's whole session: the runner turns it into an orderly
    # stop -- its own reviewer pair shut down, the working tree removed, the
    # status file finished -- where SIGKILL would leave both behind.
    try:
        os.killpg(int(current["pid"]), signal.SIGINT)
    except ProcessLookupError:
        pass
    ev_status.write(stopped_by=user.email)
    await audit.record(audit_store(request), actor=user.email, action="evals.stop",
                       target="golden suite", detail=f"after {current.get('done', 0)} task(s)")
    return {"ok": True}
