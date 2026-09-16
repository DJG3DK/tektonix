"""Poll each service's health probes, and act on the RIGHT failure.

    .venv/bin/python scripts/health_watchdog.py [--dry-run]

Run from cron every minute. The whole point is the distinction 3DSteals'
own health controller documents:

    "There used to be one endpoint that returned 503 when the database probe
     failed, so a monitor could not tell 'this process is wedged' from 'this
     process is fine but Postgres is down' -- and the usual reaction to a
     failing health check, restarting, fixes the first and does nothing for
     the second."

So:

  * LIVENESS fails -> the process cannot serve at all. Restarting is the
    correct response, and this restarts it.
  * READINESS fails while liveness passes -> the process is fine and a
    dependency is not. Restarting cannot help and makes it worse: it throws
    away warm connections and hammers an already-struggling database with a
    reconnect storm. This ALERTS ONLY, and says so in the alert.

pm2 already restarts a process that CRASHES. What it cannot see is a process
that is alive but wedged -- event loop blocked, accepting sockets, answering
nothing. That is the gap liveness fills and the only thing this restarts for.

Two bounds, because an automatic restarter that cannot stop is worse than no
restarter:

  * CONSECUTIVE_FAILS before acting, so one dropped packet is not an incident.
  * MAX_RESTARTS_PER_HOUR, after which it alerts and stops trying. A service
    that needs a fourth restart in an hour is not going to be fixed by a
    fifth, and the restart loop hides the real fault.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config  # noqa: E402
from agent.graph import _make_pool  # noqa: E402
from agent.notify import notify_operators  # noqa: E402

STATE_PATH = Path(os.environ.get("HEALTH_WATCHDOG_STATE")
                  or Path(__file__).resolve().parent.parent / "data" / "health_watchdog.json")

CONSECUTIVE_FAILS = 3          # ~3 minutes at a 1-minute cron
MAX_RESTARTS_PER_HOUR = 3
ALERT_REPEAT_S = 1800          # re-alert on a still-broken service at most this often
TIMEOUT_S = 12

# repo is passed to notify_operators so a single-repo operator only hears
# about their own service (agent/notify.py's audit H1 fan-out filter).
SERVICES = [
    {
        "name": "3dsteals-api",
        "repo": "3DSteals",
        "live": "https://3dsteals.com/api/v1/health/live",
        "ready": "https://3dsteals.com/api/v1/health/ready",
        "pm2": "3dsteals-api",
    },
    # 3d-bot gained these on 2026-09-16 (task 230eed5b). Probed on localhost
    # rather than through nginx on purpose: this watchdog decides whether to
    # restart the PROCESS, and a vhost or TLS problem is not something a pm2
    # restart fixes -- routing it through nginx would let an edge failure
    # trigger a restart that cannot help, which is the same mistake as
    # restarting on readiness.
    {
        "name": "3d-bot",
        "repo": "3d-bot",
        "live": "http://127.0.0.1:14001/healthz",
        "ready": "http://127.0.0.1:14001/readyz",
        "pm2": "3d-bot",
    },
    {
        "name": "3d-bot-compute",
        "repo": "3d-bot",
        "live": "http://127.0.0.1:14002/healthz",
        "ready": "http://127.0.0.1:14002/readyz",
        "pm2": "3d-bot-compute",
    },
]


def _probe(url: str) -> tuple[bool, str]:
    """True when the probe answers 200. Never raises -- a watchdog that can
    crash is a watchdog that stops watching."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", str(TIMEOUT_S), url],
            capture_output=True, text=True, timeout=TIMEOUT_S + 5)
        code = (r.stdout or "").strip()
        return code == "200", code or "no-response"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:  # noqa: BLE001 -- a missing or corrupt file starts clean
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, STATE_PATH)
    except Exception as e:  # noqa: BLE001
        print(f"[watchdog] could not persist state: {e}", file=sys.stderr)


