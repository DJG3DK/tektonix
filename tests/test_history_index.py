"""The write path of the keyword index over past tasks.

CI has no Postgres by design, so the SQL itself is proven against the live
database by hand (see scripts/backfill_history_index.py) and what is pinned
here is everything above it: what each extractor puts in weight A, that a
record is chunked on its own boundaries, and the three reconciliation rules
that decide whether history survives -- never demote from a read that
failed, never leave a stale tail chunk behind, never let an unreachable
index fail the task that was merely writing an episode.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agent import history_index as hi


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------

class FakeIndex:
    """Records what a sync asked of the index, in order."""

    def __init__(self):
        self.upserted: list[hi.HistoryRow] = []
        self.dropped: list[tuple] = []
        self.demoted: list[tuple] = []
        self.calls: list[str] = []

    async def ensure_schema_once(self):
        self.calls.append("ensure_schema")

    async def upsert(self, rows):
        self.calls.append("upsert")
        self.upserted.extend(rows)
        return len(rows)

    async def drop_tail_chunks(self, corpus, repo, counts):
        self.calls.append("drop")
        self.dropped.append((corpus, repo, dict(counts)))
        return 0

    async def demote_missing(self, corpus, repo, live_keys):
        self.calls.append("demote")
        self.demoted.append((corpus, repo, list(live_keys)))
        return 0


class Item:
    def __init__(self, key, value, created_at=None):
        self.key = key
        self.value = value
        self.created_at = created_at
        self.namespace = ()


class FakeStore:
    def __init__(self, by_namespace):
        self.by_namespace = {tuple(k): v for k, v in by_namespace.items()}

    async def asearch(self, ns, *, limit=100, offset=0, **kw):
        return self.by_namespace.get(tuple(ns), [])[offset:offset + limit]


class BrokenStore:
    async def asearch(self, ns, **kw):
        raise RuntimeError("the database went away mid-pass")


def _episode(**over):
    record = {
        "task_id": "t1",
        "goal": "# Fix the merge\n\nThe branch will not land.",
        "outcome": "escalated",
        "escalation_reason": "merge/deploy failed: Diverging branches can't be fast-forwarded",
        "review_verdict": "READY",
        "cost_usd": 1.25,
        "iteration_count": 3,
        "timestamp": "2026-07-02T11:04:18Z",
    }
    record.update(over)
    return record


def _stored_episode(**over):
    """An episode as the store actually holds it: a FileData document whose
    content is the record as a JSON string."""
    return {"content": json.dumps(_episode(**over)), "encoding": "utf-8"}


@pytest.fixture(autouse=True)
def _no_installed_index():
    """The process default is module state. Leave it as it was found."""
    before = hi.default_index()
    hi.install(None)
    yield
    hi.install(before)


# ---------------------------------------------------------------------------
# extraction: what lands in weight A
# ---------------------------------------------------------------------------

def test_an_episode_puts_its_failure_text_in_the_weighted_field():
    """The one property the whole subsystem rests on. escalation_reason is
    the only field in this system holding the actual text of a failure, and
    if it is not in `err` the index is a grep over goal prose."""
    rows = hi.episode_rows("test-repo", "/episodes/a.json", _episode())

    assert len(rows) == 1
    assert "fast-forwarded" in rows[0].err
    assert "fast-forwarded" not in rows[0].body
    assert rows[0].outcome == "escalated"
    assert rows[0].task_id == "t1"


def test_an_episode_label_names_the_outcome_and_the_goal_without_its_markdown():
    """The label is the one line a digest prints, so a leading '#' would
    render as a heading in whatever printed it next."""
    label = hi.episode_rows("test-repo", "/episodes/a.json", _episode())[0].label

    assert label == "escalated Fix the merge"


def test_an_episode_that_shipped_carries_no_error_text():
    rows = hi.episode_rows("test-repo", "/e.json", _episode(outcome="shipped", escalation_reason=None))

    assert rows[0].err == ""
    assert rows[0].outcome == "shipped"


def test_an_episode_is_dated_by_its_own_timestamp_not_by_the_row():
    when = hi.episode_rows("test-repo", "/e.json", _episode())[0].occurred_at

    assert when == datetime(2026, 7, 2, 11, 4, 18, tzinfo=UTC)


def test_a_record_with_no_usable_timestamp_sorts_oldest_never_newest():
    """now() would float every undated record to the top of a
    recency-ordered read forever."""
    when = hi.episode_rows("test-repo", "/e.json", _episode(timestamp="not a date"))[0].occurred_at

    assert when == datetime(1970, 1, 1, tzinfo=UTC)


def test_a_task_row_carries_its_status_and_its_escalation_reason():
    rows = hi.task_rows("test-repo", "abc", {
        "task_id": "abc", "goal": "Ship the thing", "status": "escalated",
        "escalation_reason": "work node failed: Model call limits exceeded",
        "route": "build", "route_reason": "it edits code", "latest_todos": "one, two",
        "created_at": "2026-09-01T00:00:00Z",
    })

    assert "Model call limits exceeded" in rows[0].err
    assert rows[0].outcome == "escalated"
    assert "it edits code" in rows[0].body


def test_a_task_log_puts_only_its_error_shaped_lines_in_the_weighted_field():
    """A transcript has no single failure field, so the error text has to be
    picked out of it -- and the ordinary lines must stay out of weight A or
    every log outranks every real escalation."""
    rows = hi.task_log_rows("test-repo", "sess", {
        "session_id": "s1",
        "entries": [
            {"summary": "calling: write_todos", "detail": "calling: write_todos",
             "timestamp": "2026-09-15T10:54:55Z"},
            {"summary": "tool result", "detail": "npm ERR! build failed with exit code 1"},
            {"summary": "retrying", "detail": "retrying"},
        ],
    })

    assert len(rows) == 1
    assert "build failed" in rows[0].err
    assert "write_todos" not in rows[0].err
    assert "write_todos" in rows[0].body
    assert rows[0].session_id == "s1"
    assert rows[0].label == "calling: write_todos"


def test_only_the_offending_lines_of_an_entry_are_weighted_not_the_whole_entry():
    """Measured on the live task logs: matching whole entries put 20% of the
    corpus in weight A. A weight nearly every row carries is not a weight,
    it is a constant -- and it would bury the handful of real escalations
    under hundreds of build transcripts."""
    rows = hi.task_log_rows("test-repo", "sess", {"entries": [{
        "summary": "ran the suite",
        "detail": "ok test_one\nok test_two\nFAILED test_three: exit code 1\nok test_four",
    }]})

    assert rows[0].err == "FAILED test_three: exit code 1"
    assert "test_two" in rows[0].body


def test_one_chunk_can_only_be_so_much_weight_a_text():
    noisy = [{"summary": "", "detail": "error on line %d" % n} for n in range(500)]
    rows = hi.task_log_rows("test-repo", "sess", {"entries": noisy})

    assert all(len(r.err) <= hi.ERR_CHARS for r in rows)


def test_a_long_task_log_is_chunked_and_every_chunk_is_independently_searchable():
    """One live row holds 2000 entries and 1.3 MB of text. A single tsvector
    over that is neither cheap nor, past 1 MB, legal."""
    entries = [{"summary": f"step {n} " + "x" * 400, "detail": ""} for n in range(60)]
    entries[55]["detail"] = "deploy failed: the remote refused the push"
    rows = hi.task_log_rows("test-repo", "sess", {"entries": entries})

    assert len(rows) > 1, "a 24,000-character log must not be one chunk"
    assert [r.chunk_no for r in rows] == list(range(len(rows)))
    assert all(len(r.body) <= hi.CHUNK_CHARS + 500 for r in rows)
    # The error belongs to the chunk it actually happened in, not to all of
    # them and not to the first.
    carrying = [r.chunk_no for r in rows if "remote refused" in r.err]
    assert len(carrying) == 1
    assert "remote refused" in rows[carrying[0]].body


def test_an_episode_error_rides_on_every_chunk_of_that_episode():
    """Unlike a transcript, an episode is ONE record with ONE reason it
    failed: a hit on its third chunk still has to be able to say why."""
    rows = hi.episode_rows("test-repo", "/e.json", _episode(goal="line\n" * 4000))

    assert len(rows) > 1
    assert all("fast-forwarded" in r.err for r in rows)


def test_a_nul_byte_in_stored_text_never_reaches_the_database():
    """Not a search-quality question: psycopg raises on a NUL, and one can
    arrive legitimately from a JSON escape in stored content."""
    rows = hi.episode_rows("test-repo", "/e.json", _episode(goal="before\x00after"))

    assert "\x00" not in rows[0].body
    assert "before" in rows[0].body and "after" in rows[0].body


def test_an_unparseable_episode_costs_its_own_row_and_not_the_pass():
    """None, not [], and the difference is a delete.

    sync_corpus hands a count of rows per item to drop_tail_chunks, and a
    count of zero deletes EVERY chunk that item has -- so an extractor that
    starts raising on a class of records would hard-delete the archived
    copy of each one, on the next nightly pass, silently, at the very moment
    consolidation is about to delete the store row too. An empty list has to
    mean "nothing to index here" and nothing else.
    """
    assert hi.rows_for_item(hi.CORPUS_EPISODE, "test-repo",
                            Item("/e.json", {"content": "{not json"})) is None
    assert hi.rows_for_item(hi.CORPUS_EPISODE, "test-repo", Item("/e.json", "not a dict")) == []


def test_an_episode_that_parses_to_an_empty_object_is_a_record_and_not_a_failure():
    """_episode_record already draws the line by returning None, and a
    truthiness test on the dict threw it away: an episode whose content is
    "{}" was treated exactly like content that would not parse."""
    rows = hi.rows_for_item(hi.CORPUS_EPISODE, "test-repo", Item("/e.json", {"content": "{}"}))

    assert rows is not None, "an empty record is not an extraction failure"


def test_an_episode_is_read_out_of_the_document_the_store_actually_holds():
    """StoreBackend keeps the record as a JSON string in `content`; nothing
    about the episode is visible at the top level of the stored value."""
    rows = hi.rows_for_item(hi.CORPUS_EPISODE, "test-repo", Item("/e.json", _stored_episode()))

    assert rows and "fast-forwarded" in rows[0].err


def test_the_corpora_are_read_from_the_namespaces_the_removal_path_walks():
    """One list of what a project owns. A corpus read from a namespace
    project_removal does not clear is a corpus that survives removal."""
    from agent.project_removal import namespaces

    owned = set(namespaces("test-repo").values())
    for corpus in hi.CORPORA:
        assert hi.namespaces_for(corpus, "test-repo") in owned


def test_the_planning_log_is_not_indexed():
    """Cut from v1 deliberately: the worst chunk-cost-to-value ratio of the
    four corpora and the one with no outcome field to weight."""
    assert "planning_log" not in hi.CORPORA


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------

async def test_a_sync_indexes_every_item_and_records_how_many_chunks_each_made():
    index = FakeIndex()
    store = FakeStore({
        hi.namespaces_for(hi.CORPUS_EPISODE, "test-repo"): [
            Item("/episodes/a.json", _stored_episode()),
            Item("/episodes/b.json", _stored_episode(task_id="t2")),
        ],
    })

    result = await hi.sync_corpus(index, "test-repo", store, hi.CORPUS_EPISODE)

    assert result.items == 2
    assert result.rows == 2
    assert index.dropped == [(hi.CORPUS_EPISODE, "test-repo",
                              {"/episodes/a.json": 1, "/episodes/b.json": 1})]


async def test_a_pruned_episode_is_demoted_and_never_deleted_from_the_index():
    """This is the property that stops _prune_consolidated_episodes
    destroying history: the store row goes, the index copy stays and says
    the store row is gone."""
    index = FakeIndex()
    store = FakeStore({hi.namespaces_for(hi.CORPUS_EPISODE, "test-repo"): [
        Item("/episodes/still-here.json", _stored_episode()),
    ]})

    await hi.sync_corpus(index, "test-repo", store, hi.CORPUS_EPISODE)

    assert index.demoted == [(hi.CORPUS_EPISODE, "test-repo", ["/episodes/still-here.json"])]


async def test_a_namespace_that_could_not_be_read_demotes_nothing():
    """A read that raised is not a namespace that is empty. Demoting on one
    would mark every row in the project index-only on a transient database
    hiccup -- and there is no pass that puts that back."""
    index = FakeIndex()

    result = await hi.sync_corpus(index, "test-repo", BrokenStore(), hi.CORPUS_EPISODE)

    assert result.failed == [hi.CORPUS_EPISODE]
    assert index.demoted == []
    assert index.upserted == []


async def test_a_sync_covers_every_corpus_and_reports_them_together():
    index = FakeIndex()
    store = FakeStore({
        hi.namespaces_for(hi.CORPUS_EPISODE, "test-repo"): [Item("/e.json", _stored_episode())],
        hi.namespaces_for(hi.CORPUS_TASK, "test-repo"): [
            Item("abc", {"task_id": "abc", "goal": "g", "status": "shipped"})],
        hi.namespaces_for(hi.CORPUS_TASK_LOG, "test-repo"): [
            Item("abc", {"entries": [{"summary": "a step", "detail": ""}]})],
    })

    result = await hi.sync_project(_config(), "test-repo", store, index=index)

    assert result.items == 3
    assert result.rows == 3
    assert {r.corpus for r in index.upserted} == set(hi.CORPORA)


async def test_a_sync_with_no_index_installed_does_nothing_and_says_so():
    """An installation with no index must not be an installation that
    crashes every night."""
    result = await hi.sync_project(_config(), "test-repo", FakeStore({}))

    assert result.rows == 0 and result.failed == []


async def test_a_sync_never_raises_at_its_caller():
    """The nightly job's real work is memory. A search index that cannot be
    written must not take that down with it."""
    class Exploding(FakeIndex):
        async def ensure_schema_once(self):
            raise RuntimeError("no database")

    result = await hi.sync_project(_config(), "test-repo", FakeStore({}), index=Exploding())

    assert result.failed == ["test-repo"]


# ---------------------------------------------------------------------------
# the episode write hook
# ---------------------------------------------------------------------------

async def test_indexing_an_episode_with_no_index_installed_is_a_no_op():
    assert await hi.index_episode(_config(), "test-repo", "/e.json", _episode()) == 0


async def test_an_unreachable_index_never_fails_the_task_that_shipped():
    class Exploding(FakeIndex):
        async def upsert(self, rows):
            raise RuntimeError("connection refused")

    hi.install(Exploding())

    assert await hi.index_episode(_config(), "test-repo", "/e.json", _episode()) == 0


async def test_an_episode_write_reaches_the_index():
    """The wire itself: agent/episodes.py is the only episode writer, so if
    it does not index, nothing indexes at write time."""
    from agent import episodes
    from tests.test_episodes import FakeStore as EpisodeStore

    index = FakeIndex()
    hi.install(index)

    await episodes.write_episode(EpisodeStore(), _config(), "test-repo", _episode())

    assert len(index.upserted) == 1
    assert index.upserted[0].corpus == hi.CORPUS_EPISODE
    assert index.upserted[0].repo == "test-repo"


# ---------------------------------------------------------------------------
# opening
# ---------------------------------------------------------------------------

def test_there_is_no_index_on_a_backend_that_has_none_yet():
    """Absent, not broken. The SQLite half is FTS5 and lands with the SQLite
    backend itself; until then a local installation gets no index rather
    than a half-built one."""
    assert hi.open_index(_config(dsn="sqlite:///state.db")) is None


def test_opening_an_index_makes_no_connection():
    """Importing this module and building an index has to cost nothing --
    scripts/doctor.py and the test suite both do it on boxes with no
    reachable database."""
    assert hi.open_index(_config()) is not None


async def test_an_index_that_cannot_be_migrated_is_not_installed():
    """Half-installed is the dangerous state: a default index whose table
    does not exist would fail on every episode write forever."""
    assert await hi.install_for(_config(dsn="sqlite:///state.db")) is None
    assert hi.default_index() is None


# ---------------------------------------------------------------------------
# what actually goes down the wire
# ---------------------------------------------------------------------------

class FakeCursor:
    rowcount = 0
    description = ()

    def __init__(self, log):
        self.log = log

    async def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    async def executemany(self, sql, params):
        self.log.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return FakeCursor(self.log)

    async def execute(self, sql, params=None):
        return await self._one(sql, params)

    async def _one(self, sql, params):
        cursor = FakeCursor(self.log)
        await cursor.execute(sql, params)
        return cursor


def _fake_index():
    """An index over a connection that records statements instead of running
    them. CI has no Postgres, and the parameter ORDER of the upsert is
    exactly the kind of thing that silently swaps two text columns and is
    only noticed as bad search results months later."""
    import contextlib

    log: list[tuple] = []

    @contextlib.asynccontextmanager
    async def connect():
        yield FakeConn(log)

    return hi.PostgresHistoryIndex(connect), log


async def test_the_upsert_binds_the_columns_in_the_order_it_names_them():
    index, log = _fake_index()
    row = hi.HistoryRow(
        corpus="episode", repo="test-repo", item_key="/e.json", chunk_no=2,
        occurred_at=datetime(2026, 7, 2, tzinfo=UTC), task_id="t1", session_id="s1",
        outcome="escalated", label="the label", err="the error", body="the body")

    await index.upsert([row])

    sql, params = log[-1]
    assert params == [("episode", "test-repo", "/e.json", 2, row.occurred_at, "t1", "s1",
                       "escalated", "the label", "the error", "the body", row.paths, True)]
    named = sql.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
    assert named == ["corpus", "repo", "item_key", "chunk_no", "occurred_at", "task_id",
                     "session_id", "outcome", "label", "err", "body", "paths", "source_live",
                     "indexed_at"]


async def test_re_indexing_an_unchanged_row_writes_nothing():
    """The idempotence claim, as SQL: the conflict clause has to compare
    every column it would overwrite, or a re-run rewrites the whole table
    and churns the GIN index for nothing."""
    index, log = _fake_index()

    await index.upsert([hi.HistoryRow("episode", "test-repo", "/e.json", 0,
                                      datetime(2026, 7, 2, tzinfo=UTC))])

    sql = log[-1][0]
    for column in ("occurred_at", "task_id", "session_id", "outcome",
                   "label", "err", "body", "source_live"):
        assert f"{hi.TABLE}.{column} IS DISTINCT FROM EXCLUDED.{column}" in sql, column


async def test_a_restored_row_keeps_the_flag_saying_its_store_record_is_gone():
    """A demoted row is the only copy of a pruned episode. Restoring it as
    live would claim a store record that is not there."""
    index, log = _fake_index()

    await index.restore_project("renamed", [{
        "corpus": "episode", "repo": "was-called-something-else", "item_key": "/e.json",
        "chunk_no": 0, "occurred_at": "2026-07-02T11:04:18Z", "label": "l",
        "err": "e", "body": "b", "source_live": False,
    }])

    (bound,) = log[-1][1]
    assert bound[1] == "renamed", "a restore writes under the CURRENT project name"
    assert bound[-1] is False


async def test_a_migration_is_applied_once_and_recorded():
    index, log = _fake_index()

    await index.ensure_schema()

    statements = [sql for sql, _ in log]
    assert any(f"CREATE TABLE IF NOT EXISTS {hi.MIGRATIONS_TABLE}" in s for s in statements)
    assert sum(1 for s in statements if "ON CONFLICT DO NOTHING" in s) == len(hi.MIGRATIONS)
    # Nothing here may touch langgraph's own tables: its setup() runs on the
    # same database on every start and must stay correct.
    joined = " ".join(statements)
    for theirs in ("store_migrations", "checkpoint_migrations", "vector_migrations"):
        assert theirs not in joined
    assert " store " not in joined and "ALTER TABLE store " not in joined


async def test_the_migration_is_not_re_run_on_every_episode_write():
    """ensure_schema always re-reads the version counter (that is what makes
    it safe on every startup, and it is what the live database proves by
    applying nothing on a second run). ensure_schema_once is the call the
    per-episode hot path takes, and it must not make a round trip."""
    index, log = _fake_index()
    await index.ensure_schema()
    before = len(log)
    await index.ensure_schema_once()
    assert len(log) == before


# ---------------------------------------------------------------------------
# the wires that are easy to forget
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "agent/server.py",
    "scripts/run_consolidation.py",
    "scripts/backfill_history_index.py",
])
def test_every_process_that_writes_history_installs_an_index(path):
    """The index is never opened on demand -- a process that does not ask
    for one indexes nothing, silently, forever.

    That is the deliberate trade: no module reaches for the database behind
    a caller's back (the suite's own promise is that it connects to
    nothing), and the cost is three call sites that have to exist. This is
    what says they still do.
    """
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent / path).read_text()
    assert "history_index.install_for(" in source, (
        f"{path} writes history and never installs an index -- it will index nothing")


def test_the_doctor_can_say_whether_this_installation_has_an_index():
    """'The feature did nothing' and 'the feature is not installed' look
    identical from the dashboard; one doctor line is the whole difference."""
    from agent.capabilities import CAPABILITIES

    capability = next((c for c in CAPABILITIES if c.name == "history index"), None)
    assert capability is not None
    assert capability.hint, "an operator told 'not available' needs to be told what to do"
    # Never raises, whatever the database is doing -- doctor.py is what you
    # run when the installation is already broken.
    assert capability.available() in (True, False)


def _config(dsn: str | None = None):
    from agent.config import load_config

    config = load_config()
    if dsn is None:
        return config
    import dataclasses

    return dataclasses.replace(config, dsn=dsn)


# ---------------------------------------------------------------------------
# an extraction that fails must never be read as an extraction of nothing
# ---------------------------------------------------------------------------

async def test_a_record_that_will_not_extract_keeps_the_chunks_it_already_has():
    """The hole beside the demotion path.

    sync_corpus wrote counts[key] = len(extracted), so a failed extraction
    stored 0 and drop_tail_chunks then issued DELETE ... WHERE item_key = ?
    AND chunk_no >= 0 -- every chunk, not a tail. Proved end to end against
    a scratch schema: two episodes indexed, then a.json's stored content
    replaced with "{not json}", then a sync -> 0 rows left for that key and
    0 rows demoted. The same record simply LEAVING the store is handled
    correctly, which is what makes this the hole and not the design.

    An extractor change that raises on a class of records would destroy,
    on the next nightly pass and silently, exactly the archive this whole
    subsystem was argued for.
    """
    index = FakeIndex()
    store = FakeStore({hi.namespaces_for(hi.CORPUS_EPISODE, "test-repo"): [
        Item("/episodes/good.json", _stored_episode()),
        Item("/episodes/bad.json", {"content": "{not json"}),
    ]})

    result = await hi.sync_corpus(index, "test-repo", store, hi.CORPUS_EPISODE)

    corpus, repo, counts = index.dropped[0]
    assert "/episodes/bad.json" not in counts, (
        "a count of 0 is a DELETE of every chunk, not a drop of the tail")
    assert counts == {"/episodes/good.json": 1}
    assert "episode:/episodes/bad.json" in result.failed


async def test_a_record_that_will_not_extract_is_not_demoted_either():
    """It is still in the store. Demoting it would have the index claim the
    record is gone while the dashboard still shows it."""
    index = FakeIndex()
    store = FakeStore({hi.namespaces_for(hi.CORPUS_EPISODE, "test-repo"): [
        Item("/episodes/bad.json", {"content": "{not json"}),
    ]})

    await hi.sync_corpus(index, "test-repo", store, hi.CORPUS_EPISODE)

    assert index.demoted == [(hi.CORPUS_EPISODE, "test-repo", ["/episodes/bad.json"])]


def test_one_unreadable_record_stops_the_prune_for_the_whole_project():
    """Deliberately blunt rather than surgical.

    The record that would not extract is the one with no copy anywhere, and
    working out whether the pruner was going to reach THAT key is a second
    thing to get right in the function whose mistakes have no undo. Blocking
    costs a night of extra rows in a table that already holds thousands, and
    the nightly run now exits non-zero saying so, so it does not cost a
    night twice.
    """
    result = hi.SyncResult(failed=["episode:/episodes/bad.json"])
    assert not result.copied
    assert "/episodes/bad.json" in result.why_not_copied

    whole = hi.SyncResult()
    assert whole.copied and whole.why_not_copied == ""


# ---------------------------------------------------------------------------
# which KIND of nothing an empty sync is
# ---------------------------------------------------------------------------

async def test_a_postgres_box_with_no_index_says_the_index_is_unavailable():
    """The worse half of the prune hole, because it does not even look like
    a failure. With no index installed, sync_project returned an entirely
    clean SyncResult -- byte-identical to the one a SQLite installation
    legitimately returns -- and consolidation deleted store rows on the
    strength of it."""
    result = await hi.sync_project(_config("postgresql://x/y"), "test-repo", FakeStore({}))

    assert result.failed == []
    assert result.skipped == "unavailable"
    assert not result.copied, "nothing may be pruned behind a sync that never ran"
    assert "could not open one" in result.why_not_copied


async def test_a_sqlite_box_with_no_index_is_not_an_error_and_may_still_prune():
    """An installation that has no index BY DESIGN. Refusing to prune here
    would grow the store forever on the one backend where that matters
    most."""
    result = await hi.sync_project(_config("sqlite:///state.db"), "test-repo", FakeStore({}))

    assert result.skipped == "no-index"
    assert result.copied


async def test_merging_keeps_the_first_reason_nothing_was_copied():
    merged = hi.SyncResult().merge(hi.SyncResult(skipped="unavailable"))
    assert merged.skipped == "unavailable"


# ---------------------------------------------------------------------------
# a build log says what its task was for
# ---------------------------------------------------------------------------

def test_a_build_log_is_labelled_by_its_task_s_goal():
    """Build logs are more than half the index and the top hit for most
    queries, so their label is the line a reader most often has to choose
    from. It was the transcript's FIRST entry -- whatever tool the task
    happened to call first: "tool result: exit_code=0", "calling:
    write_todos({...". None of those names what the task was for."""
    rows = hi.task_log_rows("test-repo", "abc",
                            {"entries": [{"summary": "calling: write_todos({...})"}]},
                            None, "# Make the merge land\n\nand nothing else")

    assert rows[0].label == "Make the merge land"


def test_a_build_log_whose_task_row_is_gone_still_has_a_label():
    rows = hi.task_log_rows("test-repo", "abc",
                            {"entries": [{"summary": "calling: write_todos({...})"}]})

    assert rows[0].label == "calling: write_todos({...})"


async def test_a_sync_joins_each_transcript_to_its_task_s_goal():
    """The join is on the key: a task row and its transcript are both
    stored under the task id."""
    index = FakeIndex()
    store = FakeStore({
        hi.namespaces_for(hi.CORPUS_TASK, "test-repo"): [
            Item("abc", {"task_id": "abc", "goal": "Make the merge land", "status": "shipped"})],
        hi.namespaces_for(hi.CORPUS_TASK_LOG, "test-repo"): [
            Item("abc", {"entries": [{"summary": "calling: bash({...})"}]})],
    })

    await hi.sync_project(_config(), "test-repo", store, index=index)

    log_row = next(r for r in index.upserted if r.corpus == hi.CORPUS_TASK_LOG)
    assert log_row.label == "Make the merge land"


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def test_a_row_carries_the_searchable_spellings_of_the_paths_it_mentions():
    row = hi.HistoryRow(corpus="episode", repo="test-repo", item_key="/e.json", chunk_no=0,
                        occurred_at=datetime(2026, 7, 2, tzinfo=UTC),
                        err="No such file: '/srv/app/services/router/config.yaml'")

    assert "config.yaml" in row.paths.split()
    assert "services/router/config.yaml" in row.paths.split()


def test_a_restored_archive_row_gets_its_paths_recomputed():
    """The column is derived, and an archive written before it existed
    carries no copy of it. Recomputing is what stops a restore putting back
    a row the search can no longer reach by path."""
    row = hi.HistoryRow(corpus="episode", repo="test-repo", item_key="/e.json", chunk_no=0,
                        occurred_at=datetime(2026, 7, 2, tzinfo=UTC),
                        body="edited services/router/config.yaml")

    assert "config.yaml" in row.paths.split()


# ---------------------------------------------------------------------------
# migrating a fresh database twice at once
# ---------------------------------------------------------------------------

async def test_the_migration_takes_a_lock_rather_than_claiming_it_cannot_race():
    """CREATE TABLE IF NOT EXISTS is NOT safe against a concurrent identical
    CREATE -- it races in the catalog, not on the name. Four concurrent
    first migrations against a scratch schema returned one success and three
    UniqueViolations on pg_type_typname_nsp_index, and install_for answers
    an exception here by installing NO index: the loser of that race spends
    its whole life with no history search and no episode indexing. The
    realistic trigger is the server's lifespan and the consolidation cron
    starting together after a restart."""
    index, log = _fake_index()

    await index.ensure_schema()

    statements = [sql for sql, _ in log]
    assert any("pg_advisory_lock" in s for s in statements)
    assert any("pg_advisory_unlock" in s for s in statements)
    assert statements.index(next(s for s in statements if "pg_advisory_lock" in s)) == 0


async def test_the_lock_is_released_even_when_a_migration_fails():
    """It is usually the server's POOLED connection. A session lock carried
    back into the pool is every later borrower's problem."""
    released = []

    class Failing:
        async def execute(self, sql, params=None):
            if "pg_advisory_unlock" in sql:
                released.append(sql)
            if "CREATE TABLE" in sql:
                raise RuntimeError("catalog race")
            return self

        async def fetchone(self):
            return None

    import contextlib

    @contextlib.asynccontextmanager
    async def connect():
        yield Failing()

    with pytest.raises(RuntimeError):
        await hi.PostgresHistoryIndex(connect).ensure_schema()

    assert released, "the advisory lock was never released"


# ---------------------------------------------------------------------------
# the second delete path: a task removed from the dashboard
# ---------------------------------------------------------------------------

async def test_a_task_and_its_transcript_are_indexed_before_they_are_deleted():
    """The two corpora with NO write-time hook. An episode is indexed as
    agent/episodes.py writes it; a task row and its build transcript are
    indexed only by the nightly sync_project, so a task deleted from the
    dashboard before the next nightly run was never indexed at all -- and
    demote_missing cannot rescue it, because there is no row to demote."""
    index = FakeIndex()
    hi.install(index)

    written = await hi.index_task(
        _config(), "test-repo", "abc",
        {"task_id": "abc", "goal": "Make the merge land", "status": "shipped"},
        {"entries": [{"summary": "calling: bash({...})"}]})

    assert written
    assert {r.corpus for r in index.upserted} == {hi.CORPUS_TASK, hi.CORPUS_TASK_LOG}
    assert all(r.item_key == "abc" for r in index.upserted)


async def test_indexing_a_task_before_deleting_it_never_fails_the_deletion():
    """Same contract as episodes.write_episode's own call: a stored record
    that is not searchable is a gap the next sync closes, and that is a
    different thing from a deleted record that was never indexed."""
    class Exploding(FakeIndex):
        async def upsert(self, rows):
            raise RuntimeError("no database")

    hi.install(Exploding())

    assert await hi.index_task(_config(), "test-repo", "abc", {"goal": "g"}, None) == 0


async def test_indexing_a_task_with_no_index_installed_is_a_no_op():
    assert await hi.index_task(_config(), "test-repo", "abc", {"goal": "g"}, None) == 0
