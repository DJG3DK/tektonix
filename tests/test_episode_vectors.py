"""The semantic leg of episode recall, proved where it can be proved.

SQLite is the backend that needs nothing from the system -- sqlite-vec is a
wheel with the extension compiled in and it arrives with
langgraph-checkpoint-sqlite -- so the behaviour is exercised end to end here
against a real database file, and Postgres differs only in which index the
same config asks for. That is the opposite of the order the design assumed,
and it is better: the harder-to-reach backend stops being the only place the
feature is ever run.

The embedder in these tests is deterministic and tiny. It maps words to
CONCEPTS -- database/mongo/postgres to one dimension, health/api/endpoint to
another -- so "the API answers 200 when the database is gone" and "health
endpoint does not actually check Mongo" land on the same vector while
sharing no content word. That is the one case full text cannot make, and it
is the case this leg exists for. What the fake proves is the wiring; whether
a real model puts those two sentences together is a question for a live
query, not for a test that would then be asserting its own fixture.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from agent import embeddings, episode_recall, episode_vectors, episodes
from agent.episode_recall import EpisodeHit

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the SQLite path is POSIX-only in this version")


# --- what gets embedded ----------------------------------------------------

def _record(**over):
    record = {
        "task_id": "t1",
        "goal": "make the health endpoint fail when the database is unreachable",
        "outcome": "escalated",
        "escalation_reason": "the readiness probe never opened a connection",
        "review_verdict": "CHANGES_REQUESTED",
        "cost_usd": 0.41,
        "iteration_count": 3,
        "timestamp": "2026-09-21T10:00:00Z",
    }
    record.update(over)
    return record


def test_the_embedded_text_is_the_goal_and_why_it_failed():
    text = episode_vectors.embed_text(_record())
    assert "health endpoint" in text
    assert "the readiness probe never opened a connection" in text
    assert "escalated" in text and "CHANGES_REQUESTED" in text


def test_numbers_are_left_out_of_the_embedding():
    """cost_usd and iteration_count mean nothing in a 1536-dimension
    average; they are noise wearing a value's clothes."""
    text = episode_vectors.embed_text(_record(cost_usd=99.5, iteration_count=17))
    assert "99.5" not in text and "17" not in text


def test_an_episode_with_nothing_to_say_carries_no_embedding_key():
    """An empty string embeds to a vector equidistant from every query, so
    it would match everything a little. No key means no vector, and
    langgraph's writer skips it without a request."""
    assert episode_vectors.embed_fields(
        {"task_id": "t", "goal": "", "outcome": "", "timestamp": "x"}) == {}


def test_the_digest_is_of_the_text_that_was_embedded():
    fields = episode_vectors.embed_fields(_record())
    assert embeddings.unchanged(fields[episode_vectors.EMBED_FIELD],
                                fields[episode_vectors.DIGEST_FIELD])


# --- fusion ----------------------------------------------------------------

def _hits(leg, refs, **extra):
    return [EpisodeHit(ref=r, repo="p", rank=n, leg=leg, extra=dict(extra))
            for n, r in enumerate(refs, 1)]


def test_one_leg_alone_keeps_its_own_ordering():
    """The property the whole build order rests on, asserted rather than
    believed: turning the vector leg off must leave full text's ranking
    exactly as it was, so the decision is reversible in both directions."""
    fts = _hits("fts", ["episode:p:a", "episode:p:b", "episode:p:c", "episode:p:d"])
    assert [h.ref for h in episode_recall.fuse([fts])] == [h.ref for h in fts]


def test_disabling_the_vector_leg_restores_the_full_text_order_exactly():
    fts = _hits("fts", [f"episode:p:{c}" for c in "abcdefgh"])
    vector = _hits("vector", ["episode:p:h", "episode:p:g"])
    with_both = [h.ref for h in episode_recall.fuse([fts, vector])]
    without = [h.ref for h in episode_recall.fuse([fts])]

    assert with_both != without, "the vector leg must actually change something"
    assert without == [h.ref for h in fts]


def test_an_episode_both_legs_found_outranks_one_either_found_alone():
    fts = _hits("fts", ["episode:p:a", "episode:p:b"])
    vector = _hits("vector", ["episode:p:b", "episode:p:z"])
    fused = episode_recall.fuse([fts, vector])

    assert fused[0].ref == "episode:p:b"
    assert fused[0].extra["found_by"] == ["fts", "vector"]


