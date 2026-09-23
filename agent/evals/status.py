"""A running eval suite's progress, as a file the dashboard can read.

A run started from the dashboard is a separate, detached process -- it takes
an hour, and a tektonix restart (a deploy) must not kill it -- so the API
cannot ask it anything. The run writes this file instead: who it is, what it
was asked to do, how far it has got, and, when it ends, how. The API reads it.

Atomic replace on every write, so a reader never sees half a file. `running`
is derived on read rather than stored: a run that was killed outright never
gets to write "finished", and a stored flag would say it is running forever.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# agent.paths and nothing else: scripts/run_evals.py imports this before it
# has pointed AGENT_PROJECTS_JSON at the eval's own project list, and anything
# that pulled in agent.config here would freeze the operator's real projects
# into the run (see that script's docstring).
from agent import paths

STATUS_PATH = paths.REPO_ROOT / "logs" / "evals" / "status.json"


def write(path: Path | None = None, **fields) -> dict:
    """Merge `fields` into the status file and return the whole record."""
    path = path or STATUS_PATH
    current = _load(path) or {}
    current.update(fields)
    current["updated_at"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2) + "\n")
    os.replace(tmp, path)
    return current


def start(path: Path | None, *, notes: str, only: list[str] | None, tasks_total: int) -> dict:
    """A fresh record for a run that is starting; replaces the last one."""
    path = path or STATUS_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return write(path, pid=os.getpid(), started_at=time.time(), notes=notes, only=only or [],
                 tasks_total=tasks_total, done=0, passed=0, spent_usd=0.0, results=[],
                 finished_at=None, exit_code=None, report=None)


# How long a claim with no process behind it yet still counts as a run: the
# dashboard writes it the moment it starts the child, and the child replaces
# it with its own record within a second or two. Longer than that and the
# child evidently never started -- its log says why.
CLAIM_GRACE_S = 120


def claim(path: Path | None = None, *, notes: str, only: list[str] | None, tasks_total: int) -> dict:
    """Mark a run as starting before its process exists (see CLAIM_GRACE_S)."""
    path = path or STATUS_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return write(path, pid=None, claimed=True, started_at=time.time(), notes=notes, only=only or [],
                 tasks_total=tasks_total, done=0, passed=0, spent_usd=0.0, results=[],
                 finished_at=None, exit_code=None, report=None)


def task_done(path: Path | None, *, task_id: str, passed: bool, cost_usd: float, outcome: str) -> dict:
    path = path or STATUS_PATH
    cur = _load(path) or {}
    results = list(cur.get("results") or [])
    results.append({"id": task_id, "passed": passed, "cost_usd": round(cost_usd, 4), "outcome": outcome})
    return write(path, results=results, done=len(results), passed=sum(1 for r in results if r["passed"]),
                 spent_usd=round(sum(r["cost_usd"] for r in results), 4))


def finish(path: Path | None, *, exit_code: int, report: str | None) -> dict:
    return write(path or STATUS_PATH, finished_at=time.time(), exit_code=exit_code, report=report)


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def read(path: Path | None = None) -> dict | None:
    """The last run's record, with `running` derived: the process is alive
    and has not written its finish. A run killed outright reads as ended."""
    rec = _load(path or STATUS_PATH)
    if rec is None:
        return None
    unfinished = rec.get("finished_at") is None
    if rec.get("pid") is None:
        # Claimed by the dashboard; the child has not written its own record yet.
        rec["running"] = bool(unfinished and time.time() - float(rec.get("started_at") or 0) < CLAIM_GRACE_S)
    else:
        rec["running"] = bool(unfinished and _pid_alive(rec.get("pid")))
    return rec
