"""Shared infra reused by the current graph (agent/outer_graph.py).
project_lock/open_checkpointer/open_store are generic (parameterized by
config, not tied to any particular state schema), and outer_graph.py
re-exports them from here rather than duplicating them.
"""

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent.backends import backend_for_dsn
from agent.config import Config

logger = logging.getLogger("tektonix")

# A pool, not a single long-lived connection: a bare connection held open for
# this process's entire lifetime (it runs for days) goes stale silently
# across any Postgres restart, and psycopg does not notice until the next
# query fails. The pool's `check` runs a liveness probe on every checkout,
# right before a caller actually uses the connection, discarding and
# replacing anything that fails it; `max_idle`/`max_lifetime` recycle
# connections proactively in the background.
#
# `langgraph.checkpoint.postgres._ainternal.Conn` accepts either a bare
# connection or a pool, so both Saver and Store take a pool as `conn`
# transparently. AsyncPostgresStore.from_conn_string supports a pool via its
# `pool_config` kwarg; AsyncPostgresSaver.from_conn_string only opens a
# single bare connection, so the checkpointer builds and owns its pool
# explicitly here instead.
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 10
_POOL_MAX_IDLE = 300      # seconds a connection may sit unused before recycling
_POOL_MAX_LIFETIME = 1800  # seconds before a connection is recycled regardless
_POOL_KWARGS = {"autocommit": True, "prepare_threshold": 0}


def _make_pool(config: Config) -> AsyncConnectionPool:
    from psycopg.rows import dict_row

    return AsyncConnectionPool(
        config.pg_dsn,
        min_size=_POOL_MIN_SIZE,
        max_size=_POOL_MAX_SIZE,
        kwargs={**_POOL_KWARGS, "row_factory": dict_row},
        check=AsyncConnectionPool.check_connection,
        max_idle=_POOL_MAX_IDLE,
        max_lifetime=_POOL_MAX_LIFETIME,
        open=False,
    )

# One task at a time per project. Two tasks against the same worktree would
# overwrite each other's uncommitted work, and the loser's diff is whatever
# survived the race.
#
# This was an in-process asyncio.Lock, which made "one task per project" true
# only while exactly one process existed. A second uvicorn worker, a restart
# that overlaps the old process, or an operator running a script against the
# same database would each hold their own lock object and happily run two
# tasks on one directory. The rule is a property of the PROJECT, so it has to
# live where every process can see it: a Postgres session-level advisory lock.
#
# Session-level, not transaction-level, on a dedicated connection: Postgres
# drops it when that connection closes, so a crashed or killed process
# releases its claim without anyone cleaning up. That is the half an
# in-process lock can never do.
_project_locks: dict[str, asyncio.Lock] = {}

# Advisory locks are a flat 64-bit namespace shared with anything else using
# this database. The two-int form keeps ours in their own corner.
_LOCK_NAMESPACE = 0x3D46          # "3D" + "46" -- this project's corner
_LOCK_WAIT_WARN_S = 5.0

# Injectable so tests can drive the SQL without a live Postgres.
_connect = AsyncConnection.connect


def advisory_key(repo: str) -> int:
    """A stable int4 for a project name. blake2b rather than hash(): Python's
    hash is salted per process, so two workers would lock different keys for
    the same repo -- which is exactly the bug this replaces."""
    return int.from_bytes(hashlib.blake2b(repo.encode(), digest_size=4).digest(), "big", signed=True)


def _in_process_lock(repo: str) -> asyncio.Lock:
    """Kept in front of the database lock: tasks queued inside one process
    wait on a local object instead of each holding an idle connection."""
    if repo not in _project_locks:
        _project_locks[repo] = asyncio.Lock()
    return _project_locks[repo]