def test_the_two_legs_do_not_show_one_task_twice():
    """Full text folds an episode and its task row into one hit and can
    return a `task:` ref; the vector leg only ever knows episodes. Fused on
    the ref alone, one piece of work would take two slots of the page."""
    fts = _hits("fts", ["task:p:T7"], task_id="T7")
    vector = _hits("vector", ["episode:p:/episodes/x.json"], task_id="T7")
    fused = episode_recall.fuse([fts, vector])

    assert len(fused) == 1
    assert fused[0].extra["found_by"] == ["fts", "vector"]


def test_one_leg_returning_a_task_twice_still_returns_two_hits():
    """Grouping is between legs and never inside one. A leg that shows a
    task twice has decided both are worth a slot, and folding them here
    would shorten its page -- at which point "one leg's order is unchanged"
    would stop being true exactly when a corpus has near-duplicate records,
    which this one does."""
    fts = _hits("fts", ["episode:p:a", "episode:p:b"], task_id="T7")
    assert len(episode_recall.fuse([fts])) == 2


def test_a_leg_that_found_nothing_cannot_move_the_others_top_hit():
    """The identifier queries this corpus is mostly made of. Full text puts
    the episode carrying the literal string at rank 1; before
    episode_vectors.MIN_SIMILARITY the vector leg always ranked SOMETHING
    first, and a leg that had found nothing could still demote that exact
    match to rank 2. A leg with nothing to say now returns an empty ranking,
    and an empty ranking cannot vote."""
    fts = _hits("fts", ["episode:p:exact", "episode:p:b", "episode:p:c"])
    assert [h.ref for h in episode_recall.fuse([fts, []])] == [h.ref for h in fts]


def test_one_legs_second_hit_for_a_task_does_not_eat_the_others_slot():
    """The two rules above, together, on the corpus shape this system
    actually has -- one task writing several episodes.

    Observed live before it was fixed: the fused page carried one episode at
    BOTH rank 1 and rank 3 and dropped the episode the vector leg had ranked
    first. The vector leg's rank-1 hit took the cross-leg key `task=p/T7`;
    its rank-2 hit found that key taken and fell back to its ref -- which is
    the ref full text had already grouped under `task=p/T7`. Each of the two
    tests above asserts one half of this and neither reaches the
    combination.
    """
    fts = _hits("fts", ["episode:p:a"], task_id="T7")
    vector = _hits("vector", ["episode:p:b", "episode:p:a"], task_id="T7")
    fused = episode_recall.fuse([fts, vector])
    refs = [h.ref for h in fused]

    assert refs.count("episode:p:a") == 1, "one episode must not take two slots of a page"
    assert "episode:p:b" in refs, "the hit the vector leg ranked first must not vanish"


def test_a_hit_with_no_task_id_is_its_own_piece_of_work():
    fused = episode_recall.fuse([_hits("fts", ["episode:p:a"]), _hits("vector", ["episode:p:b"])])
    assert len(fused) == 2


# --- the leg's refusals ----------------------------------------------------

class _NoIndexStore:
    index_config = None

    async def asearch(self, *a, **k):  # pragma: no cover -- must never be reached
        raise AssertionError("a store with no index must not be asked a semantic question")


async def test_a_store_with_no_index_is_not_asked():
    """asearch with no index config does not fail -- it ignores `query` and
    returns rows by updated_at DESC. Plausible garbage, silently. So the leg
    checks for itself."""
    assert await episode_vectors.vector_leg(_NoIndexStore(), "p", "anything") == []


async def test_a_search_that_excludes_episodes_skips_this_leg():
    assert await episode_vectors.vector_leg(_NoIndexStore(), "p", "x", corpora=("task",)) == []


async def test_an_empty_query_costs_nothing():
    assert await episode_vectors.vector_leg(_NoIndexStore(), "p", "   ") == []


# --- end to end, against a real SQLite database ----------------------------

# Words that mean the same thing to this fake embedder. Each group is one
# dimension, so a sentence made of one group's words lands on the same
# vector however it is spelled.
_CONCEPTS = [
    {"database", "mongo", "postgres", "db"},
    {"health", "healthy", "endpoint", "api", "readiness", "probe", "200"},
    {"merge", "rebase", "fast-forward", "branch", "git"},
    {"logo", "svg", "brand", "palette", "colour"},
]


