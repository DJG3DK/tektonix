"""The live server's path through open_store and open_checkpointer,
asserted without a database.

This is the regression guard for the one thing the foundation commit was not
allowed to change. Everything else in it is new code nobody runs yet; these
two openers are on the path of every task the running server executes, and
the dispatch in front of them has to be invisible from the Postgres side.
Each test drives the real function with the DSN shape the live box has and
asserts the Postgres branch is what it reached.

project_lock got the same dispatch and is tested in tests/test_project_lock.py,
with the rest of the lock's behaviour and the fake connection that file
already has.
"""

from contextlib import asynccontextmanager

import pytest

from agent import backends, graph
from agent.config import load_config

LIVE_SHAPE = "postgresql://agent:pw@127.0.0.1:5432/agent"
LOCAL_SHAPE = "sqlite:///.state/agent.db"


def _config(dsn):
    return load_config(dsn=dsn)


async def test_open_store_reaches_the_postgres_branch_for_the_live_dsn(monkeypatch):
    reached = []

    @asynccontextmanager
    async def fake(config):
        reached.append(config.dsn)
        yield "the postgres store"

    monkeypatch.setattr(graph, "_open_postgres_store", fake)
    async with graph.open_store(_config(LIVE_SHAPE)) as store:
        assert store == "the postgres store"
    assert reached == [LIVE_SHAPE], "the dispatch must hand the config straight through, unaltered"


async def test_open_checkpointer_reaches_the_postgres_branch_for_the_live_dsn(monkeypatch):
    reached = []

    @asynccontextmanager
    async def fake(config):
        reached.append(config.dsn)
        yield "the postgres saver"

    monkeypatch.setattr(graph, "_open_postgres_checkpointer", fake)
    async with graph.open_checkpointer(_config(LIVE_SHAPE)) as saver:
        assert saver == "the postgres saver"
    assert reached == [LIVE_SHAPE]


async def test_the_postgres_store_still_opens_with_the_pooled_connection_string(monkeypatch):
    """One layer deeper: the branch itself still calls from_conn_string with
    the DSN and a pool config, which is what the comments above it promise
    and what the pool's whole reason for existing depends on."""
    seen = {}

    @asynccontextmanager
    async def fake_from_conn_string(conn_string, **kwargs):
        seen["conn_string"] = conn_string
        seen["kwargs"] = kwargs

        class _Store:
            async def setup(self):
                seen["setup"] = True

        yield _Store()

    monkeypatch.setattr(graph.AsyncPostgresStore, "from_conn_string", fake_from_conn_string)
    async with graph.open_store(_config(LIVE_SHAPE)):
        pass
    assert seen["conn_string"] == LIVE_SHAPE
    assert seen["kwargs"]["pool_config"]["max_size"] == graph._POOL_MAX_SIZE
    assert seen["setup"] is True


@pytest.mark.parametrize("opener,branch", [
    ("open_store", "_open_sqlite_store"),
    ("open_checkpointer", "_open_sqlite_checkpointer"),
])
async def test_a_local_dsn_reaches_the_sqlite_branch(opener, branch, monkeypatch):
    """The other half of the dispatch. The branch itself is exercised
    against a real file by tests/test_sqlite_store_parity.py; this asserts
    only that a local DSN gets there, with the config handed through
    unaltered."""
    reached = []

    @asynccontextmanager
    async def fake(config):
        reached.append(config.dsn)
        yield "the sqlite half"

    monkeypatch.setattr(graph, branch, fake)
    async with getattr(graph, opener)(_config(LOCAL_SHAPE)) as opened:
        assert opened == "the sqlite half"
    assert reached == [LOCAL_SHAPE]


@pytest.mark.parametrize("opener", ["open_store", "open_checkpointer"])
async def test_a_local_dsn_without_the_package_names_the_install_line(opener, monkeypatch):
    """The degradation shape the rest of the tree uses: a predicate, and a
    refusal carrying the exact command. A server never installs
    requirements-cli.txt, so this is what an AGENT_DSN typo on a server
    would produce -- a sentence, not an ImportError three layers down."""
    monkeypatch.setattr(backends, "sqlite_available", lambda: False)
    with pytest.raises(RuntimeError) as e:
        async with getattr(graph, opener)(_config(LOCAL_SHAPE)):
            pass
    assert backends.SQLITE_INSTALL_HINT in str(e.value)