@asynccontextmanager
async def project_lock(repo: str, dsn: str | None = None):
    """Hold this project for the duration of the block.

    Without `dsn` this is the old in-process lock, which is what callers that
    have no database (tests, scripts) get. With one, the claim is visible to
    every process pointed at the same database.
    """
    async with _in_process_lock(repo):
        if not dsn:
            yield
            return
        if backend_for_dsn(dsn) == "sqlite":
            # A local installation has no advisory lock to take, and the
            # honest equivalent is an OS file lock beside the database --
            # which keys on a state directory rather than on a project name
            # every process sharing a database can see. That difference is
            # worth writing down rather than papering over, so it lands with
            # the rest of the SQLite backend instead of here.
            raise NotImplementedError(
                "locking a project on SQLite is not built yet -- this installation is pointed at "
                f"{dsn!r}, and only Postgres can hold a project today"
            )
        key = advisory_key(repo)
        conn = await _connect(dsn, autocommit=True)
        try:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (_LOCK_NAMESPACE, key))
            row = await cur.fetchone()
            if not (row and row[0]):
                # Someone else has this project. Wait, the way the old
                # in-process lock made callers wait -- but say so, because
                # "the task just sits there" is otherwise unexplainable.
                logger.warning(
                    "project %s is locked by another process; waiting for it to finish "
                    "(advisory lock %s/%s)", repo, _LOCK_NAMESPACE, key)
                started = time.monotonic()
                await conn.execute("SELECT pg_advisory_lock(%s, %s)", (_LOCK_NAMESPACE, key))
                waited = time.monotonic() - started
                if waited > _LOCK_WAIT_WARN_S:
                    logger.warning("project %s acquired after waiting %.0fs", repo, waited)
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NAMESPACE, key))
        finally:
            # Closing would release the lock on its own; the explicit unlock
            # above is for the case where this connection is somehow reused.
            await conn.close()


# The message both SQLite branches carry until the backend exists. One
# string because an operator who reaches one of them is going to reach the
# other, and two different sentences for one missing backend reads as two
# different problems.
_SQLITE_NOT_BUILT = (
    "the SQLite backend is not built yet -- this installation is pointed at {dsn!r}, and only "
    "Postgres is implemented today"
)


@asynccontextmanager
async def open_checkpointer(config: Config):
    # A dispatch and nothing else. Both halves of the real work live in the
    # _open_* functions below so that adding a backend adds a function,
    # rather than growing this one an `if` per database.
    if backend_for_dsn(config.dsn) == "sqlite":
        raise NotImplementedError(_SQLITE_NOT_BUILT.format(dsn=config.dsn))
    async with _open_postgres_checkpointer(config) as saver:
        yield saver


@asynccontextmanager
async def open_store(config: Config):
    if backend_for_dsn(config.dsn) == "sqlite":
        raise NotImplementedError(_SQLITE_NOT_BUILT.format(dsn=config.dsn))
    async with _open_postgres_store(config) as store:
        yield store


@asynccontextmanager
async def _open_postgres_checkpointer(config: Config):
    pool = _make_pool(config)
    await pool.open(wait=True)
    try:
        saver = AsyncPostgresSaver(conn=pool)
        # setup() must be called before first use of a Postgres
        # checkpointer/store -- it creates tables and runs migrations. It
        # checks the currently applied migration version and only runs
        # newer ones, so calling it on every startup is safe and cheap.
        # Despite being on the async saver class, this method is genuinely
        # named `setup()`, not `asetup()`.
        await saver.setup()
        yield saver
    finally:
        await pool.close()


@asynccontextmanager
async def _open_postgres_store(config: Config):
    pool_config = {
        "min_size": _POOL_MIN_SIZE,
        "max_size": _POOL_MAX_SIZE,
        "max_idle": _POOL_MAX_IDLE,
        "max_lifetime": _POOL_MAX_LIFETIME,
        "check": AsyncConnectionPool.check_connection,
        "kwargs": _POOL_KWARGS,
    }
    async with AsyncPostgresStore.from_conn_string(
        config.pg_dsn, pool_config=pool_config
    ) as store:
        # AsyncPostgresStore's async setup method is also just named
        # `setup()`, not `asetup()`. Same reasoning as open_checkpointer above.
        await store.setup()
        yield store