class _Embedder:
    """Deterministic, offline, and counts its calls -- the count is what
    proves a write carrying no embed_text costs nothing."""

    def __init__(self):
        self.calls = 0
        self.texts: list[str] = []

    async def __call__(self, texts):
        self.calls += 1
        self.texts.extend(texts)
        out = []
        for text in texts:
            words = set(text.lower().replace(",", " ").replace(".", " ").split())
            vector = [float(len(words & concept)) for concept in _CONCEPTS]
            norm = sum(v * v for v in vector) ** 0.5 or 1.0
            # A tiny constant tail so an all-zero text still has a direction
            # and cosine distance stays defined.
            out.append([v / norm for v in vector] + [0.01])
        return out


def _index(embedder):
    """The real config with the real embedder swapped out.

    Built from episode_vectors.index_config rather than spelled out here,
    because the two stores disagree about whether the key is `fields` or
    `text_fields` and a fixture carrying its own copy of that answer would
    go on passing after the product got it wrong.
    """
    index = dict(_real_index_config())
    index["embed"] = embedder
    index["dims"] = len(_CONCEPTS) + 1
    return index


def _real_index_config() -> dict:
    import agent.embeddings as embeddings_module

    real = embeddings_module.available
    embeddings_module.available = lambda config=None: True
    try:
        return episode_vectors.index_config(_Config())
    finally:
        embeddings_module.available = real


def test_the_index_config_names_the_field_for_both_stores():
    """The measured trap, pinned: langgraph's Postgres store reads `fields`
    and its SQLite store reads `text_fields`, and each silently falls back
    to the whole value. A config carrying only one of them embeds every
    store write on the other backend -- including the build transcript
    rewritten every twenty seconds during a live turn -- and never says so.
    """
    from langgraph.store.postgres.base import _ensure_index_config as pg_config
    from langgraph.store.sqlite.base import _ensure_index_config as sqlite_config

    index = _real_index_config()
    for ensure in (pg_config, sqlite_config):
        _, prepared = ensure({**index, "embed": lambda texts: [[0.0] * 5 for _ in texts]})
        assert prepared["__tokenized_fields"] == [
            (episode_vectors.EMBED_FIELD, [episode_vectors.EMBED_FIELD])
        ], f"{ensure.__module__} would embed the whole value of every store write"


async def _open(tmp_path, embedder):
    from langgraph.store.sqlite.aio import AsyncSqliteStore

    from agent.backends import open_sqlite_conn

    conn = await open_sqlite_conn(tmp_path / "state.db", autocommit=True)
    store = AsyncSqliteStore(conn, index=_index(embedder))
    await store.setup()
    return store, conn


class _Config:
    """Enough of a Config for write_episode: it passes this to the history
    index, which on a non-Postgres DSN does nothing at all."""

    dsn = "sqlite:///state.db"
    embedding_dims = len(_CONCEPTS) + 1
    embedding_alias = "embedder"
    embeddings_enabled = True


async def _write(store, repo, **over):
    return await episodes.write_episode(store, _Config(), repo, _record(**over))


@pytest.fixture
def leg():
    """The leg registered for the duration of one test, then taken away --
    the registry is module state and a leak would change what every other
    test's search returns."""
    episode_recall.register_leg(episode_vectors.LEG_NAME, episode_vectors.vector_leg)
    yield
    episode_recall.unregister_leg(episode_vectors.LEG_NAME)


async def test_an_episode_is_found_by_words_it_does_not_contain(tmp_path, leg):
    """The whole case. A months-old episode about a different area, and a
    query that shares no content word with it."""
    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        await _write(store, "p", task_id="db",
                     goal="the readiness endpoint reports healthy while postgres is down",
                     escalation_reason="")
        await _write(store, "p", task_id="git",
                     goal="the rebase left the branch unable to fast-forward",
                     escalation_reason="")

        hits = await episode_recall.recall_episodes(store, "p", "api 200 mongo", limit=5)

        assert hits, "the vector leg returned nothing at all"
        assert hits[0].extra["task_id"] == "db"
        assert hits[0].extra["found_by"] == ["vector"]
        assert hits[0].ref.startswith("episode:p:/episodes/")
    finally:
        await conn.close()


