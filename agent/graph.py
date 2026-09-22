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
from psycopg import OperationalError as PgOperationalError
from psycopg_pool import AsyncConnectionPool

from agent import episode_vectors, file_lock
from agent.backends import backend_for_dsn, open_sqlite_conn, sqlite_path_from_dsn
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
async def project_lock(repo: str, dsn: str | None = None, on_wait=None):
    """Hold this project for the duration of the block.

    Without `dsn` this is the old in-process lock, which is what callers that
    have no database (tests, scripts) get. With a Postgres one, the claim is
    visible to every process pointed at the same database. With a sqlite one
    there is no database to ask, and the claim covers every process using
    that state directory -- a narrower promise, spelled out in
    agent/file_lock.py.

    `on_wait` is awaited ONCE, before blocking, if the project is already
    held. It exists because waiting here is invisible from outside: the
    caller has already told the dashboard the task is running, and it then
    sits on this lock doing nothing, looking identical to a task that is
    working. The log has said so since this lock was written ("the task just
    sits there is otherwise unexplainable"); nothing else did. A caller that
    passes this can say "queued" instead of lying.

    Called at most once per acquisition, and never when the lock was free --
    so the overwhelmingly common path writes nothing and costs nothing.
    Failures in the callback are swallowed: a status update that cannot be
    written must not stop the task it describes from running.
    """
    async def _announce_wait():
        if on_wait is None:
            return
        try:
            await on_wait()
        except Exception:  # noqa: BLE001 -- see the docstring: cosmetic, never fatal
            logger.exception("project %s: could not announce the wait", repo)

    # Checked BEFORE awaiting. Tasks queued inside one process contend here
    # rather than on the database, so this is where most waiting actually
    # happens on a single-process deployment -- and awaiting first would mean
    # the callback fired only after the wait it was meant to announce.
    local = _in_process_lock(repo)
    if local.locked():
        await _announce_wait()
    async with local:
        if not dsn:
            yield
            return
        if backend_for_dsn(dsn) == "sqlite":
            # A local installation has no advisory lock to take, and the
            # honest equivalent is an OS file lock beside the database --
            # which keys on a state directory rather than on a project name
            # every process sharing a database can see. agent/file_lock.py
            # is where that difference, and the rest of what a file lock
            # does not cover, is written down.
            async with file_lock.hold(dsn, repo, advisory_key(repo)):
                yield
            return
        key = advisory_key(repo)
        conn = await _connect(dsn, autocommit=True)
        try:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (_LOCK_NAMESPACE, key))
            row = await cur.fetchone()
            if not (row and row[0]):
                # Someone else has this project -- another process, since the
                # in-process lock above already cleared this one. Wait, the
                # way the old in-process lock made callers wait -- but say
                # so, because "the task just sits there" is otherwise
                # unexplainable.
                await _announce_wait()
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


@asynccontextmanager
async def open_checkpointer(config: Config):
    # A dispatch and nothing else. Both halves of the real work live in the
    # _open_* functions below so that adding a backend adds a function,
    # rather than growing this one an `if` per database.
    if backend_for_dsn(config.dsn) == "sqlite":
        async with _open_sqlite_checkpointer(config) as saver:
            yield saver
        return
    async with _open_postgres_checkpointer(config) as saver:
        yield saver


@asynccontextmanager
async def open_store(config: Config):
    if backend_for_dsn(config.dsn) == "sqlite":
        async with _open_sqlite_store(config) as store:
            yield store
        return
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
    # index= is None on an installation with no embedder, and None is
    # byte-for-byte today's behaviour: setup() runs the ordinary migrations
    # only, no store_vectors table is created, and no write embeds anything.
    # With one, setup() additionally applies VECTOR_MIGRATIONS -- which
    # touch no row of the existing store table, and which is the whole
    # reason this is safe to switch on against live data.
    async with AsyncPostgresStore.from_conn_string(
        config.pg_dsn, pool_config=pool_config, index=episode_vectors.index_config(config)
    ) as store:
        # AsyncPostgresStore's async setup method is also just named
        # `setup()`, not `asetup()`. Same reasoning as open_checkpointer above.
        await store.setup()
        yield store


