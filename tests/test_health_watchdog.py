"""The watchdog restarts for a wedged process and NEVER for a sick dependency.

That distinction is the whole reason this exists, and it is easy to get
backwards. 3DSteals' own health controller spells out why: restarting fixes a
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
        return ok, "200" if ok else "503"

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
    assert "ALIVE but NOT READY" in body


def test_liveness_failure_does_restart(probes, restarts):
    probes["http://live"] = False
    state = {}
    alerts = run(state, probes, times=wd.CONSECUTIVE_FAILS)
    assert restarts == ["svc"], "a wedged process is exactly what this is for"
    assert "restarted" in alerts[-1][1]


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
    assert "recovered" in alerts[0][1], "a DOWN with no matching UP leaves you guessing"
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
    ok, code = wd._probe("http://x")
    assert ok is False and code == "OSError"
