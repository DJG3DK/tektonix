"""The supervisor: heals infrastructure failures without waiting for anyone.

An escalation used to mean "a person must look". Most of them should not: of
the 19 escalations in the thirty days to 2026-09-23, 16 were plumbing -- the
reviewer not answering, main moving under a merge, a dropped connection -- and
the fix for every one was the same click on Resume once the cause had gone.
The operator's own words: it is supposed to heal itself.

Every minute this looks at escalated tasks and, for each one:

  * whose commits are already on main (landed by hand, rebased by someone
    else, merged on GitHub) -> marks it done. Waiting on it is waiting for
    nothing. `git cherry` compares patches, so a rebased landing counts.
  * escalated by an INFRASTRUCTURE failure whose cause has cleared -> puts it
    back through the gate via lifecycle.heal, with backoff and a cap.
  * anything else -> leaves it for the operator. A budget, a loop, a review
    that will not converge: those are the task's own failures, and a person
    deciding what to do next is the point of escalating them.

What this does NOT do is guess. A reason no pattern below recognises is not
healed, nor is one older than a day (MAX_AGE_S), and the cap (runtime
setting auto_heal_attempts, 0 = off) bounds a failure that keeps coming back. Every heal is written into the task's own log
and sent as an alert, so nothing happens behind the operator's back.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC
from collections.abc import Awaitable, Callable

from agent import lifecycle

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 60
# Backoff before heal N (0-based): wait this long after first seeing the
# escalation. Long enough for a restarting service to come back, short enough
# that a merge does not sit for an hour.
BACKOFF_S = (60, 180, 600, 1800)
# Only escalations younger than this are healed. An outage is recent by
# nature; an escalation that has sat for days is one the operator has seen and
# left, and reviving it would restart old -- and paid -- work nobody asked for.
# (The first dry run against live data would have revived a month-old task.)
MAX_AGE_S = 24 * 3600


def escalated_at(values: dict) -> float | None:
    """When this task escalated: its last log entry, which is the escalation
    itself. None when that cannot be told -- and then it is not healed."""
    from datetime import datetime

    log = values.get("execution_log") or []
    stamp = (log[-1] or {}).get("timestamp") if log else None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Kind:
    name: str
    pattern: re.Pattern
    # What must be true before retrying: None, "reviewer" (both review
    # services answer) or "live_clean" (the live checkout has no local edits).
    needs: str | None
    # Where the failure happened. "gate": the work was finished and the
    # failure came after it. "work": a work pass was cut off mid-flight.
    stage: str


# Order matters: first match wins, and the work-stage connection failures
# must be recognised before the generic network pattern claims them.
KINDS: tuple[Kind, ...] = (
    Kind("review_timeout", re.compile(r"review service did not review \S+ within \d+s"), "reviewer", "gate"),
    Kind("stale_review", re.compile(r"'reason': 'stale'|Newer commit\(s\) since the last review"), "reviewer", "gate"),
    Kind("base_moved", re.compile(
        r"Diverging branches|'reason': 'diverged'|live moved on and the branch could not be rebased"), None, "gate"),
    Kind("live_dirty", re.compile(r"Your local changes to the following files would be overwritten"), "live_clean", "gate"),
    Kind("push_failed", re.compile(r"could not push"), None, "gate"),
    Kind("work_connection", re.compile(
        r"^work node failed: .*(peer closed connection|incomplete chunked read|RemoteProtocolError|"
        r"ReadTimeout|ConnectTimeout|Server disconnected|502 Bad Gateway|503 Service Unavailable|"
        r"504 Gateway)", re.I | re.S), None, "work"),
    Kind("gate_connection", re.compile(
        r"^(verify_and_ship failed|merge/deploy failed).*(All connection attempts failed|Connection refused|"
        r"ConnectError|peer closed connection|ReadTimeout|ConnectTimeout|Server disconnected|"
        r"502 Bad Gateway|503 Service Unavailable|504 Gateway)", re.I | re.S), "reviewer", "gate"),
)


def classify(reason: str | None) -> Kind | None:
    """The infrastructure kind of an escalation reason, or None for anything
    that is the task's own failure -- or that nothing here recognises."""
    if not reason:
        return None
    for kind in KINDS:
        if kind.pattern.search(reason):
            return kind
    return None


