"""Poll each service's health probes, and act on the RIGHT failure.

    .venv/bin/python scripts/health_watchdog.py [--dry-run]

Run from cron every minute. The whole point is the distinction storefront'
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

# What to watch is this INSTALL's business, not the repo's: the list names
# somebody's real services, their URLs and their pm2 process names, and no two
# deployments share one. It lives in a gitignored file beside this script, the
# same way the router's config.yaml and the reviewer's builtin projects do.
#
# `repo` is passed to notify_operators so a single-repo operator only hears
# about their own service (agent/notify.py's audit H1 fan-out filter). Probe
# URLs should point at the PROCESS -- localhost, not through nginx: this
# watchdog decides whether to restart a process, and a vhost or TLS problem is
# not something a pm2 restart fixes. Routing probes through the edge would let
# an edge failure trigger a restart that cannot possibly help.
SERVICES_FILE = Path(__file__).resolve().parent / "watchdog-services.local.json"


def load_services() -> list[dict]:
    """The services to watch, or an empty list.

    Empty is a valid answer, not an error: a fresh install watches nothing
    until an operator says what to watch, and a watchdog that crashed on a
    missing file would take out the cron job that runs it every minute.
    """
    if not SERVICES_FILE.exists():
        return []
    try:
        data = json.loads(SERVICES_FILE.read_text())
    except (OSError, ValueError) as e:
        print(f"watchdog: {SERVICES_FILE.name} is unreadable ({e}); watching nothing")
        return []
    if not isinstance(data, list):
        print(f"watchdog: {SERVICES_FILE.name} must be a list of services; watching nothing")
        return []
    out = []
    for svc in data:
        missing = [k for k in ("name", "repo", "live", "pm2") if not svc.get(k)]
        if missing:
            print(f"watchdog: skipping a service entry missing {missing}")
            continue
        out.append(svc)
    return out


_CODE_MARK = "\n__watchdog_http_code__="
BODY_LIMIT = 64_000


def _probe(url: str) -> tuple[bool, str, str]:
    """(answered 200, the HTTP code, the body). Never raises -- a watchdog
    that can crash is a watchdog that stops watching.

    The body is kept because it is where a readiness endpoint says WHICH
    dependency failed. Without it the alert could only guess, and guessed
    "check Postgres" for a service whose outside data feed had dropped
    (2026-09-24) -- a service with no database check at all."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", str(TIMEOUT_S), "-w", _CODE_MARK + "%{http_code}", url],
            capture_output=True, text=True, timeout=TIMEOUT_S + 5, errors="replace")
        body, _, code = (r.stdout or "").rpartition(_CODE_MARK)
        code = code.strip()
        if not code or code == "000":
            return False, "no-response", ""
        return code == "200", code, body[:BODY_LIMIT]
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__, ""


# Keys a readiness body uses to describe itself, never a dependency.
_NOT_A_CHECK = {"ok", "status", "degraded", "service", "timestamp", "time", "version", "uptime",
                "info", "details", "error", "checks", "message"}
_BAD = {"down", "error", "fail", "failed", "failing", "unhealthy", "unavailable", "timeout", "stale"}


def _describe(value) -> str:
    """A check's own fields, short: `connected=false, lastMsgAgeMs=65012`."""
    if not isinstance(value, dict):
        return "" if value in (False, None) or str(value).lower() in _BAD else str(value)[:120]
    parts = []
    for k, v in value.items():
        if k in ("ok",) or (k == "status" and str(v).lower() in _BAD):
            continue
        if isinstance(v, (dict, list)):
            continue
        parts.append(f"{k}={json.dumps(v) if isinstance(v, str) else str(v).lower() if isinstance(v, bool) else v}")
    return ", ".join(parts)[:200]


