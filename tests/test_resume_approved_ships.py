"""Resuming an escalated task whose commit is already approved re-ships it.

It used to go back to work with a "continue the task" note, paying for a pass
that redid finished changes, when all that had failed was the way out.
"""
import asyncio
from types import SimpleNamespace

import pytest
from langgraph.graph import END

import agent.server as server
from agent.routers import tasks as tasks_routes

# The routes read the app off the request (agent/routers/tasks.py).
_REQUEST = SimpleNamespace(app=server.app)
from agent.outer_graph import _route_after_verify


class _Ckpt:
    def __init__(self, values):
        self.values = values


class _Graph:
    def __init__(self, values):
        self.values = values
        self.patches = []

    async def aget_state(self, cfg):
        return _Ckpt(self.values)

    async def aupdate_state(self, cfg, patch, **kw):
        self.patches.append((dict(patch), kw.get("as_node")))


class _Store:
    async def aget(self, ns, key):
        return None


def _escalated(**over):
    v = {"repo": "proj", "goal": "g", "budget_usd": 5.0, "max_iterations": 40,
         "escalated": True, "escalation_reason": "review service did not review abc within 900s",
         "committed_sha": "abc123", "merge_approved_sha": "abc123"}
    v.update(over)
    return v


@pytest.fixture
def wired(monkeypatch):
    def _wire(values):
        g = _Graph(values)
        monkeypatch.setattr(server.app.state, "graph", g, raising=False)
        monkeypatch.setattr(server.app.state, "store", _Store(), raising=False)
        monkeypatch.setattr(tasks_routes, "check_repo_access", lambda *a, **k: None)
        started = []

        async def _stream(*a, **k):
            started.append(a)
        monkeypatch.setattr(server.app.state, "stream_graph", _stream, raising=False)
        return g
    return _wire


async def _resume(message=None):
    req = server.ResumeTaskRequest(additional_budget_usd=0, message=message)
    out = await server.resume_task(_REQUEST, "t-1", req, user=object())
    await asyncio.sleep(0)
    server._running_tasks.pop("t-1", None)
    return out


async def test_an_approved_commit_goes_straight_back_to_shipping(wired):
    g = wired(_escalated())
    await _resume()
    patch, as_node = g.patches[-1]
    assert as_node == "verify_and_ship"
    assert patch["escalated"] is False
    assert "pending_feedback" not in patch   # no work pass
    assert _route_after_verify({**g.values, **patch}) == "verify_and_ship"


async def test_an_operator_message_still_means_more_work(wired):
    g = wired(_escalated())
    await _resume("also handle the empty case")
    patch, _ = g.patches[-1]
    assert "also handle the empty case" in patch["pending_feedback"]
    assert _route_after_verify({**g.values, **patch}) == "work"


@pytest.mark.parametrize("approved", [None, "older999"])
async def test_without_an_approval_for_this_commit_it_goes_to_work(wired, approved):
    g = wired(_escalated(merge_approved_sha=approved))
    await _resume()
    patch, _ = g.patches[-1]
    assert patch["pending_feedback"]
    assert _route_after_verify({**g.values, **patch}) == "work"


def test_route_is_end_while_escalated():
    assert _route_after_verify(_escalated()) == END