def _restart(pm2_name: str, dry: bool) -> tuple[bool, str]:
    if dry:
        return True, "dry-run: would restart"
    try:
        r = subprocess.run(["pm2", "restart", pm2_name, "--update-env"],
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stdout or r.stderr or "")[-300:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _recent_restarts(hist: list, now: float) -> list:
    return [t for t in hist if now - t < 3600]


def check(svc: dict, state: dict, now: float, dry: bool) -> list[tuple[str, str]]:
    """Returns (repo, message) alerts to send. Mutates `state` for this service."""
    key = svc["name"]
    st = state.setdefault(key, {"live_fails": 0, "ready_fails": 0, "restarts": [], "last_alert": 0,
                                "down": False})
    alerts: list[tuple[str, str]] = []

    live_ok, live_code = _probe(svc["live"])
    # Only consult readiness when the process is actually up; a dead process
    # fails both, and reporting "database down" then would be a lie.
    ready_ok, ready_code = _probe(svc["ready"]) if live_ok else (False, "n/a")

    if live_ok:
        st["live_fails"] = 0
    else:
        st["live_fails"] += 1

    if live_ok and ready_ok:
        st["ready_fails"] = 0
        if st["down"]:
            st["down"] = False
            st["restarts"] = []
            alerts.append((svc["repo"], f"✅ {key} recovered — liveness and readiness both green again."))
        return alerts

    # A service that has never alerted must alert NOW. Comparing the elapsed
    # time alone made the very first alert depend on the clock being larger
    # than ALERT_REPEAT_S -- true for a unix timestamp, false the moment
    # anything (a test, a fake clock) starts counting from zero, and a
    # first-alert path that only works because the number is big is a
    # first-alert path nobody has actually exercised.
    last = st.get("last_alert", 0)
    stale_alert = (not last) or (now - last > ALERT_REPEAT_S)

    # ── the process itself is not serving: restart is the right answer ──────
    if not live_ok:
        if st["live_fails"] < CONSECUTIVE_FAILS:
            return alerts  # not yet an incident
        recent = _recent_restarts(st["restarts"], now)
        if len(recent) >= MAX_RESTARTS_PER_HOUR:
            if stale_alert:
                st["last_alert"] = now
                alerts.append((svc["repo"], (
                    f"🔴 {key} liveness DOWN (HTTP {live_code}) and the restart budget is spent "
                    f"— {len(recent)} restarts in the last hour already. NOT restarting again: a "
                    f"service that needs a fourth restart in an hour will not be fixed by a fifth, "
                    f"and the loop hides the real fault. This one needs a person.")))
            st["down"] = True
            return alerts
        ok, detail = _restart(svc["pm2"], dry)
        st["restarts"] = recent + [now]
        st["down"] = True
        st["last_alert"] = now
        alerts.append((svc["repo"], (
            f"🔄 {key} liveness DOWN (HTTP {live_code}) for {st['live_fails']} consecutive checks "
            f"— {'restarted' if ok else 'RESTART FAILED'} it automatically "
            f"({len(recent) + 1}/{MAX_RESTARTS_PER_HOUR} this hour).\n{detail.strip()[:200]}")))
        return alerts

    # ── alive but not ready: a dependency is down. Restarting cannot help. ──
    st["ready_fails"] += 1
    if st["ready_fails"] >= CONSECUTIVE_FAILS and stale_alert:
        st["last_alert"] = now
        st["down"] = True
        alerts.append((svc["repo"], (
            f"🟠 {key} is ALIVE but NOT READY (readiness HTTP {ready_code}) for "
            f"{st['ready_fails']} consecutive checks — its database probe is failing.\n\n"
            f"Deliberately NOT restarting: the process is serving fine, the dependency is not, "
            f"and a restart would drop warm connections and reconnect-storm a database that is "
            f"already struggling. Check Postgres.")))
    return alerts


async def main(dry: bool) -> int:
    now = time.time()
    state = _load_state()
    pending: list[tuple[str, str]] = []
    for svc in SERVICES:
        try:
            pending += check(svc, state, now, dry)
        except Exception as e:  # noqa: BLE001 -- one service must not stop the rest
            print(f"[watchdog] {svc['name']} check failed: {e}", file=sys.stderr)
    _save_state(state)

    for _repo, msg in pending:
        print(f"[watchdog] {msg.splitlines()[0]}")
    if pending and not dry:
        cfg = load_config()
        pool = _make_pool(cfg)
        await pool.open()
        try:
            for repo, msg in pending:
                await notify_operators(pool, msg, repo=repo)
        finally:
            await pool.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="probe and report; never restart or alert")
    raise SystemExit(asyncio.run(main(ap.parse_args().dry_run)))
