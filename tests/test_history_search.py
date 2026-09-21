"""Reading the keyword index back: the ranking ladder and what it returns.

CI has no Postgres, so what is pinned here is everything the SQL is asked
to do rather than the SQL running: which stage runs when, what the widened
stage is allowed to widen to, that a record occupies one slot however many
chunks it matched in, and that the allow-list is BOUND and never spliced
into a statement.

The ladder is the part worth testing hardest, because both of its failure
modes are quiet. A precise-only search silently returns nothing for a
pasted traceback whose wording has drifted; an unbounded widened search
silently returns half the table, which reads as "the tool is useless"
rather than "the tool is broken" -- and nobody reports that.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import pytest

from agent import episode_recall as er
from agent import history_index as hi


class _Cursor:
    def __init__(self, plan, log):
        self._plan = plan
        self._log = log
        self._rows: list[dict] = []
        self.description = ()

    async def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self._log.append((flat, params))
        self._rows = self._plan(flat, params)

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, plan, log):
        self._plan, self._log = plan, log

    def cursor(self):
        return _Cursor(self._plan, self._log)

    async def execute(self, sql, params=None):
        cur = _Cursor(self._plan, self._log)
        await cur.execute(sql, params)
        return cur


def _index(plan):
    """An index over a connection that answers from a plan and records every
    statement it was given."""
    log: list[tuple] = []

    @contextlib.asynccontextmanager
    async def connect():
        yield _Conn(plan, log)

    index = hi.PostgresHistoryIndex(connect)
    index._schema_ready = True
    return index, log


def _row(key="/e1.json", *, corpus="episode", repo="demo", chunk=0, score=0.09,
         snippet="…<merge> failed…", label="escalated Fix the merge", outcome="escalated",
         live=True, when=None, task_id=None):
    return {
        "corpus": corpus, "repo": repo, "item_key": key, "chunk_no": chunk,
        "occurred_at": when or datetime(2026, 7, 2, tzinfo=UTC),
        # One task per record unless a test says otherwise. search() folds
        # hits that share a task id into one entry -- an episode, its
        # near-duplicate and the task row are one piece of work -- so a
        # fixture that gave every fake row the same id would be asserting
        # against the fold rather than against whatever it meant to test.
        "task_id": task_id if task_id is not None else key,
        "outcome": outcome, "label": label, "source_live": live,
        "score": score, "snippet": snippet,
    }


def _stage(sql: str) -> str:
    if sql.upper().startswith("SET "):
        return "set"
    if "websearch_to_tsquery" in sql:
        return "precise"
    if "unnest(to_tsvector" in sql:
        return "df"
    if "AS tsq" in sql:
        return "widened"
    return "other"


def _stages(log) -> list[str]:
    """The ladder's own statements, without the deadline each one is wrapped
    in -- see PostgresHistoryIndex._reading."""
    return [s for s in (_stage(sql) for sql, _ in log) if s != "set"]


def _statements(log, stage: str) -> list[tuple]:
    return [(sql, params) for sql, params in log if _stage(sql) == stage]


def _plan_from(*, precise, widened=(), df=()):
    def plan(sql, params):
        return {"precise": list(precise), "widened": list(widened),
                "df": list(df)}.get(_stage(sql), [])
    return plan


# --- which stage runs -----------------------------------------------------

async def test_a_precise_query_that_answers_is_the_whole_search():
    """The common case costs one statement. Widening a query that already
    found what was asked for can only make the page worse."""
    index, log = _index(_plan_from(precise=[_row(f"/e{n}.json") for n in range(4)]))

    hits = await index.search("fast-forward merge failed", repos=["demo"])

    assert len(hits) == 4
    assert _stages(log) == ["precise"]
    assert {h.stage for h in hits} == {"precise"}


async def test_a_thin_precise_answer_is_widened_rather_than_returned_alone():
    """One hit is as often one accidental word match as it is the answer --
    and a pasted traceback with one stale token is exactly how a real search
    lands here."""
    index, log = _index(_plan_from(
        precise=[_row("/e1.json", score=0.5)],
        df=[{"lexeme": "fast", "ndoc": 2, "total": 100.0},
            {"lexeme": "forward", "ndoc": 3, "total": 100.0}],
        widened=[_row("/e1.json", score=0.4), _row("/e2.json", score=0.3)],
    ))

    hits = await index.search("merge failed at 3f1c9ad2", repos=["demo"])

    assert _stages(log) == ["precise", "df", "widened"]
    assert [h.item_key for h in hits] == ["/e1.json", "/e2.json"]
    assert [h.stage for h in hits] == ["precise", "widened"], \
        "the record that matched every term stays above the one that matched some"


async def test_nothing_anywhere_is_not_an_error():
    index, _ = _index(_plan_from(precise=[], df=[], widened=[]))
    assert await index.search("nothing like this ever happened", repos=["demo"]) == []


# --- what the widened stage is allowed to widen to ------------------------

async def test_a_word_in_most_of_the_corpus_is_dropped_from_the_widened_query():
    """Measured on the real corpus: an unbounded OR of a realistic paste
    returned 86 of 146 episodes. The words responsible are the ones every
    build episode contains, and they are the ones this drops."""
    index, log = _index(_plan_from(
        precise=[],
        df=[{"lexeme": "task", "ndoc": 90, "total": 100.0},
            {"lexeme": "fast-forward", "ndoc": 4, "total": 100.0},
            {"lexeme": "merge", "ndoc": 9, "total": 100.0}],
        widened=[_row()],
    ))

    await index.search("the task could not fast-forward the merge", repos=["demo"])

    widened = next(params for sql, params in _statements(log, "widened"))
    assert widened[0] == "'fast-forward' | 'merge'"


async def test_a_query_made_entirely_of_common_words_still_asks_something():
    """Dropping every term would turn a real question into an empty query,
    which reads as "no history" rather than "nothing distinctive to go on"."""
    index, log = _index(_plan_from(
        precise=[],
        df=[{"lexeme": "test", "ndoc": 80, "total": 100.0},
            {"lexeme": "fail", "ndoc": 75, "total": 100.0}],
        widened=[_row()],
    ))

    await index.search("tests failing", repos=["demo"])

    widened = next(params for sql, params in _statements(log, "widened"))
    assert widened[0] == "'test' | 'fail'"


async def test_a_lexeme_with_a_quote_in_it_cannot_break_the_tsquery():
    """Lexemes off this corpus are paths, shas and version strings, not
    identifiers."""
    index, log = _index(_plan_from(
        precise=[], df=[{"lexeme": "it's", "ndoc": 1, "total": 100.0}], widened=[_row()]))

    await index.search("it's broken", repos=["demo"])

    widened = next(params for sql, params in _statements(log, "widened"))
    assert widened[0] == "'it''s'"


async def test_the_widened_tail_is_cut_at_a_fraction_of_the_best_score():
    """An OR matches everything that shares one word with the query. Without
    the cut, the bottom of every widened page is noise that looks exactly
    like a result."""
    index, _ = _index(_plan_from(
        precise=[],
        df=[{"lexeme": "merge", "ndoc": 4, "total": 100.0}],
        widened=[_row("/keep1.json", score=0.40), _row("/keep2.json", score=0.12),
                 _row("/drop.json", score=0.05)],
    ))

    hits = await index.search("merge", repos=["demo"])

    assert [h.item_key for h in hits] == ["/keep1.json", "/keep2.json"]


# --- one record, one slot -------------------------------------------------

async def test_a_record_that_matched_in_four_chunks_takes_one_slot():
    """A 2000-entry build transcript indexes into several chunks. Letting it
    take four slots of an eight-slot page pushes out four other records, and
    the reader has one thing to go and read either way.

    Asserted against the SQL because that is where it happens now. It used
    to be done in Python over an over-fetch of limit * 4 CHUNKS, and on the
    live index one transcript held 17 of the 32 chunks fetched for "failed"
    -- so a page of eight came back with five records out of the 95 that
    matched, and the tool printed "5 match(es)" as though that were the
    answer.
    """
    index, log = _index(_plan_from(precise=[_row()]))

    await index.search("anything", repos=["demo"], limit=8)

    sql, params = _statements(log, "precise")[0]
    assert "row_number() OVER (PARTITION BY corpus, repo, item_key" in sql
    assert "WHERE rn = 1" in sql
    assert 8 * hi._FOLD_FETCH in params, "the page is cut in SQL, not after the fact"


async def test_the_chunk_that_ranked_is_the_chunk_that_is_shown():
    """The objection to DISTINCT ON -- it must order by the key before the
    score, and so hands back the wrong chunk's snippet -- is what the
    ORDER BY inside the window answers."""
    index, log = _index(_plan_from(precise=[_row()]))

    await index.search("anything", repos=["demo"])

    sql, _ = _statements(log, "precise")[0]
    assert "ORDER BY score DESC, chunk_no) AS rn" in sql
    # ...and the snippet is projected onto the already-limited page, not
    # computed for every row the sort had to look at.
    assert sql.index("LIMIT %s") < sql.index("ts_headline")


async def test_a_page_of_eight_is_eight_pieces_of_work():
    """An episode, the near-duplicate episode written seventy seconds later
    and the task row are three records describing one task. On the live
    index a page of eight resolved to three distinct task ids, so five
    slots said nothing the reader had not been told."""
    index, _ = _index(_plan_from(precise=[
        _row("/e1.json", score=0.30, task_id="t1", snippet="best"),
        _row("/e2.json", score=0.20, task_id="t1"),
        _row("t1", corpus="task", score=0.10, task_id="t1"),
        _row("/e9.json", score=0.05, task_id="t9"),
    ]))

    hits = await index.search("anything", repos=["demo"])

    assert [h.item_key for h in hits] == ["/e1.json", "/e9.json"]
    assert hits[0].snippet == "best", "the best-ranked record of the group is the one kept"
    assert hits[0].also == ("task",), "the other corpora are named, not dropped"


async def test_a_hit_with_no_task_id_is_its_own_piece_of_work():
    """Folding on a missing id would make every unattributed record one
    entry -- the opposite of the point."""
    index, _ = _index(_plan_from(precise=[
        _row("/a.json", score=0.3, task_id=None), _row("/b.json", score=0.2, task_id=None)]))

    hits = await index.search("anything", repos=["demo"])

    assert [h.item_key for h in hits] == ["/a.json", "/b.json"]


async def test_the_page_never_exceeds_the_limit_it_was_given():
    index, _ = _index(_plan_from(precise=[_row(f"/e{n}.json") for n in range(30)]))
    assert len(await index.search("anything", repos=["demo"], limit=3)) == 3


async def test_a_limit_beyond_the_cap_is_capped():
    """Twenty digest entries is already most of a page of context spent on
    records the model has not decided to read."""
    index, log = _index(_plan_from(precise=[_row(f"/e{n}.json") for n in range(200)]))

    hits = await index.search("anything", repos=["demo"], limit=500)

    assert len(hits) == hi.MAX_LIMIT


# --- the allow-list reaches SQL as data -----------------------------------

async def test_the_projects_a_search_may_read_are_bound_never_spliced():
    index, log = _index(_plan_from(precise=[_row()]))

    await index.search("x", repos=["demo", "other"], corpora=("episode",))

    sql, params = _statements(log, "precise")[0]
    assert "demo" not in sql and "other" not in sql
    assert ["demo", "other"] in params and ["episode"] in params


async def test_an_empty_query_or_an_empty_scope_asks_the_database_nothing():
    index, log = _index(_plan_from(precise=[_row()]))
    assert await index.search("   ", repos=["demo"]) == []
    assert await index.search("x", repos=[]) == []
    assert log == []


async def test_a_query_the_parser_rejects_is_a_miss_not_an_outage():
    """websearch_to_tsquery is forgiving but to_tsquery is not, and a search
    that raises would take down the task that was merely curious."""
    def plan(sql, params):
        if _stage(sql) == "set":
            return []
        raise RuntimeError('syntax error in tsquery: "|"')

    index, _ = _index(plan)
    assert await index.search("anything", repos=["demo"]) == []


# --- opening one ----------------------------------------------------------

def _fetch_rows(*bodies, err="the merge would not fast-forward", live=True):
    return [{"corpus": "episode", "repo": "demo", "item_key": "/e1.json", "chunk_no": n,
             "occurred_at": datetime(2026, 7, 2, tzinfo=UTC), "task_id": "t1",
             "session_id": None, "outcome": "escalated", "label": "escalated Fix the merge",
             "err": err, "body": body, "source_live": live}
            for n, body in enumerate(bodies)]


async def test_a_record_is_reassembled_from_its_chunks_in_order():
    index, _ = _index(lambda sql, params: _fetch_rows("first", "second", "third"))

    record = await index.fetch("episode", "demo", "/e1.json")

    assert record["text"] == "first\nsecond\nthird"
    assert record["chunks"] == 3


async def test_an_episodes_failure_reason_is_printed_once_not_once_per_chunk():
    index, _ = _index(lambda sql, params: _fetch_rows("a", "b"))
    assert (await index.fetch("episode", "demo", "/e1.json"))["err"].count("fast-forward") == 1


async def test_a_transcripts_per_chunk_error_lines_are_not_printed_twice():
    """A build log's `err` is lifted OUT of its own body text, so printing
    it above the body would show the same lines twice."""
    rows = _fetch_rows("a", "b")
    rows[0]["err"], rows[1]["err"] = "failed here", "and here"

    index, _ = _index(lambda sql, params: rows)

    assert (await index.fetch("episode", "demo", "/e1.json"))["err"] == ""


async def test_a_record_whose_store_row_was_pruned_still_reads():
    """The property the whole table was argued for: history the pruner
    deleted is still here, and says so."""
    index, _ = _index(lambda sql, params: _fetch_rows("the goal", live=False))

    record = await index.fetch("episode", "demo", "/e1.json")

    assert record["source_live"] is False
    assert record["text"] == "the goal"


async def test_a_ref_that_names_nothing_fetches_nothing():
    index, _ = _index(lambda sql, params: [])
    assert await index.fetch("episode", "demo", "/gone.json") is None


# --- refs -----------------------------------------------------------------

def test_a_ref_splits_from_the_left_because_only_the_key_holds_colons():
    """An episode key carries an ISO timestamp, so splitting on every colon
    would put half of one in the repo."""
    assert hi.parse_ref("episode:demo:/episodes/2026-07-02T11:04:18Z-3f1c.json") == (
        "episode", "demo", "/episodes/2026-07-02T11:04:18Z-3f1c.json")


@pytest.mark.parametrize("bad", ["", "episode", "episode:demo", "episode::/e.json", ":::"])
def test_a_malformed_ref_is_refused_rather_than_queried_as_empty_strings(bad):
    with pytest.raises(ValueError, match="history ref"):
        hi.parse_ref(bad)


def test_a_hit_names_itself_the_way_a_reader_quotes_it_back():
    hit = hi.Hit(corpus="episode", repo="demo", item_key="/e1.json",
                 occurred_at=datetime(2026, 7, 2, tzinfo=UTC))
    assert hit.ref == "episode:demo:/e1.json"
    assert hi.parse_ref(hit.ref) == ("episode", "demo", "/e1.json")


# --- the leg --------------------------------------------------------------

async def test_the_index_registers_a_leg_and_takes_it_away_again():
    """episode_recall.available() has to answer "can anything search history
    here", not "was this module imported" -- it is what decides whether the
    seats are built with the tools at all."""
    before = er.registered_legs()
    try:
        index, _ = _index(_plan_from(precise=[_row()]))
        hi.install(index)
        assert hi.LEG_NAME in er.registered_legs()
        assert er.available()

        hi.install(None)
        assert hi.LEG_NAME not in er.registered_legs()
    finally:
        hi.install(None)
        assert er.registered_legs() == before


async def test_one_leg_means_the_fused_order_is_that_legs_order():
    """The property the whole build order rests on: adding the second leg is
    additive, so this ranking must survive it unchanged."""
    index, _ = _index(_plan_from(precise=[
        _row("/a.json", score=0.9), _row("/b.json", score=0.5), _row("/c.json", score=0.1)]))
    try:
        hi.install(index)
        hits = await er.recall_episodes(None, "demo", "merge failed", limit=8)
        assert [h.ref for h in hits] == [
            "episode:demo:/a.json", "episode:demo:/b.json", "episode:demo:/c.json"]
        assert {h.leg for h in hits} == {hi.LEG_NAME}
    finally:
        hi.install(None)


async def test_a_leg_carries_what_the_digest_has_to_print():
    index, _ = _index(_plan_from(precise=[_row(live=False)]))
    try:
        hi.install(index)
        hit, = await er.recall_episodes(None, "demo", "merge", limit=8)
        assert hit.extra["corpus"] == "episode"
        assert hit.extra["outcome"] == "escalated"
        assert hit.extra["source_live"] is False
        assert hit.extra["occurred_at"].year == 2026
    finally:
        hi.install(None)


async def test_a_leg_given_an_argument_meant_for_another_leg_does_not_fail_the_search():
    """recall_episodes drops a leg that raises, so a leg that cannot tolerate
    an unknown keyword takes the whole search down when the next one lands."""
    index, _ = _index(_plan_from(precise=[_row()]))
    hits = await hi.fts_leg(None, "demo", "merge", limit=4, index=index,
                            min_similarity=0.7, rerank=True)
    assert len(hits) == 1


async def test_a_search_with_no_index_installed_returns_nothing_rather_than_raising():
    hi.install(None)
    assert await hi.fts_leg(None, "demo", "merge", limit=4) == []


# --- the widened stage is allowed to be a miss ----------------------------

async def test_a_question_that_collapses_to_one_common_word_is_a_miss():
    """The failure the design named as the worst, because it looks useless
    rather than broken and so nobody reports it.

    Nothing required more than ONE lexeme to survive the frequency cut, so
    on the live index "redis cluster failover latency" widened to 'latenc'
    alone and returned five hits about an unrelated health-endpoints task,
    and "how do I rotate the VAPID key" widened to 'key' alone and returned
    eight about a strategy-removal feature -- each printed as "N match(es)
    ... best first". A miss says so, and the miss message already tells the
    model what to do next.
    """
    index, log = _index(_plan_from(
        precise=[],
        df=[{"lexeme": "redi", "ndoc": 95, "total": 100.0},
            {"lexeme": "cluster", "ndoc": 92, "total": 100.0},
            {"lexeme": "failov", "ndoc": 90, "total": 100.0},
            {"lexeme": "latenc", "ndoc": 4, "total": 100.0}],
        widened=[_row()],
    ))

    assert await index.search("redis cluster failover latency", repos=["demo"]) == []
    assert _stages(log) == ["precise", "df"], "the widened stage must not have run"


async def test_a_one_word_question_is_still_allowed_to_widen():
    """The floor is about a question COLLAPSING, not about how long it was.
    One word is all the query ever had."""
    index, log = _index(_plan_from(
        precise=[], df=[{"lexeme": "merge", "ndoc": 4, "total": 100.0}], widened=[_row()]))

    hits = await index.search("merge", repos=["demo"])

    assert _stages(log) == ["precise", "df", "widened"]
    assert [h.stage for h in hits] == ["widened"]


# --- document frequency is counted in records -----------------------------

def test_document_frequency_counts_records_and_not_chunks():
    """Chunking is wildly uneven -- a handful of build transcripts hold more
    than half the rows -- so counting chunks made a term that lives in a few
    long transcripts look like a term that is everywhere. Measured on the
    live index: '/workspace' in 50.8% of chunks and 5.2% of records, 'git'
    42.4% against 15.5%. Both were over the cut and were dropped, which is
    the widened stage discarding exactly the tokens that identify a build
    transcript."""
    sql = " ".join(hi._DF_SQL.split())
    assert "count(DISTINCT (corpus, repo, item_key))::float AS total" in sql
    assert "count(DISTINCT (f.corpus, f.repo, f.item_key))" in sql
    assert "count(*)" not in sql


# --- a path is searchable the way a reader types it -----------------------

def test_a_path_is_indexed_as_every_spelling_of_itself():
    """Postgres' parser makes ONE lexeme of a whole path, so a reader who
    typed the relative form was asking for a lexeme the corpus does not
    contain even though the file is all over it. Verified against the live
    index: querying the absolute path returned 2 hits, querying the same
    path relative returned 0, and the lexeme 'config.yaml' was in 0 of 715
    chunks."""
    terms = hi._path_terms("[Errno 2] No such file: '/srv/app/services/router/config.yaml'").split()

    assert "config.yaml" in terms
    assert "router/config.yaml" in terms
    assert "services/router/config.yaml" in terms
    assert "config" in terms, "the basename without its extension"


def test_a_bare_filename_is_a_path_and_an_ordinary_word_is_not():
    assert "run_checks.py" in hi._path_terms("run_checks.py exited 1").split()
    assert hi._path_terms("it failed, e.g. on the third try") == ""


def test_path_terms_are_capped():
    """A build transcript mentions hundreds of files; past a few thousand
    characters they stop identifying the record and become the record."""
    text = " ".join(f"/very/deep/tree/number/{n}/file{n}.py" for n in range(2000))
    assert len(hi._path_terms(text)) <= hi.PATH_CHARS


async def test_the_widened_query_asks_for_the_path_the_way_it_was_typed():
    """The other half of the join: the index writes the suffixes, and this
    is the query breaking the reader's own spelling down to meet them."""
    index, log = _index(_plan_from(
        precise=[],
        df=[{"lexeme": "services/router/config.yaml", "ndoc": 2, "total": 100.0},
            {"lexeme": "missing", "ndoc": 3, "total": 100.0}],
        widened=[_row()],
    ))

    await index.search("services/router/config.yaml missing", repos=["demo"])

    widened = _statements(log, "widened")[0][1][0]
    assert "'config.yaml'" in widened
    assert "'router/config.yaml'" in widened
    assert "'services/router/config.yaml'" in widened