def failing_checks(body: str) -> list[tuple[str, str]]:
    """(check name, what it reports) for each failing check in a readiness
    body, in the shapes these services use:

      {"checks": {"tickers": {"ok": false, "count": 0}, ...}}     a checks map
      {"status": "error", "error": {"database": {"status": "down", "message": ...}}}   NestJS terminus
      {"status": "degraded", "database": "down"}                    flat

    Empty when the body is not JSON or names nothing -- the alert then says
    it does not know, rather than guessing."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    checks = data.get("checks")
    if isinstance(checks, dict):
        for name, v in checks.items():
            bad = (v is False or (isinstance(v, dict) and (v.get("ok") is False
                   or str(v.get("status", "")).lower() in _BAD)))
            if bad:
                out.append((str(name), _describe(v)))
    err = data.get("error")
    if isinstance(err, dict):
        for name, v in err.items():
            if (str(name), _describe(v)) not in out:
                out.append((str(name), _describe(v)))
    for name, v in data.items():
        if name in _NOT_A_CHECK:
            continue
        if v is False or (isinstance(v, str) and v.lower() in _BAD):
            out.append((str(name), "" if v is False else str(v)))
    if not out and isinstance(data.get("message"), str):
        out.append(("", data["message"][:200]))
    return out


# What a failing check usually means, by what it is called. Matched on the
# name, so a new service gets a useful hint without being listed here.
_HINTS = (
    (("database", "postgres", "prisma", "db", "sql", "pg"),
     "Its database is not answering: check Postgres (`pg_lsclusters`) and the service's connection pool."),
    (("redis", "cache", "queue", "bull"), "Its Redis/queue is not answering: check Redis."),
    (("ws", "socket", "ticker", "candle", "feed", "exchange", "market", "stream", "upstream", "api"),
     "An outside feed it depends on is not answering (an exchange, an upstream API). "
     "That usually comes back on its own; if it does not, check the provider's status."),
    (("worker", "pool"), "Its worker pool is short of workers."),
    (("engine", "cycle", "loop", "scheduler"),
     "Its main loop is not completing. If this does not clear by itself, read its logs: a stuck loop "
     "is the one readiness failure a restart can fix."),
    (("disk", "storage", "fs"), "It is short of disk: check `df -h`."),
)


def _hint(names: list[str]) -> str:
    hints: list[str] = []
    for name in names:
        low = name.lower().replace("_", "-")
        tokens = set(low.replace("-", " ").split())
        for keys, text in _HINTS:
            if any(k in tokens or low.startswith(k) for k in keys):
                if text not in hints:
                    hints.append(text)
                break
    return " ".join(hints)


def _ago(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


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
    logs = f"`pm2 logs {svc['pm2']} --lines 50`"

    live_ok, live_code, _ = _probe(svc["live"])
    # Only consult readiness when the process is actually up; a dead process
    # fails both, and reporting "database down" then would be a lie. A
    # service with no readiness probe is ready whenever it is live.
    if not live_ok:
        ready_ok, ready_code, ready_body = False, "n/a", ""
    elif svc.get("ready"):
        ready_ok, ready_code, ready_body = _probe(svc["ready"])
    else:
        ready_ok, ready_code, ready_body = True, "200", ""

    if live_ok:
        st["live_fails"] = 0
    else:
        st["live_fails"] += 1

    if live_ok and ready_ok:
        st["ready_fails"] = 0
        if st["down"]:
            was = st.get("failing") or []
            since = st.get("since")
            st["down"] = False
            st["restarts"] = []
            what = f" — {', '.join(was)} {'is' if len(was) == 1 else 'are'} answering again" if was else ""
            took = f" after {_ago(now - since)}" if since else ""
            alerts.append((svc["repo"], f"✅ {key} is healthy again{took}{what}. Nothing to do."))
        st.pop("since", None)
        st.pop("failing", None)
        return alerts

    st.setdefault("since", now)

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
        answer = f"no answer within {TIMEOUT_S}s" if live_code == "no-response" else f"HTTP {live_code}"
        alerts.append((svc["repo"], (
            f"🔄 {key} stopped answering ({answer}) for {st['live_fails']} checks in a row — the process "
            f"itself is stuck or gone, which is what a restart fixes. "
            + (f"Restarted it ({len(recent) + 1}/{MAX_RESTARTS_PER_HOUR} this hour)."
               if ok else f"The restart FAILED: {detail.strip()[:200]}")
            + f"\n\nIf it happens again, the reason is in {logs}.")))
        return alerts

    # ── alive but not ready: a dependency is down. Restarting cannot help. ──
    st["ready_fails"] += 1
    failing = failing_checks(ready_body)
    names = [n for n, _ in failing if n]
    st["failing"] = names
    if st["ready_fails"] >= CONSECUTIVE_FAILS and stale_alert:
        st["last_alert"] = now
        st["down"] = True
        if failing:
            lines = "\n".join(f"• {n or 'reason'}" + (f": {d}" if d else "") for n, d in failing[:6])
            what = f"Failing:\n{lines}"
            cannot = f"a restart cannot bring back {', '.join(names)}" if names else "a restart cannot fix this"
        else:
            what = "Its readiness answer does not say which check failed."
            cannot = "a restart cannot fix a dependency"
        hint = _hint(names)
        alerts.append((svc["repo"], (
            f"🟠 {key} is up but NOT READY (HTTP {ready_code}) for {_ago(now - st['since'])}.\n\n"
            f"{what}\n\n"
            + (f"{hint}\n\n" if hint else "")
            + f"NOT restarting: the process is fine and {cannot}; it would only drop the connections "
            f"that still work. You will get a message when it recovers. Logs: {logs}.")))
    return alerts


async def main(dry: bool) -> int:
    now = time.time()
    state = _load_state()
    pending: list[tuple[str, str]] = []
    for svc in load_services():
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
