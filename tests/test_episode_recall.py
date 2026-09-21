"""The recall skeleton: legs register, absent legs cost nothing, and the
telemetry that decides whether a second leg is worth building gets written.

There is no leg today. That is the property most worth pinning, because the
whole build order rests on it: with one leg the fused ranking is that leg's
ranking unchanged, so adding the second one is additive and removing it
again is a revert rather than a migration.
"""

import json

import pytest

from agent import episode_recall as recall
from agent.episode_recall import EpisodeHit


@pytest.fixture(autouse=True)
def _no_legs():
    """Registration is module state. Leave it the way it was found."""
    before = dict(recall._LEGS)
    recall._LEGS.clear()
    yield
    recall._LEGS.clear()
    recall._LEGS.update(before)


def _hits(refs, leg="a"):
    return [EpisodeHit(ref=r, repo="test-repo", rank=n, leg=leg) for n, r in enumerate(refs, 1)]


async def test_with_no_legs_recall_returns_nothing_and_says_so():
    assert recall.available() is False
    assert await recall.recall_episodes(None, "test-repo", "anything") == []


async def test_one_leg_is_returned_in_that_leg_s_own_order():
    """The assertion the vector gate's reversibility depends on."""
    async def leg(store, repo, query, limit=20, **kw):
        return _hits(["c", "a", "b"])

    recall.register_leg("text", leg)
    assert recall.available() is True
    assert [h.ref for h in await recall.recall_episodes(None, "test-repo", "q")] == ["c", "a", "b"]


async def test_an_episode_two_legs_agree_on_outranks_one_either_found_alone():
    async def text(store, repo, query, limit=20, **kw):
        return _hits(["a", "shared", "b"], leg="text")

    async def vector(store, repo, query, limit=20, **kw):
        return _hits(["c", "shared", "d"], leg="vector")

    recall.register_leg("text", text)
    recall.register_leg("vector", vector)
    assert [h.ref for h in await recall.recall_episodes(None, "test-repo", "q")][0] == "shared"


async def test_a_leg_that_raises_is_dropped_rather_than_failing_the_search():
    """A broken index degrades retrieval; it must not break the task that
    was merely curious."""
    async def broken(store, repo, query, limit=20, **kw):
        raise RuntimeError("the index is gone")

    async def working(store, repo, query, limit=20, **kw):
        return _hits(["a"])

    recall.register_leg("broken", broken)
    recall.register_leg("working", working)
    assert [h.ref for h in await recall.recall_episodes(None, "test-repo", "q")] == ["a"]


async def test_registering_the_same_name_twice_does_not_vote_twice():
    async def leg(store, repo, query, limit=20, **kw):
        return _hits(["a"])

    recall.register_leg("text", leg)
    recall.register_leg("text", leg)
    assert recall.registered_legs() == ("text",)


async def test_recall_honours_the_limit():
    async def leg(store, repo, query, limit=20, **kw):
        return _hits([f"k{n}" for n in range(30)])

    recall.register_leg("text", leg)
    assert len(await recall.recall_episodes(None, "test-repo", "q", limit=4)) == 4


def test_fusion_renumbers_ranks_from_one():
    fused = recall.fuse([_hits(["a", "b"]), _hits(["b", "c"])])
    assert [h.rank for h in fused] == [1, 2, 3]


# --- telemetry -------------------------------------------------------------

def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_search_records_what_it_asked_and_what_it_offered(tmp_path):
    log = tmp_path / "retrieval.jsonl"
    recall.record_query("advisory lock timeout", "test-repo",
                        ["/episodes/a.json", "/episodes/b.json"], task_id="t1", path=log)
    entry, = _lines(log)
    assert entry["event"] == "query"
    assert entry["query"] == "advisory lock timeout"
    assert entry["refs"] == ["/episodes/a.json", "/episodes/b.json"]
    assert entry["task_id"] == "t1"


def test_only_the_head_of_the_results_is_written_down(tmp_path):
    """The question is whether there was a hit in the top few; the tail
    costs bytes and answers nothing."""
    log = tmp_path / "retrieval.jsonl"
    refs = [f"/episodes/{n}.json" for n in range(20)]
    recall.record_query("q", "test-repo", refs, path=log)
    entry, = _lines(log)
    assert len(entry["refs"]) == recall.TOP_N
    assert entry["n_hits"] == 20, "the count of everything found is still recorded"


def test_reading_an_offered_episode_is_recorded_against_it(tmp_path):
    """The two halves the gate needs: what was offered, and what was used."""
    log = tmp_path / "retrieval.jsonl"
    recall.record_query("q", "test-repo", ["/episodes/a.json"], task_id="t1", path=log)
    recall.record_use("/episodes/a.json", "test-repo", task_id="t1", path=log)
    query, use = _lines(log)
    assert use["event"] == "use"
    assert use["ref"] in query["refs"]


def test_telemetry_never_raises_when_it_cannot_write(tmp_path):
    """It must not be able to break the search it is describing."""
    unwritable = tmp_path / "a-file" / "retrieval.jsonl"
    (tmp_path / "a-file").write_text("not a directory")
    recall.record_query("q", "test-repo", [], path=unwritable)
    recall.record_use("/episodes/a.json", "test-repo", path=unwritable)
