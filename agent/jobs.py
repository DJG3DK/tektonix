"""The daily jobs, scheduled by the agent itself.

Memory consolidation and the cartographer used to be host crons
(scripts/*-cron.sh). A cron is a thing the compose bundle does not have and
a desktop app that is closed at night never reaches, so in both they simply
never ran (2026-09-28). Now the agent runs them: once a day, at the first
quiet moment after they are due -- no task running, no planning turn open
-- checked on startup after the stack settles and every ten minutes after
that. A run can also be asked for from the dashboard.

Each job writes the same marker file its cron wrapper wrote
(data/last_consolidation.json, data/last_cartography.json), so the status
panel, the cron scripts and this scheduler all read one record, and a
cron that still runs on a host install counts: the job is simply not due
again for a day. Both jobs are cheap when there is nothing to do: they
find no new episodes or an unchanged tree and return.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from collections.abc import Awaitable, Callable

from agent import live_state, paths

logger = logging.getLogger("tektonix.jobs")

INTERVAL = timedelta(hours=24)
SETTLE_S = 120          # after startup, before the first check
CHECK_EVERY_S = 600     # between checks
BUSY_RETRY_S = 600      # a due job waits this long when a task is in flight


@dataclass(frozen=True)
class Job:
    name: str
    title: str
    marker: str           # file name under data/
    log: str              # file name under data/
    run: Callable[[Any], Awaitable[dict]]   # (app_state) -> per-project summaries


async def _consolidate(state) -> dict:
    from agent.config import PROJECTS
    from agent.consolidation import run_consolidation

    out: dict[str, Any] = {}
    failures = []
    for repo in list(PROJECTS):
        try:
            summary = await run_consolidation(state.config, repo, state.checkpointer, state.store)
            out[repo] = summary
            if summary.get("history_failed"):
                failures.append(f"{repo}: history index")
        except Exception as e:  # noqa: BLE001 -- one project's failure must not stop the rest
            out[repo] = {"error": str(e)[:400]}
            failures.append(f"{repo}: {str(e)[:200]}")
    if failures:
        raise JobFailed("; ".join(failures), out)
    return out


async def _cartograph(state) -> dict:
    from agent.cartographer import run_cartographer
    from agent.config import PROJECTS

    out: dict[str, Any] = {}
    failures = []
    for repo in list(PROJECTS):
        try:
            out[repo] = await run_cartographer(state.config, repo, state.store)
        except Exception as e:  # noqa: BLE001
            out[repo] = {"error": str(e)[:400]}
            failures.append(f"{repo}: {str(e)[:200]}")
    if failures:
        raise JobFailed("; ".join(failures), out)
    return out


class JobFailed(Exception):
    def __init__(self, message: str, summary: dict):
        super().__init__(message)
        self.summary = summary


JOBS: dict[str, Job] = {
    "consolidation": Job("consolidation", "Memory consolidation", "last_consolidation.json",
                         "consolidation.log", _consolidate),
    "cartography": Job("cartography", "Codebase map", "last_cartography.json",
                       "cartographer.log", _cartograph),
}

_locks: dict[str, asyncio.Lock] = {}
_running: dict[str, float] = {}          # name -> started (monotonic)
_waiting: dict[str, str] = {}            # name -> why the last due check did not run it


def _lock(name: str) -> asyncio.Lock:
    lock = _locks.get(name)
    if lock is None:
        lock = _locks[name] = asyncio.Lock()
    return lock


def marker_path(job: Job) -> Path:
    return paths.DATA_DIR / job.marker


def read_marker(job: Job) -> dict:
    try:
        return json.loads(marker_path(job).read_text())
    except (OSError, ValueError):
        return {}


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def due_at(job: Job) -> datetime | None:
    """When the job is next due: a day after its last run, or None for
    "now" when it has never run."""
    ran = _parse(read_marker(job).get("ran_at"))
    return ran + INTERVAL if ran else None


def is_due(job: Job, now: datetime | None = None) -> bool:
    when = due_at(job)
    return when is None or when <= (now or datetime.now(UTC))


def quiet() -> bool:
    """No task running and no planning turn open."""
    return not live_state.running_tasks and not live_state.running_planning_turns


def _write_marker(job: Job, record: dict) -> None:
    path = marker_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) + "\n")


def _append_log(job: Job, started: str, lines: list[str]) -> None:
    try:
        path = paths.DATA_DIR / job.log
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(f"=== {started} ===\n" + "".join(ln + "\n" for ln in lines))
    except OSError as e:
        logger.warning("jobs: could not append to %s: %s", job.log, e)


async def run_job(name: str, state, *, trigger: str) -> dict:
    """Run one job now, under its lock, and write its marker. Returns the
    marker record. Raises JobBusy when it is already running."""
    job = JOBS[name]
    lock = _lock(name)
    if lock.locked():
        raise JobBusy(f"{job.title} is already running")
    async with lock:
        started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _running[name] = time.monotonic()
        _waiting.pop(name, None)
        record: dict = {"ran_at": started, "trigger": trigger}
        lines: list[str] = []
        try:
            summary = await job.run(state)
            record.update(ok=True, exit_code=0, summary=summary)
            lines += [f"{repo}: {s}" for repo, s in summary.items()]
        except JobFailed as e:
            record.update(ok=False, exit_code=1, summary=e.summary, error=str(e)[:600])
            lines += [f"{repo}: {s}" for repo, s in e.summary.items()] + [f"FAILED: {e}"]
            logger.error("jobs: %s failed: %s", name, e)
        except Exception as e:  # noqa: BLE001 -- the marker must say it failed, whatever it was
            record.update(ok=False, exit_code=1, error=f"{type(e).__name__}: {str(e)[:500]}")
            lines.append(f"FAILED: {type(e).__name__}: {e}")
            logger.exception("jobs: %s crashed", name)
        finally:
            record["duration_s"] = round(time.monotonic() - _running.pop(name), 1)
        _write_marker(job, record)
        _append_log(job, started, lines)
        return record


class JobBusy(Exception):
    pass


def status(name: str) -> dict:
    job = JOBS[name]
    rec = read_marker(job)
    when = due_at(job)
    return {
        "name": name, "title": job.title,
        "ran_at": rec.get("ran_at"), "ok": rec.get("ok"), "exit_code": rec.get("exit_code"),
        "trigger": rec.get("trigger"), "error": rec.get("error"), "duration_s": rec.get("duration_s"),
        "due_at": when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None,
        "due": is_due(job),
        "running": name in _running,
        "waiting": _waiting.get(name),
    }


async def run_due(state) -> list[str]:
    """One scheduler tick: every due job, if the agent is quiet. Returns the
    names run."""
    ran = []
    for name, job in JOBS.items():
        if not is_due(job) or _lock(name).locked():
            continue
        if not quiet():
            _waiting[name] = "due, waiting for the agent to be idle"
            continue
        await run_job(name, state, trigger="scheduled")
        ran.append(name)
    return ran


async def run_forever(state, *, settle_s: float = SETTLE_S, check_every_s: float = CHECK_EVERY_S) -> None:
    await asyncio.sleep(settle_s)
    while True:
        try:
            ran = await run_due(state)
            if ran:
                logger.info("jobs: ran %s", ", ".join(ran))
        except Exception:  # noqa: BLE001 -- the loop outlives any one tick's failure
            logger.exception("jobs: tick failed")
        await asyncio.sleep(check_every_s)