# --- a query is bounded, and a timeout is not a miss ----------------------

async def test_a_query_longer_than_the_cap_is_cut_before_it_reaches_sql():
    """Cost is superlinear in query length and the text is model-authored,
    so nothing upstream bounds it. Measured on the live index: 1,200
    characters 0.19s, 6,000 characters 5.66s, 24,000 characters 23.67s --
    on the server's auth pool, the same five connections that answer
    logins."""
    index, log = _index(_plan_from(precise=[_row()]))

    await index.search("merge failed " * 4000, repos=["demo"])

    sent = _statements(log, "precise")[0][1][0]
    assert len(sent) <= hi.MAX_QUERY_CHARS


async def test_every_search_statement_runs_under_a_deadline():
    """The floor under the cap: a query shape nobody predicted must cost one
    slow response, never a pool with no free connection left for a login.
    The same ceiling agent/tools/project_db.py puts on the other statement
    in this system whose text a model wrote."""
    index, log = _index(_plan_from(precise=[_row()]))

    await index.search("anything", repos=["demo"])

    sets = [sql for sql, _ in log if _stage(sql) == "set"]
    assert f"SET statement_timeout = {hi.SEARCH_TIMEOUT_MS}" in sets
    assert "SET statement_timeout = DEFAULT" in sets, "a pooled connection keeps what it is set"


async def test_a_timeout_is_reported_as_a_timeout_and_not_as_no_history():
    """_ranked treats a query the parser rejects as a miss, which is right.
    A timeout returning nothing is a search that quietly finds no history in
    a corpus that has some -- the one symptom of this subsystem nobody would
    ever report."""
    class Cancelled(RuntimeError):
        sqlstate = "57014"

    def plan(sql, params):
        if _stage(sql) == "set":
            return []
        raise Cancelled("canceling statement due to statement timeout")

    index, _ = _index(plan)
    with pytest.raises(hi.SearchTimeout):
        await index.search("anything", repos=["demo"])


async def test_a_leg_that_times_out_tells_the_caller_rather_than_returning_nothing():
    class Cancelled(RuntimeError):
        sqlstate = "57014"

    def plan(sql, params):
        if _stage(sql) == "set":
            return []
        raise Cancelled("canceling statement due to statement timeout")

    index, _ = _index(plan)
    try:
        hi.install(index)
        errors: list[str] = []
        hits = await er.recall_episodes(None, "demo", "merge", limit=8, errors=errors)
        assert hits == []
        assert errors and "took longer than" in errors[0]
    finally:
        hi.install(None)
