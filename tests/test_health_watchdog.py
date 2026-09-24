"""The watchdog restarts for a wedged process and NEVER for a sick dependency.

That distinction is the whole reason this exists, and it is easy to get
backwards. storefront' own health controller spells out why: restarting fixes a
wedged process and does nothing for a database outage -- so a monitor that
restarts on readiness turns one incident into a restart loop against a
database that is already struggling.

The bounds matter as much as the trigger. An automatic restarter with no
ceiling is worse than none: it hides the fault it cannot fix behind a service
that keeps looking briefly alive.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "health_watchdog", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "health_watchdog.py")
wd = importlib.util.module_from_spec(_SPEC)
sys.modules["health_watchdog"] = wd
_SPEC.loader.exec_module(wd)

SVC = {"name": "svc", "repo": "R", "live": "http://live", "ready": "http://ready", "pm2": "svc"}


@pytest.fixture
def probes(monkeypatch):
    """Drive both probes by URL so a test states exactly what each returns."""
    table = {}

    def fake(url):
        ok = table.get(url, True)
        return ok, "200" if ok else "503", table.get(url + "#body", "")

    monkeypatch.setattr(wd, "_probe", fake)
    return table


@pytest.fixture
def restarts(monkeypatch):
    calls = []
    monkeypatch.setattr(wd, "_restart", lambda name, dry: (calls.append(name), (True, "ok"))[1])
    return calls


def run(state, probes, now=1000.0, times=1):
    out = []
    for i in range(times):
        out += wd.check(SVC, state, now + i, dry=False)
    return out


# ── the thing that must never be got backwards ─────────────────────────────

def test_readiness_failure_never_restarts(probes, restarts):
    """Alive but not ready = a dependency is down. A restart cannot fix that,
    and would reconnect-storm the database that is already failing."""
    probes["http://ready"] = False
    state = {}
    alerts = run(state, probes, times=wd.CONSECUTIVE_FAILS)
    assert restarts == [], "restarted on a readiness failure -- the one thing it must not do"
    assert alerts, "a dependency outage still has to reach a human"
    body = alerts[-1][1]
    assert "NOT restarting" in body
    assert "NOT READY" in body


def test_liveness_failure_does_restart(probes, restarts):
    probes["http://live"] = False
    state = {}
    alerts = run(state, probes, times=wd.CONSECUTIVE_FAILS)
    assert restarts == ["svc"], "a wedged process is exactly what this is for"
    assert "Restarted it" in alerts[-1][1]


def test_a_dead_process_is_not_reported_as_a_database_problem(probes, restarts):
    """When liveness fails, readiness fails too -- but saying "database down"
    then would be a guess presented as a finding."""
    probes["http://live"] = False
    probes["http://ready"] = False
    alerts = run({}, probes, times=wd.CONSECUTIVE_FAILS)
    assert "NOT READY" not in alerts[-1][1]


# ── bounds ─────────────────────────────────────────────────────────────────

def test_one_blip_is_not_an_incident(probes, restarts):
    probes["http://live"] = False
    run({}, probes, times=wd.CONSECUTIVE_FAILS - 1)
    assert restarts == [], "acted before the failure was consecutive"


def test_restarts_are_capped_per_hour(probes, restarts):
    probes["http://live"] = False
    state = {"svc": {"live_fails": wd.CONSECUTIVE_FAILS, "ready_fails": 0,
                     "restarts": [999.0] * wd.MAX_RESTARTS_PER_HOUR, "last_alert": 0, "down": True}}
    alerts = wd.check(SVC, state, 1000.0, dry=False)
    assert restarts == [], "kept restarting past the budget"
    assert "needs a person" in alerts[-1][1]


def test_the_budget_frees_up_once_the_hour_passes(probes, restarts):
    probes["http://live"] = False
    old = [10.0] * wd.MAX_RESTARTS_PER_HOUR          # more than an hour ago
    state = {"svc": {"live_fails": wd.CONSECUTIVE_FAILS, "ready_fails": 0,
                     "restarts": old, "last_alert": 0, "down": True}}
    wd.check(SVC, state, 10_000.0, dry=False)
    assert restarts == ["svc"], "an hour-old restart should not count against the budget forever"


def test_recovery_is_announced_and_clears_the_budget(probes, restarts):
    state = {"svc": {"live_fails": 0, "ready_fails": 0, "restarts": [999.0], "last_alert": 0,
                     "down": True}}
    alerts = wd.check(SVC, state, 1000.0, dry=False)
    assert "healthy again" in alerts[0][1], "a DOWN with no matching UP leaves you guessing"
    assert state["svc"]["restarts"] == []


def test_a_healthy_service_is_silent(probes, restarts):
    assert run({}, probes, times=5) == []
    assert restarts == []


def test_repeat_alerts_are_rate_limited(probes, restarts):
    """A service down for an hour must not send sixty messages."""
    probes["http://ready"] = False
    state = {"svc": {"live_fails": 0, "ready_fails": wd.CONSECUTIVE_FAILS,
                     "restarts": [], "last_alert": 999.0, "down": True}}
    assert wd.check(SVC, state, 1000.0, dry=False) == []


def test_dry_run_never_restarts(probes, monkeypatch):
    calls = []
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: calls.append(a))
    ok, detail = wd._restart("svc", dry=True)
    assert ok and calls == [] and "dry-run" in detail


def test_a_probe_that_throws_counts_as_down_not_as_a_crash(monkeypatch):
    """A watchdog that can raise is a watchdog that stops watching."""
    def boom(*a, **k):
        raise OSError("network gone")
    monkeypatch.setattr(wd.subprocess, "run", boom)
    ok, code, body = wd._probe("http://x")
    assert ok is False and code == "OSError" and body == ""


# ── the message says what failed, from the service's own answer ────────────

def _not_ready(probes, body):
    probes["http://ready"] = False
    probes["http://ready#body"] = body
    state = {}
    alerts = run(state, probes, times=wd.CONSECUTIVE_FAILS)
    return alerts[-1][1], state


def test_an_outside_feed_is_named_and_not_blamed_on_the_database(probes, restarts):
    """2026-09-24: a service's outside data feed dropped, and the alert
    said "its database probe is failing -- check Postgres" about a service
    with no database check at all."""
    body = ('{"ok":false,"checks":{"engine":{"ok":true,"running":true},'
            '"ws-public":{"ok":false,"connected":false,"lastMsgAgeMs":65012},'
            '"tickers":{"ok":false,"applicable":true,"count":12,"newestAgeMs":71000}},"service":"bot"}')
    msg, state = _not_ready(probes, body)
    assert "• ws-public: connected=false, lastMsgAgeMs=65012" in msg
    assert "• tickers:" in msg and "engine" not in msg.split("Failing:")[1].split("\n\n")[0]
    assert "outside feed" in msg
    assert "Postgres" not in msg and "database" not in msg
    assert "cannot bring back ws-public, tickers" in msg and restarts == []
    assert state["svc"]["failing"] == ["ws-public", "tickers"]


def test_a_database_outage_says_database(probes, restarts):
    terminus = ('{"status":"error","info":{},"error":{"database":{"status":"down",'
                '"message":"connect ECONNREFUSED 127.0.0.1:5432"}},"details":{}}')
    msg, _ = _not_ready(probes, terminus)
    assert '• database: message="connect ECONNREFUSED 127.0.0.1:5432"' in msg and "Postgres" in msg
    msg, _ = _not_ready(probes, '{"status":"degraded","database":"down","timestamp":"t"}')
    assert "• database: down" in msg and "Postgres" in msg


def test_an_answer_that_names_nothing_is_not_guessed_at(probes, restarts):
    for body in ("", "<html>Bad Gateway</html>", '{"status":"error"}'):
        msg, _ = _not_ready(probes, body)
        assert "does not say which check failed" in msg
        assert "Postgres" not in msg and "outside feed" not in msg


def test_recovery_says_what_came_back_and_how_long_it_took(probes, restarts):
    state = {}
    probes["http://ready"] = False
    probes["http://ready#body"] = '{"checks":{"tickers":{"ok":false}}}'
    for i in range(wd.CONSECUTIVE_FAILS):
        wd.check(SVC, state, 1000.0 + 60 * i, dry=False)
    probes["http://ready"] = True
    msg = wd.check(SVC, state, 1000.0 + 60 * 5, dry=False)[0][1]
    assert msg == "✅ svc is healthy again after 5 min — tickers is answering again. Nothing to do."
    assert "since" not in state["svc"] and "failing" not in state["svc"]


def test_a_service_without_a_readiness_probe_is_ready_when_live(probes, restarts):
    svc = {k: v for k, v in SVC.items() if k != "ready"}
    assert wd.check(svc, {}, 1000.0, dry=False) == []


def test_the_probe_keeps_the_body_and_the_code(monkeypatch):
    class R:
        stdout = '{"ok":false}' + wd._CODE_MARK + "503"
    monkeypatch.setattr(wd.subprocess, "run", lambda *a, **k: R())
    assert wd._probe("http://x") == (False, "503", '{"ok":false}')
    R.stdout = wd._CODE_MARK + "000"
    assert wd._probe("http://x") == (False, "no-response", "")