async def test_a_write_carrying_no_embed_text_makes_no_embedding_call(tmp_path):
    """The lever the whole design hangs on: fields=["embed_text"] means
    every other writer in this system pays nothing, with not one line
    changed. Exercised against the real shape agent/planning_log.py flushes
    -- a document of entries, rewritten every twenty seconds during a live
    turn."""
    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        await store.aput(("task_log", "p"), "task-1",
                         {"entries": [{"text": "ran the tests"} for _ in range(200)]})
        await store.aput(("tasks", "p"), "task-1", {"goal": "ship it", "status": "done"})
        assert embedder.calls == 0
    finally:
        await conn.close()


async def test_writing_an_episode_makes_exactly_one_embedding_call(tmp_path):
    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        await _write(store, "p")
        assert embedder.calls == 1
        assert "readiness probe" in embedder.texts[0]
    finally:
        await conn.close()


async def test_an_embedding_failure_keeps_the_episode(tmp_path):
    """A task that has shipped and spent its money must not be lost because
    the embedder was unreachable. It is stored without a vector and the
    backfill picks it up."""

    async def broken(texts):
        raise embeddings.EmbeddingError("the router did not answer")

    store, conn = await _open(tmp_path, broken)
    try:
        key = await _write(store, "p")
        item = await store.aget(("episodes", "p"), key)

        assert item is not None
        assert json.loads(item.value["content"])["task_id"] == "t1"
        assert episode_vectors.EMBED_FIELD not in item.value
    finally:
        await conn.close()


async def test_the_stored_episode_still_reads_back_as_a_file(tmp_path):
    """Every reader goes through deepagents' StoreBackend, which rebuilds a
    FileData from content/encoding/created_at/modified_at. Composing the
    value by hand is what lets embed_text survive the write; it must not
    cost the shape the readers expect."""
    from deepagents.backends import StoreBackend
    from deepagents.backends.utils import file_data_to_string

    from agent.deep_agent import episodes_namespace

    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        key = await _write(store, "p")
        backend = StoreBackend(namespace=episodes_namespace("p"), store=store)
        result = await backend.aread(key)

        assert json.loads(file_data_to_string(result.file_data)) == _record()
    finally:
        await conn.close()


async def _open_unindexed(tmp_path):
    """A store opened the way every installation that has not switched the
    feature on opens it: no index config, so nothing is ever embedded."""
    from langgraph.store.sqlite.aio import AsyncSqliteStore

    from agent.backends import open_sqlite_conn

    conn = await open_sqlite_conn(tmp_path / "state.db", autocommit=True)
    store = AsyncSqliteStore(conn)
    await store.setup()
    return store, conn


async def test_an_episode_written_with_no_index_is_left_for_the_backfill(tmp_path):
    """The gap this closes was silent and permanent.

    write_episode used to stamp `embed_digest` whether or not anything had
    been embedded, and the backfill treats a matching digest as "already
    current". So every episode written on the live server -- where
    EMBEDDINGS_ENABLED is unset and the store carries no index -- reported
    as done and could never get a vector, which is exactly the gap the
    backfill exists to close.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import backfill_episode_embeddings as backfill  # noqa: PLC0415

    store, conn = await _open_unindexed(tmp_path)
    try:
        key = await _write(store, "p")
        item = await store.aget(("episodes", "p"), key)

        assert episode_vectors.EMBED_FIELD in item.value, \
            "the text the backfill will embed has to survive the write"
        assert episode_vectors.DIGEST_FIELD not in item.value, \
            "a digest with no vector behind it is a claim the backfill believes"

        items = await store.asearch(("episodes", "p"), limit=100)
        assert len(backfill._pending(items)) == 1
    finally:
        await conn.close()


async def test_a_query_with_no_semantic_match_returns_nothing(tmp_path, leg):
    """A nearest-neighbour search has no idea of "no match": it returns
    `limit` rows whatever is asked of it. Without a floor the leg filled
    every page with near-misses, and fusion -- which counts POSITIONS --
    then let a rank 1 that only meant "least far away" outvote full text's
    correct answer of nothing. Measured live before the floor: a nonsense
    query returned 0 hits from full text and 8 from the fused page, all of
    them vector-only and all irrelevant."""
    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        await _write(store, "p", goal="the readiness probe never opened a database connection")

        assert await episode_vectors.vector_leg(store, "p", "zqjxvb quarkmelon wuzzle") == []
        assert await episode_vectors.vector_leg(store, "p", "mongo health endpoint") != [], \
            "the floor must not cost the case the leg exists for"
    finally:
        await conn.close()


async def test_the_backfill_is_idempotent(tmp_path):
    """Re-putting identical text re-embeds it -- langgraph has no change
    detection -- so the digest is what stops a re-run paying for the whole
    corpus a second time."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import backfill_episode_embeddings as backfill  # noqa: PLC0415

    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        # An episode as it looked before this feature existed: no embed_text.
        record = _record(task_id="old")
        await store.aput(("episodes", "p"), "/episodes/2026-01-01T00:00:00Z-old.json",
                         {"content": json.dumps(record), "encoding": "utf-8"}, index=False)
        assert embedder.calls == 0

        items = await store.asearch(("episodes", "p"), limit=100)
        assert len(backfill._pending(items)) == 1

        await store.abatch([__import__("langgraph.store.base", fromlist=["PutOp"]).PutOp(
            ("episodes", "p"), key, value) for key, value, _ in backfill._pending(items)])
        assert embedder.calls == 1

        items = await store.asearch(("episodes", "p"), limit=100)
        assert backfill._pending(items) == [], "a second run must embed nothing"
    finally:
        await conn.close()


