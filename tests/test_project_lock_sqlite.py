"""The third branch of project_lock: one task per project without a database.

These run REAL SUBPROCESSES rather than threads or a fake. flock is a
property of a process's open file description, and every interesting thing
about it -- that a second process waits, that a killed holder releases
instantly -- is invisible to a test that never leaves this one. A mocked
flock would have asserted that we call the function we call.

The two limitations are tested as deliberately as the guarantees: two state
directories do not contend, and a platform without flock refuses rather than
pretending.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from agent import file_lock, graph

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What a holder subprocess does: take the lock, say so, and wait to be told
# (or killed). Line-buffered, because the parent reads that line to know the
# lock is actually held rather than sleeping and hoping.
_HOLDER = textwrap.dedent("""
    import asyncio, sys
    sys.path.insert(0, {root!r})
    from agent import file_lock

    async def main():
        async with file_lock.hold({dsn!r}, "held-project", 1234):
            print("held", flush=True)
            sys.stdin.readline()

    asyncio.run(main())
""")


# A whole process whose wait for the lock is cancelled, which then tries to
# exit. Run as a subprocess because what is being asserted is that the
# INTERPRETER can shut down -- a thread parked in an uninterruptible flock()
# is joined by asyncio.run's shutdown_default_executor and by threading's
# exit handler, and neither is visible from inside a test.
_CANCELLED_WAITER = textwrap.dedent("""
    import asyncio, sys
    sys.path.insert(0, {root!r})
    from agent import file_lock

    async def main():
        held = file_lock.hold({dsn!r}, "held-project", 1234)
        try:
            await asyncio.wait_for(held.__aenter__(), 0.5)
        except TimeoutError:
            print("cancelled", flush=True)

    asyncio.run(main())
    print("exited", flush=True)
""")


def _holder(dsn: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER.format(root=REPO_ROOT, dsn=dsn)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held", "the holder never took the lock"
    return proc


async def _acquire(dsn: str, timeout: float) -> bool:
    """Take the lock and give it straight back, or give up after `timeout`."""
    held = file_lock.hold(dsn, "held-project", 1234)
    try:
        await asyncio.wait_for(held.__aenter__(), timeout)
    except TimeoutError:
        return False
    await held.__aexit__(None, None, None)
    return True


@pytest.fixture
def dsn(tmp_path):
    return f"sqlite:///{tmp_path}/state.db"


def test_the_lock_file_sits_beside_the_database_and_carries_the_shared_key(dsn, tmp_path):
    """Where it lives is part of the contract: the state directory is the
    unit this lock covers, and the key is the one the Postgres branch locks
    on, so a project's identity does not depend on which backend is open."""
    path = file_lock.lock_path(dsn, "a repo/with punctuation", graph.advisory_key("a repo/with punctuation"))
    assert path.parent == tmp_path / "locks"
    assert path.name.endswith(f"{graph.advisory_key('a repo/with punctuation') & 0xFFFFFFFF:08x}.lock")
    assert "/" not in path.name and " " not in path.name


async def test_a_second_process_waits_for_the_first(dsn):
    holder = _holder(dsn)
    try:
        assert await _acquire(dsn, timeout=0.75) is False, \
            "the lock was free while another process held it"
        holder.stdin.write("go\n")
        holder.stdin.flush()
        holder.wait(timeout=10)
        assert await _acquire(dsn, timeout=5) is True, \
            "the lock was not released when its holder exited normally"
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


async def test_a_cancelled_wait_lets_the_process_exit(dsn):
    """The wait polls instead of parking a thread on a blocking flock, and
    this is the incident that decided it.

    LOCK_EX cannot be interrupted, so a cancelled wait left a worker thread
    of the default executor blocked in it for as long as the other process
    held the project -- and asyncio.run() then hung in
    shutdown_default_executor() joining that thread. The task was cancelled
    and the process could never exit, which is the worst shape this could
    have taken: a ctrl-c that leaves the CLI unkillable.
    """
    holder = _holder(dsn)
    try:
        waiter = subprocess.Popen(
            [sys.executable, "-c", _CANCELLED_WAITER.format(root=REPO_ROOT, dsn=dsn)],
            stdout=subprocess.PIPE, text=True)
        # The holder is still holding throughout: the waiter must exit
        # anyway, rather than waiting for a lock nobody wants any more.
        out, _ = waiter.communicate(timeout=15)

        assert waiter.returncode == 0, "the process could not exit after its wait was cancelled"
        assert out.split() == ["cancelled", "exited"]
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


async def test_a_killed_holder_releases_the_project(dsn):
    """The property that chose flock over a lock file. SIGKILL runs no
    cleanup, so a lock file would still be sitting there claiming the
    project and would need a stale-pid guess to clear. The kernel drops an
    flock when the process dies, with nobody cleaning up -- which is exactly
    what the Postgres advisory lock does when its connection drops.
    """
    holder = _holder(dsn)
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=10)

    started = time.monotonic()
    assert await _acquire(dsn, timeout=5) is True
    assert time.monotonic() - started < 2, "a killed holder's claim must not have to time out"


async def test_the_lock_file_survives_its_holder_and_is_reused(dsn):
    """Released by closing the fd, never by unlinking: deleting it would
    race a process that has already opened it and is waiting, and the two
    would then hold two different inodes -- two locks on one project."""
    path = file_lock.lock_path(dsn, "held-project", 1234)
    assert await _acquire(dsn, timeout=5) is True
    assert path.exists()
    assert await _acquire(dsn, timeout=5) is True


async def test_two_state_directories_do_not_contend(tmp_path):
    """The honest limitation, asserted so it cannot be quietly lost.

    A Postgres advisory lock is global to a DATABASE: every process pointed
    at it contends for one claim on a project name. This is global to a
    STATE DIRECTORY. Two checkouts of one repository with their own
    .3d-agent directories will both run, which is correct for the CLI's
    one-checkout-per-directory shape and is NOT what the server's lock
    promises.
    """
    first = f"sqlite:///{tmp_path}/one/state.db"
    second = f"sqlite:///{tmp_path}/two/state.db"
    holder = _holder(first)
    try:
        assert await _acquire(second, timeout=5) is True
    finally:
        holder.stdin.write("go\n")
        holder.stdin.flush()
        holder.wait(timeout=10)


async def test_project_lock_dispatches_to_the_file_lock_for_a_local_dsn(dsn, monkeypatch):
    """The whole point of the branch: a sqlite DSN must not fall through to
    the in-process lock, which would look like it worked and give a second
    process its own."""
    monkeypatch.setattr(graph, "_project_locks", {})

    async def refuse(*a, **k):
        raise AssertionError("a local DSN must never open a Postgres connection")

    monkeypatch.setattr(graph, "_connect", refuse)
    async with graph.project_lock("held-project", dsn):
        assert file_lock.lock_path(dsn, "held-project", graph.advisory_key("held-project")).exists()


async def test_a_platform_without_flock_refuses_by_name(dsn, monkeypatch):
    """No Windows half is shipped. An untested byte-range lock that returns
    instead of blocking hands two processes one worktree, which is worse
    than a refusal that names the platform and says what to do."""
    monkeypatch.setattr(file_lock, "fcntl", None)
    with pytest.raises(NotImplementedError) as e:
        async with file_lock.hold(dsn, "held-project", 1234):
            pass
    assert "flock" in str(e.value) and sys.platform in str(e.value)
