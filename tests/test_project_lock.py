"""One task per project, across processes.

The lock was an in-process asyncio.Lock, which made the rule true only while
exactly one process existed: a second uvicorn worker, or a restart overlapping
the old process, would each hold their own object and run two tasks against
one worktree. It is a Postgres session-level advisory lock now.

These tests drive the SQL through an injected connection rather than a live
database, so they run in CI, and pin the two properties that matter: the key
is stable across processes, and the lock is actually taken and released.
"""
import asyncio

import pytest

from agent import graph


class FakeCursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class FakeConn:
    """Records SQL. `free` decides what pg_try_advisory_lock answers."""
    opened: list["FakeConn"] = []

    def __init__(self, free=True):
        self.sql: list[tuple] = []
        self.free = free
        self.closed = False

    async def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            return FakeCursor((self.free,))
        return FakeCursor((True,))

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
    FakeConn.opened = []

    async def _connect(dsn, autocommit=True):
        conn = FakeConn(free=getattr(_connect, "free", True))
        FakeConn.opened.append(conn)
        return conn

    monkeypatch.setattr(graph, "_connect", _connect)
    monkeypatch.setattr(graph, "_project_locks", {})
    return _connect


def test_the_key_is_stable_across_processes_and_fits_int4():
    a = graph.advisory_key("storefront")
    assert a == graph.advisory_key("storefront")
    assert a != graph.advisory_key("webapp")
    for repo in ("storefront", "webapp", "a" * 200, "", "étoile"):
        key = graph.advisory_key(repo)
        assert -(2**31) <= key < 2**31, f"{repo}: {key} is not an int4"
    # Pinned, not just self-consistent: the key must survive a release as well
    # as a restart, or an upgrade mid-task would hand the same project to two
    # processes. Not Python's salted hash, which differs per process.
    assert graph.advisory_key("storefront") == 1827256500
    assert graph.advisory_key("webapp") == 490000328


def test_the_lock_is_taken_and_released_around_the_block(fake_connect):
    async def scenario():
        async with graph.project_lock("proj", "postgresql://x/y"):
            return FakeConn.opened[0].sql.copy()

    during = asyncio.run(scenario())
    conn = FakeConn.opened[0]
    assert any("pg_try_advisory_lock" in sql for sql, _ in during)
    assert not any("pg_advisory_unlock" in sql for sql, _ in during), "released before the block ended"
    assert any("pg_advisory_unlock" in sql for sql, _ in conn.sql), "never released"
    assert conn.closed, "the connection must close, which is what frees the lock after a crash"
    key = graph.advisory_key("proj")
    assert all(params == (graph._LOCK_NAMESPACE, key) for sql, params in conn.sql if "advisory" in sql)


def test_a_project_held_elsewhere_waits_rather_than_running(fake_connect, caplog):
    fake_connect.free = False        # another process holds it
    asyncio.run(_hold("proj"))
    conn = FakeConn.opened[0]
    sqls = [sql for sql, _ in conn.sql]
    assert any("pg_try_advisory_lock" in s for s in sqls)
    assert any("pg_advisory_lock" in s and "try" not in s for s in sqls), "must block, not proceed"
    assert "locked by another process" in caplog.text


async def _hold(repo: str, dsn: str = "postgresql://x/y"):
    async with graph.project_lock(repo, dsn):
        pass


def test_without_a_dsn_it_is_the_old_in_process_lock(fake_connect):
    """Callers with no database (scripts, tests) keep working, and no
    connection is opened."""
    order: list[str] = []

    async def worker(name, delay):
        async with graph.project_lock("proj"):
            order.append(f"{name}-in")
            await asyncio.sleep(delay)
            order.append(f"{name}-out")

    async def scenario():
        await asyncio.gather(worker("a", 0.02), worker("b", 0))

    asyncio.run(scenario())
    assert order == ["a-in", "a-out", "b-in", "b-out"], order
    assert FakeConn.opened == []


def test_two_tasks_in_one_process_serialize_even_with_a_dsn(fake_connect):
    """The in-process lock stays in front of the database one, so queued
    tasks do not each hold an idle connection."""
    order: list[str] = []

    async def worker(name, delay):
        async with graph.project_lock("proj", "postgresql://x/y"):
            order.append(f"{name}-in")
            await asyncio.sleep(delay)
            order.append(f"{name}-out")

    asyncio.run(_both(worker))
    assert order == ["a-in", "a-out", "b-in", "b-out"], order
    assert len(FakeConn.opened) == 2 and all(c.closed for c in FakeConn.opened)


async def _both(worker):
    await asyncio.gather(worker("a", 0.02), worker("b", 0))


def test_different_projects_do_not_block_each_other(fake_connect):
    async def scenario():
        async with graph.project_lock("one", "postgresql://x/y"):
            async with graph.project_lock("two", "postgresql://x/y"):
                return True

    assert asyncio.run(scenario()) is True
    assert len(FakeConn.opened) == 2


def test_a_local_dsn_says_the_backend_is_not_built_yet(fake_connect):
    """The lock dispatches on the DSN like the store and the checkpointer
    do, and the SQLite half of it does not exist yet. It has to say so
    rather than fall through to the in-process lock, which would look like
    it worked and give a second process its own."""
    with pytest.raises(NotImplementedError) as e:
        asyncio.run(_hold("proj", "sqlite:///.state/agent.db"))
    assert "sqlite:///.state/agent.db" in str(e.value)
    assert FakeConn.opened == [], "no connection may be opened for a DSN that is not Postgres"