# One file, two connections -- the store's autocommit and the saver's
# transactional -- rather than one shared between them. The two classes each
# hold their connection behind their own asyncio.Lock and neither knows the
# other exists, so sharing would put the store's statements inside the
# saver's open transaction. SQLite itself is happy with two connections to
# one file: that is what WAL and busy_timeout, set in open_sqlite_conn, are
# for.
#
# Neither class is built with from_conn_string, which sets no pragmas at all
# on either half -- in particular no busy_timeout, so the second connection
# to touch a locked file fails immediately instead of waiting out a write
# that takes milliseconds.


@asynccontextmanager
async def _open_sqlite_checkpointer(config: Config):
    # Imported inside the branch, never at module top: this package is in
    # requirements-cli.txt, which a server deliberately does not install,
    # and langgraph.checkpoint.sqlite's sibling store module imports
    # sqlite_vec at ITS top level. open_sqlite_conn has already refused with
    # the install line by the time this runs.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # noqa: PLC0415

    # autocommit=False: the saver calls conn.commit() itself around each
    # write, which is a no-op on a connection that has already committed.
    conn = await open_sqlite_conn(sqlite_path_from_dsn(config.dsn), autocommit=False)
    try:
        saver = AsyncSqliteSaver(conn)
        # Same contract as the Postgres branch: CREATE TABLE IF NOT EXISTS,
        # cheap and safe to call at every start.
        await saver.setup()
        yield saver
    finally:
        await conn.close()


@asynccontextmanager
async def _open_sqlite_store(config: Config):
    from langgraph.store.sqlite.aio import AsyncSqliteStore  # noqa: PLC0415

    # autocommit=True (isolation_level=None) is what the store's own code
    # assumes: it issues no commit of its own anywhere.
    conn = await open_sqlite_conn(sqlite_path_from_dsn(config.dsn), autocommit=True)
    try:
        # sqlite-vec needs no system package and no extension anybody has to
        # create: it is a wheel that arrives with langgraph-checkpoint-sqlite
        # and setup() loads it into this connection. The store is the one
        # place the pragmas matter -- foreign_keys=ON in open_sqlite_conn is
        # what makes store_vectors' ON DELETE CASCADE real, and without it a
        # deleted episode leaves its vector behind to match on text that is
        # gone.
        store = AsyncSqliteStore(conn, index=episode_vectors.index_config(config))
        # A versioned store_migrations table, like the Postgres store's, so
        # this replays only what is new.
        await store.setup()
        yield store
    finally:
        await conn.close()


async def read_with_retry(fn):
    """One retry for the read-only store/checkpointer lookups the frontend
    polls constantly (task list, stats, analytics, single-task fetch).

    The connection pool (agent/graph.py's open_checkpointer/open_store) is
    the actual fix for the class of failure this guards against: it
    validates a connection's health at checkout
    (`check=AsyncConnectionPool.check_connection`) before handing it to any
    caller, which is what a Postgres restart used to break silently -- with
    a single long-lived raw connection and no reconnect logic, every request
    touching it would 500 until the process was restarted.

    This retry is defense in depth on top of that, not a replacement for it:
    it covers the residual window where a connection dies after the pool's
    own checkout check but before/during the call itself (a real race, just
    a narrow one). Deliberately scoped to read-only calls only -- retrying a
    write here would mean thinking hard about idempotency per call site, and
    the writes in _stream_graph/_run_task (the live task-execution path)
    don't need it: they go through the exact same pool and get the same
    checkout validation for free.
    """
    try:
        return await fn()
    except PgOperationalError:
        await asyncio.sleep(0.25)
        return await fn()