@dataclass
class Deps:
    """Everything the sweep touches, passed in so it can be tested without a
    server, a database, a reviewer or a git repo."""
    projects: dict
    list_tasks: Callable[[str], Awaitable[list[dict]]]          # repo -> task metas
    read_state: Callable[[str], Awaitable[tuple[dict | None, str]]]  # task -> (values, checkpoint id)
    is_running: Callable[[str], bool]
    # (task, values, transition, start a run?) -> applied. False when the
    # task was claimed by something else in the meantime.
    apply: Callable[[str, dict, lifecycle.Transition, bool], Awaitable[bool]]
    write_meta: Callable[..., Awaitable[None]]                  # (repo, task, **updates)
    notify: Callable[[str, str, str, dict], None]               # (kind, repo, detail, values)
    reviewer_up: Callable[[], Awaitable[bool]]
    live_clean: Callable[[str], Awaitable[bool]]
    landed: Callable[[str, str], Awaitable[bool]]               # (live root, committed sha)
    max_attempts: Callable[[], int]


def _log_entry(summary: str, detail: str = "") -> dict:
    return {"node": "supervisor", "step_id": None, "summary": summary, "detail": detail,
            "cost_usd": 0.0, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ")}


async def sweep(deps: Deps, now: float | None = None) -> list[dict]:
    """One pass over every project's parked tasks. Returns what it did, one
    record per task it acted on or deliberately left, for the log and tests."""
    now = time.time() if now is None else now
    done: list[dict] = []
    reviewer_state: bool | None = None   # asked at most once per sweep

    for repo, cfg in deps.projects.items():
        try:
            metas = await deps.list_tasks(repo)
        except Exception:  # noqa: BLE001 -- one project must not stop the sweep
            logger.exception("supervisor: listing tasks failed for %s", repo)
            continue
        for meta in metas:
            task_id = meta.get("task_id")
            status = meta.get("status")
            if not task_id or status not in ("escalated", "awaiting_merge") or deps.is_running(task_id):
                continue
            try:
                values, ckpt = await deps.read_state(task_id)
                if not values:
                    continue
                phase = lifecycle.phase_of(values, status)

                # Already on main: close it, whatever it was waiting for.
                live = (cfg or {}).get("live")
                if live and values.get("committed_sha") and await deps.landed(live, values["committed_sha"]):
                    t = lifecycle.conclude_landed(values, status)
                    t = lifecycle.Transition({**t.patch, "execution_log": [_log_entry(
                        "supervisor: this task's commits are already on main -- concluded")]},
                        t.as_node, summary=t.summary)
                    if await deps.apply(task_id, values, t, False):
                        await deps.write_meta(repo, task_id, status="done", escalation_reason=None)
                        deps.notify("auto_concluded", repo, "Its commits were found on main.", values)
                        done.append({"task": task_id, "action": "concluded"})
                    continue

                if phase != "escalated":
                    continue
                kind = classify(values.get("escalation_reason"))
                if kind is None:
                    continue
                when = escalated_at(values)
                if when is None or now - when > MAX_AGE_S:
                    continue

                heal = dict(meta.get("heal") or {})
                attempts = int(heal.get("attempts", 0))
                cap = deps.max_attempts()
                if attempts >= cap:
                    if heal.get("gave_up_at") != ckpt:
                        await deps.write_meta(repo, task_id, heal={**heal, "gave_up_at": ckpt})
                        done.append({"task": task_id, "action": "gave_up", "kind": kind.name})
                    continue
                # One escalation = one checkpoint. A new one since the last
                # heal starts its own backoff clock.
                if heal.get("ckpt") != ckpt:
                    heal = {**heal, "ckpt": ckpt, "seen_at": now}
                    await deps.write_meta(repo, task_id, heal=heal)
                wait = BACKOFF_S[min(attempts, len(BACKOFF_S) - 1)]
                if now - float(heal.get("seen_at", now)) < wait:
                    continue

                if kind.needs == "reviewer":
                    if reviewer_state is None:
                        reviewer_state = await deps.reviewer_up()
                    if not reviewer_state:
                        done.append({"task": task_id, "action": "waiting", "kind": kind.name})
                        continue
                if kind.needs == "live_clean" and not (live and await deps.live_clean(live)):
                    done.append({"task": task_id, "action": "waiting", "kind": kind.name})
                    continue

                t = lifecycle.heal(values, status, reason=kind.name, attempt=attempts + 1, stage=kind.stage)
                t = lifecycle.Transition({**t.patch, "execution_log": [_log_entry(
                    f"supervisor: {t.summary} -- was: {str(values.get('escalation_reason'))[:300]}")]},
                    t.as_node, summary=t.summary)
                if await deps.apply(task_id, values, t, True):
                    await deps.write_meta(repo, task_id, heal={**heal, "attempts": attempts + 1, "last_at": now})
                    deps.notify("auto_healed", repo,
                                f"{kind.name} (attempt {attempts + 1} of {cap}). Was: "
                                f"{str(values.get('escalation_reason'))[:500]}", values)
                    done.append({"task": task_id, "action": "healed", "kind": kind.name,
                                 "attempt": attempts + 1})
            except Exception:  # noqa: BLE001 -- one task must not stop the sweep
                logger.exception("supervisor: failed on task %s", task_id)
    return done


async def run_forever(deps: Deps, interval: float = SWEEP_INTERVAL_S, startup_delay: float = 30.0) -> None:
    # After startup settles: the startup auto-resume reconnects orphans first,
    # and a heal racing it for the same task would be pointless.
    await asyncio.sleep(startup_delay)
    while True:
        if deps.max_attempts() > 0:
            try:
                acted = await sweep(deps)
                for a in acted:
                    if a["action"] != "waiting":
                        logger.warning("supervisor: %s", a)
            except Exception:  # noqa: BLE001 -- the loop outlives any one sweep
                logger.exception("supervisor: sweep failed")
        await asyncio.sleep(interval)


# ── the real dependencies' git and HTTP halves ──────────────────────────────

async def commit_landed(live_root: str, sha: str, base: str = "main") -> bool:
    """True when the task's commit is already on `base` -- as itself, or as
    the same patch under another sha.

    The commit, not the branch: a branch that was rebased and merged points
    at main and has no commits of its own, and so does a branch reset onto
    main after a conflict (see restore_task_workspace) -- one is landed and
    the other is not, and only the task's own commit tells them apart.
    `git cherry` marks a commit "-" when an equivalent patch is upstream, so
    work that landed rebased (new sha, same change) counts.
    """
    from agent.tools.git import _git

    if not re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
        return False
    if (await _git(f"merge-base --is-ancestor {sha} {base}", live_root, timeout=15))["ok"]:
        return True
    r = await _git(f"cherry {base} {sha}", live_root, timeout=30)
    if not r["ok"]:
        return False
    lines = [ln for ln in r["output"].splitlines() if ln.strip()]
    return bool(lines) and all(ln.startswith("-") for ln in lines)


async def live_is_clean(live_root: str) -> bool:
    from agent.tools.git import _git

    r = await _git("status --porcelain --untracked-files=no", live_root, timeout=15)
    return r["ok"] and not r["output"].strip()


async def review_services_up() -> bool:
    import httpx

    from agent.tools.review_gate import (REVIEW_CONTROL_HOST, REVIEW_CONTROL_PORT,
                                         REVIEW_SERVICE_HOST, REVIEW_SERVICE_PORT)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            a = await client.get(f"http://{REVIEW_CONTROL_HOST}:{REVIEW_CONTROL_PORT}/health")
            b = await client.get(f"http://{REVIEW_SERVICE_HOST}:{REVIEW_SERVICE_PORT}/health")
        return a.status_code == 200 and b.status_code == 200
    except Exception:  # noqa: BLE001 -- down is an answer, not an error
        return False