async def test_a_deleted_episode_takes_its_vector_with_it(tmp_path, leg):
    """store_vectors declares ON DELETE CASCADE back to store, and SQLite
    ignores that clause unless foreign_keys is on -- which is why
    agent/backends.py sets the pragma. Without it, consolidation's pruner
    leaves a vector matching text that no longer exists anywhere."""
    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        key = await _write(store, "p", goal="postgres is down and the endpoint says healthy",
                           escalation_reason="")
        assert await episode_recall.recall_episodes(store, "p", "mongo api", limit=5)

        await store.adelete(("episodes", "p"), key)
        rows = await (await conn.execute("SELECT count(*) FROM store_vectors")).fetchone()

        assert rows[0] == 0
        assert await episode_recall.recall_episodes(store, "p", "mongo api", limit=5) == []
    finally:
        await conn.close()


async def test_a_date_limited_search_does_not_drop_the_leg(tmp_path, leg):
    """The SQLite store returns a NAIVE created_at and the Postgres store an
    aware one. Compared as they come, since_days raises TypeError inside the
    leg, recall_episodes drops it, and the semantic half of the search
    disappears for exactly the queries that narrow by date -- with one
    warning line as the only trace."""
    from datetime import UTC, datetime, timedelta

    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        await _write(store, "p", goal="postgres is down and the endpoint says healthy",
                     escalation_reason="")
        errors: list[str] = []
        hits = await episode_recall.recall_episodes(
            store, "p", "mongo api", limit=5, errors=errors,
            since=datetime.now(UTC) - timedelta(days=7))

        assert errors == []
        assert hits

        errors = []
        old = await episode_recall.recall_episodes(
            store, "p", "mongo api", limit=5, errors=errors,
            since=datetime.now(UTC) + timedelta(days=1))
        assert errors == [] and old == [], "a window that excludes everything returns nothing"
    finally:
        await conn.close()


async def test_a_search_of_several_projects_embeds_the_query_once(tmp_path, leg):
    """One batch, not a loop of searches. Both stores collect the query
    texts across a batch and embed them together, so repo='*' costs one
    round trip rather than one per project -- which was most of this leg's
    latency, all of it spent asking the same question again."""
    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        for repo in ("one", "two", "three"):
            await _write(store, repo, goal="postgres is unreachable", escalation_reason="")
        before = embedder.calls

        await episode_recall.recall_episodes(
            store, "one", "mongo api", limit=5, repos=["one", "two", "three"])

        assert embedder.calls - before == 1
    finally:
        await conn.close()


async def test_a_search_of_several_projects_merges_onto_one_ranking(tmp_path, leg):
    embedder = _Embedder()
    store, conn = await _open(tmp_path, embedder)
    try:
        await _write(store, "one", task_id=str(uuid.uuid4()),
                     goal="the branch will not fast-forward", escalation_reason="")
        await _write(store, "two", task_id=str(uuid.uuid4()),
                     goal="postgres is unreachable from the readiness endpoint",
                     escalation_reason="")

        hits = await episode_recall.recall_episodes(
            store, "one", "mongo api", limit=5, repos=["one", "two"])

        assert [h.repo for h in hits][0] == "two"
        assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
    finally:
        await conn.close()
