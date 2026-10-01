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
import os
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
# A failed run does not advance ran_at (a transient failure waited the full
# day); it is retried after this instead of on the next tick, so a job that
# is broken for good does not spend on a model every ten minutes.
FAILURE_RETRY = timedelta(hours=1)
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

# The in-process lock keeps one run per job inside this process; the file
# lock beside it (lock_path) is what the host cron wrappers take too
# (scripts/consolidation-cron.sh, cartographer-cron.sh), so the schedule
# here and a cron still installed from before the jobs moved in-process
# cannot run the same job on the same store at once. The cron stamps its
# marker only when it finishes, so while it ran the job looked due here
# and a second run started (2026-09-29).
_locks: dict[str, asyncio.Lock] = {}
_claimed: set[str] = set()               # start_job took the slot; run_job has not reached the lock yet
_running: dict[str, float] = {}          # name -> started (monotonic)
_waiting: dict[str, str] = {}            # name -> why the last due check did not run it


def _lock(name: str) -> asyncio.Lock:
    lock = _locks.get(name)
    if lock is None:
        lock = _locks[name] = asyncio.Lock()
    return lock


def lock_path(job: Job) -> Path:
    return paths.DATA_DIR / f"{job.name}.lock"


def _try_flock(job: Job) -> int | None:
    """The cross-process claim: an fd holding the file lock, or None when
    another process (a cron wrapper, another agent) holds it. flock, so a
    dead holder releases it (see agent/file_lock.py on why not a pid file)."""
    import fcntl  # noqa: PLC0415 -- posix only
    path = lock_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def held_elsewhere(job: Job) -> bool:
    fd = _try_flock(job)
    if fd is None:
        return True
    os.close(fd)        # closing releases the flock
    return False


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
    """When the job is next due: a day after its last successful run, an
    hour after its last failure, whichever is later; None for "now" when
    it has never run."""
    rec = read_marker(job)
    ran = _parse(rec.get("ran_at"))
    failed = _parse(rec.get("failed_at"))
    candidates = [t for t in (ran + INTERVAL if ran else None, failed + FAILURE_RETRY if failed else None) if t]
    return max(candidates) if candidates else None


def is_due(job: Job, now: datetime | None = None) -> bool:
    when = due_at(job)
    return when is None or when <= (now or datetime.now(UTC))


def quiet() -> bool:
    """No task running and no planning turn open."""
    return not live_state.running_tasks and not live_state.running_planning_turns


def _write_marker(job: Job, record: dict) -> None:
    """Temp file and rename: the dashboard and the cron wrappers read this
    file, and a crash mid-write left a half-written one (2026-09-29)."""
    path = marker_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record) + "\n")
    os.replace(tmp, path)


def _append_log(job: Job, started: str, lines: list[str]) -> None:
    try:
        path = paths.DATA_DIR / job.log
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(f"=== {started} ===\n" + "".join(ln + "\n" for ln in lines))
    except OSError as e:
        logger.warning("jobs: could not append to %s: %s", job.log, e)


async def run_job(name: str, state, *, trigger: str, claimed: bool = False) -> dict:
    """Run one job now, under its lock, and write its marker. Returns the
    marker record. Raises JobBusy when it is already running here or in
    another process. `claimed` is start_job's: the slot was taken before
    this coroutine was scheduled."""
    job = JOBS[name]
    lock = _lock(name)
    if lock.locked() or (name in _claimed and not claimed):
        raise JobBusy(f"{job.title} is already running")
    async with lock:
        _claimed.discard(name)
        fd = _try_flock(job)      # LOCK_NB: an instant syscall, no thread hop
        if fd is None:
            _waiting[name] = f"running in another process (data/{lock_path(job).name} is held)"
            raise JobBusy(f"{job.title} is already running in another process")
        started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _running[name] = time.monotonic()
        _waiting.pop(name, None)
        # A failure keeps the last good ran_at, so the retry is FAILURE_RETRY
        # away rather than a day (2026-09-29).
        record: dict = {"ran_at": read_marker(job).get("ran_at"), "trigger": trigger}
        lines: list[str] = []
        try:
            summary = await job.run(state)
            record.update(ran_at=started, ok=True, exit_code=0, summary=summary)
            lines += [f"{repo}: {s}" for repo, s in summary.items()]
        except JobFailed as e:
            record.update(failed_at=started, ok=False, exit_code=1, summary=e.summary, error=str(e)[:600])
            lines += [f"{repo}: {s}" for repo, s in e.summary.items()] + [f"FAILED: {e}"]
            logger.error("jobs: %s failed: %s", name, e)
        except Exception as e:  # noqa: BLE001 -- the marker must say it failed, whatever it was
            record.update(failed_at=started, ok=False, exit_code=1, error=f"{type(e).__name__}: {str(e)[:500]}")
            lines.append(f"FAILED: {type(e).__name__}: {e}")
            logger.exception("jobs: %s crashed", name)
        finally:
            record["duration_s"] = round(time.monotonic() - _running.pop(name), 1)
            os.close(fd)        # releases the flock
        _write_marker(job, record)
        _append_log(job, started, lines)
        return record


def start_job(name: str, state, *, trigger: str) -> None:
    """Claim the job's slot NOW and run it in the background. The dashboard
    route checked `.locked()` and then spawned, so two clicks in one loop
    turn both got 202 and the second run raised JobBusy where nobody saw
    it (2026-09-29). Raises JobBusy synchronously instead."""
    job = JOBS[name]
    if _lock(name).locked() or name in _claimed:
        raise JobBusy(f"{job.title} is already running")
    if held_elsewhere(job):
        raise JobBusy(f"{job.title} is already running in another process")
    _claimed.add(name)
    live_state.spawn_background(run_job(name, state, trigger=trigger, claimed=True), f"job:{name}")


class JobBusy(Exception):
    pass


def status(name: str) -> dict:
    job = JOBS[name]
    rec = read_marker(job)
    when = due_at(job)
    return {
        "name": name, "title": job.title,
        "ran_at": rec.get("ran_at"), "failed_at": rec.get("failed_at"),
        "ok": rec.get("ok"), "exit_code": rec.get("exit_code"),
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
        if not is_due(job) or _lock(name).locked() or name in _claimed:
            # Whatever held it up last time is over: a stale "running in
            # another process" outlived the cron run it described by hours
            # (2026-10-01).
            if not _lock(name).locked():
                _waiting.pop(name, None)
            continue
        if not quiet():
            _waiting[name] = "due, waiting for the agent to be idle"
            continue
        if held_elsewhere(job):
            _waiting[name] = f"due, running in another process (data/{lock_path(job).name} is held)"
            continue
        try:
            await run_job(name, state, trigger="scheduled")
        except JobBusy:
            continue        # taken between the check and the lock; the reason is in _waiting
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
