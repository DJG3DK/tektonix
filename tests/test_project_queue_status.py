"""A task waiting for the project says so.

One task per project is a hard constraint: they share one worktree, so two
of them editing it would interleave their commits (agent/graph.py's
project_lock). The wait itself was fine. What was not fine is that the status
was written BEFORE the lock was taken, so a queued task was indistinguishable
from a working one -- the sidebar said "Running" with a live pulse, the log
showed nothing, the spend showed nothing, and the only way to tell was to
notice it had been like that for a while.

With 59 open alerts on one project, that is a state an operator reaches by
doing the obvious thing twice.
"""
import asyncio

import pytest

from agent.graph import project_lock


async def test_no_callback_when_the_project_is_free():
    """The common path writes nothing and costs nothing."""
    called = []
    async with project_lock("free-repo", None, on_wait=lambda: called.append(1) or asyncio.sleep(0)):
        pass
    assert called == []


async def test_the_callback_fires_when_another_task_holds_the_project():
    fired = asyncio.Event()

    async def announce():
        fired.set()

    held = asyncio.Event()
    release = asyncio.Event()

    async def first():
        async with project_lock("busy-repo", None):
            held.set()
            await release.wait()

    async def second():
        await held.wait()
        async with project_lock("busy-repo", None, on_wait=announce):
            return "got it"

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    await asyncio.wait_for(fired.wait(), timeout=5)
    # ...and it fired BEFORE the lock was granted, which is the whole point:
    # announcing the wait after it ends announces nothing.
    assert not t2.done()
    release.set()
    assert await asyncio.wait_for(t2, timeout=5) == "got it"
    await t1


async def test_the_callback_fires_once_not_per_poll():
    calls = []

    async def announce():
        calls.append(1)

    held, release = asyncio.Event(), asyncio.Event()

    async def first():
        async with project_lock("once-repo", None):
            held.set()
            await release.wait()

    async def second():
        await held.wait()
        async with project_lock("once-repo", None, on_wait=announce):
            pass

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.2)
    release.set()
    await asyncio.gather(t1, t2)
    assert calls == [1]


async def test_a_failing_callback_never_blocks_the_task():
    """A status update that cannot be written must not stop the task it
    describes from running."""
    async def boom():
        raise RuntimeError("store is down")

    held, release = asyncio.Event(), asyncio.Event()

    async def first():
        async with project_lock("boom-repo", None):
            held.set()
            await release.wait()

    async def second():
        await held.wait()
        async with project_lock("boom-repo", None, on_wait=boom):
            return "ran anyway"

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    await asyncio.sleep(0.2)
    release.set()
    assert await asyncio.wait_for(t2, timeout=5) == "ran anyway"
    await t1


# --- the consumers that a new status value could break --------------------

def test_a_queued_task_does_not_page_the_operator():
    """`queued` is a normal step on the way to running, not an event. Without
    this, starting a second task on a busy project sends a Telegram alert."""
    import inspect

    from agent import server

    src = inspect.getsource(server._alert_task_status)
    assert '("running", "queued", "stopped")' in src


def test_a_task_orphaned_while_queued_is_auto_resumed():
    """Same problem as an orphaned running task -- a status the store believes
    with no process behind it -- and the restart-recovery scan skipped it
    because it only looked for "running"."""
    import inspect

    from agent import server

    src = inspect.getsource(server._auto_resume_orphaned_tasks)
    assert 'not in ("running", "queued")' in src


def test_the_status_is_written_only_once_the_project_is_held():
    """The ordering IS the bug. `_mark("running")` has to sit inside the
    `async with`, or the task announces work it has not been cleared to start."""
    import inspect
    import re

    from agent import server

    src = inspect.getsource(server._stream_graph)
    lock_at = src.index("async with project_lock(")
    running_at = src.index('await _mark("running")')
    assert running_at > lock_at, '"running" is still written before the lock'
    # And the pre-lock mark says queued.
    assert re.search(r'await _mark\("queued"\)\n', src[:lock_at])
