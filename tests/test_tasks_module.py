"""agent/tasks.py: task creation, out of server.py and usable without it.

The extraction every remaining router seam was waiting on (docs/todo.md,
"Split agent/server.py"). These pin what makes it a real seam rather than a
move: the module does not import the server, the server's names for these are
the same objects, and a started task reaches the graph stream through
app.state with the state it should have.
"""
import asyncio
import subprocess
import sys
from types import SimpleNamespace

import agent.server as srv
from agent import live_state, tasks
from agent.classify import TaskClassification


import pytest


@pytest.mark.parametrize("module", ["agent.tasks", "agent.task_runtime"])
def test_the_seam_modules_do_not_import_the_server(module):
    """If one did, a router importing it would be the import cycle the seams
    exist to avoid."""
    out = subprocess.run(
        [sys.executable, "-c", f"import sys, {module}; print('agent.server' in sys.modules)"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_the_servers_run_state_names_are_task_runtimes_objects():
    from agent import task_runtime
    assert srv._publish is task_runtime.publish
    assert srv._live_task_log is task_runtime.live_task_log
    assert srv._task_event_seq is task_runtime.task_event_seq
    assert srv._claim_run_slot is task_runtime.claim_run_slot


def test_the_servers_names_are_the_same_objects():
    assert srv.write_task_meta is tasks.write_task_meta
    assert srv._attachments_note is tasks.attachments_note
    assert srv.app.state.stream_graph is srv._stream_graph


def test_a_started_task_reaches_the_stream_with_its_initial_state(monkeypatch):
    seen = {}

    async def stream(task_id, repo, goal, budget, state, **kw):
        seen.update(task_id=task_id, repo=repo, goal=goal, budget=budget, state=state, **kw)

    async def classify(goal, config):
        return TaskClassification(category="bug-fix", needs_tests=False)

    class _Store:
        async def aget(self, ns, key):
            return None

        async def aput(self, ns, key, value):
            seen["meta"] = value

    app = SimpleNamespace(state=SimpleNamespace(stream_graph=stream, config=object(), store=_Store()))
    monkeypatch.setattr(tasks, "classify_task", classify)

    async def go():
        out = await tasks.start_task(
            app, "  fix the login bug  ", "proj", 3.0, "auto",
            auto_approve_commands=False, require_merge_review=True,
            attachments=[{"kind": "image", "path": ".uploads/ab/shot.png"}], origin="github")
        await live_state.running_tasks.pop(out["task_id"])
        return out

    out = asyncio.run(go())
    assert out["category"] == "bug-fix"
    assert seen["task_id"] == out["task_id"] and seen["repo"] == "proj" and seen["budget"] == 3.0
    assert seen["state"]["goal"].startswith("fix the login bug")
    assert "shot.png" in seen["state"]["goal"]              # the attachments note reached the model
    assert seen["state"]["require_merge_review"] is True
    assert seen["meta"]["origin"] == "github"


def test_an_empty_goal_is_refused_before_anything_starts():
    import pytest
    from fastapi import HTTPException

    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as e:
        asyncio.run(tasks.start_task(app, "   ", "proj", None, "auto",
                                     auto_approve_commands=False, require_merge_review=True))
    assert e.value.status_code == 422
