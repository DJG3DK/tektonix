"""Telegram alerts: message builders, the best-effort sender, and the
task-status alert gate in server.py.

The operator requirement (2026-08-27): alerts for everything important, each
carrying details AND cost. The invariants worth pinning: an alert failure
never propagates, secrets never leave the masked settings shape, and a task
buzzes the phone once per distinct stop -- not once per stream cycle.
"""

import httpx
import pytest

import agent.server as server
from agent.notify import _MAX_LEN, notify_operators, send_telegram, task_alert


# ---------------------------------------------------------------------------
# task_alert -- the message the operator actually reads
# ---------------------------------------------------------------------------


def test_alert_carries_status_repo_goal_cost_and_detail():
    text = task_alert("escalated", "my-service", "# Fix the screener\nbody", 1.36,
                      "review service did not review within 900s")
    assert "ESCALATED" in text
    assert "repo: my-service" in text
    assert "# Fix the screener" in text and "body" not in text  # first line only
    assert "cost so far: $1.36" in text
    assert "900s" in text


def test_alert_without_cost_omits_the_cost_line():
    assert "cost so far" not in task_alert("done", "my-service", "g", None)


def test_every_wired_status_has_a_human_line():
    for kind in ("escalated", "awaiting_approval", "awaiting_merge", "done",
                 "error", "auto_resumed", "planning_error"):
        text = task_alert(kind, "r", "g", 0.5)
        assert not text.startswith(kind), f"{kind} fell through to the raw key"


# ---------------------------------------------------------------------------
# send_telegram -- best-effort, always
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    status = 200
    body = "ok"
    raises = None
    last_payload = None

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None):
        _FakeAsyncClient.last_payload = json
        if _FakeAsyncClient.raises:
            raise _FakeAsyncClient.raises
        class _R:
            status_code = _FakeAsyncClient.status
            text = _FakeAsyncClient.body
        return _R()


@pytest.fixture(autouse=True)
def _fake_httpx(monkeypatch):
    _FakeAsyncClient.status, _FakeAsyncClient.raises, _FakeAsyncClient.last_payload = 200, None, None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


async def test_send_success():
    assert await send_telegram("tok", "42", "hello") is True
    assert _FakeAsyncClient.last_payload["chat_id"] == "42"


async def test_send_truncates_to_telegram_limit():
    await send_telegram("tok", "42", "x" * 10_000)
    assert len(_FakeAsyncClient.last_payload["text"]) <= _MAX_LEN + 1


async def test_http_error_returns_false_never_raises():
    _FakeAsyncClient.status = 400
    assert await send_telegram("tok", "42", "hello") is False


async def test_network_error_returns_false_never_raises():
    _FakeAsyncClient.raises = OSError("no route to host")
    assert await send_telegram("tok", "42", "hello") is False


async def test_notify_operators_survives_a_broken_recipient_query():
    class _BrokenPool: ...
    import agent.auth as auth_mod
    # get_telegram_targets will raise on a non-pool -- must come back 0, not raise
    assert await notify_operators(_BrokenPool(), "hi") == 0


# ---------------------------------------------------------------------------
# the server-side alert gate
# ---------------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "notify_operators_bg",
                        lambda pool, text, repo=None: calls.append((text, repo)))
    monkeypatch.setattr(server.app.state, "auth_pool", object(), raising=False)
    server._last_task_alert.clear()
    return calls


def test_rest_states_alert_with_detail_and_cost(sent):
    server._alert_task_status("t1", "escalated", "my-service", "goal", 2.5, "why it stopped")
    assert len(sent) == 1 and "why it stopped" in sent[0][0] and "$2.50" in sent[0][0]
    assert sent[0][1] == "my-service", "the alert must carry its repo so the fan-out can scope it"


def test_running_and_stopped_never_alert(sent):
    server._alert_task_status("t1", "running", "r", "g", 0.1, None)
    server._alert_task_status("t1", "stopped", "r", "g", 0.1, None)
    assert sent == []


def test_the_same_stop_alerts_once_across_stream_cycles(sent):
    """A resumed task re-enters _stream_graph and re-derives the same rest
    state; one distinct stop = one buzz."""
    for _ in range(3):
        server._alert_task_status("t1", "awaiting_merge", "r", "g", 1.0, "commit abc passed review")
    assert len(sent) == 1


def test_a_new_stop_on_the_same_task_alerts_again(sent):
    server._alert_task_status("t1", "awaiting_approval", "r", "g", 1.0, "run rm -rf?")
    server._alert_task_status("t1", "awaiting_approval", "r", "g", 1.2, "run sudo thing?")
    server._alert_task_status("t1", "done", "r", "g", 1.5, None)
    assert len(sent) == 3


# ---------------------------------------------------------------------------
# service-restart watch -- the pure diff, pinned
# ---------------------------------------------------------------------------

from agent.notify import diff_services, snapshot_services


def _svc(pid, restarts, status="online"):
    return (pid, restarts, status)


def test_snapshot_parses_pm2_jlist_shape():
    snap = snapshot_services([
        {"name": "model-router", "pid": 111, "pm2_env": {"restart_time": 3, "status": "online"}},
        {"name": "broken-entry"},  # tolerated, skipped fields default
    ])
    assert snap["model-router"] == (111, 3, "online")


def test_a_restart_is_reported_once_with_its_count():
    prev = {"model-router": _svc(111, 3)}
    cur = {"model-router": _svc(222, 4)}
    assert diff_services(prev, cur) == ["🔄 service restarted: model-router (restart #4)"]


def test_a_pid_change_without_counter_bump_still_reports():
    """pm2 resets restart_time on `pm2 delete` + start -- the pid is the
    tell that the process is not the one we knew."""
    prev = {"my-service": _svc(111, 5)}
    cur = {"my-service": _svc(999, 0)}
    assert diff_services(prev, cur) == ["🔄 service restarted: my-service (restart #0)"]


def test_a_service_going_down_reports_down_not_restart():
    prev = {"model-router": _svc(111, 3)}
    cur = {"model-router": _svc(None, 3, "errored")}
    assert diff_services(prev, cur) == ["🔴 service DOWN: model-router (status: errored)"]


def test_a_service_removed_from_pm2_reports_gone():
    prev = {"old-svc": _svc(1, 0)}
    assert diff_services(prev, {}) == ["🔴 service GONE from pm2: old-svc"]


def test_steady_state_reports_nothing():
    snap = {"model-router": _svc(111, 3), "my-service": _svc(4, 9)}
    assert diff_services(snap, dict(snap)) == []


def test_a_new_service_appearing_is_not_an_incident():
    assert diff_services({}, {"new-svc": _svc(5, 0)}) == []
