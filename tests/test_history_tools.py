"""The two tools a seat actually gets: what they cost, and what they refuse.

The economy is the point of the pair. An episode is thousands of tokens, so
returning eight of them whole would spend ~25,000 tokens before the model
has decided any one of them is relevant. `search_history` returns a digest
of the same eight for under a thousand, and `read_history` opens the one
that was worth opening. The budget test below is therefore not a style
check: if the digest grows, the tool stops being affordable to ask on a
hunch, which is the only way it gets used at all.

The other half is the access gate. A task may search exactly the projects
its creator could read, through agent/tools/reference_tools.py's own check
-- and a ref is a name, not a capability, so it is re-checked on the way
back in.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent import episode_recall as er
from agent import history_index as hi
from agent.tools import history_tools as ht

PROJECTS = {"demo": {"sandbox": "/tmp/demo"}, "other": {"sandbox": "/tmp/other"},
            "third": {"sandbox": "/tmp/third"}}


class FakeIndex:
    """An index that answers from canned rows and records what it was asked."""

    def __init__(self, hits=(), record=None):
        self.hits = list(hits)
        self.record = record
        self.searches: list[dict] = []
        self.fetched: list[tuple] = []

    async def search(self, query, *, repos, corpora=hi.CORPORA, since=None, limit=8):
        self.searches.append({"query": query, "repos": list(repos),
                              "corpora": tuple(corpora), "since": since, "limit": limit})
        return [h for h in self.hits if h.repo in repos][:limit]

    async def fetch(self, corpus, repo, item_key):
        self.fetched.append((corpus, repo, item_key))
        if self.record is None or self.record.get("repo") != repo:
            return None
        return self.record


def _hit(key="/episodes/2026-07-02T11:04:18Z-3f1c9ad2.json", *, repo="demo",
         corpus=hi.CORPUS_EPISODE, score=0.09, live=True, outcome="escalated",
         label="escalated Fix the merge so the branch lands",
         snippet="…hint: <Diverging> <branches> can't be <fast-forwarded>, you need to…"):
    return hi.Hit(corpus=corpus, repo=repo, item_key=key,
                  occurred_at=datetime(2026, 7, 2, 11, 4, 18, tzinfo=UTC),
                  label=label, snippet=snippet, outcome=outcome, task_id="t1",
                  source_live=live, score=score, stage="precise")


def _record(*, repo="demo", text="the goal, at length", err="the merge would not fast-forward",
            live=True):
    return {"corpus": "episode", "repo": repo, "item_key": "/e1.json",
            "occurred_at": datetime(2026, 7, 2, 11, 4, 18, tzinfo=UTC), "task_id": "t1",
            "session_id": None, "outcome": "escalated", "label": "escalated Fix the merge",
            "err": err, "text": text, "chunks": 1, "source_live": live}


@pytest.fixture
def tools(monkeypatch, tmp_path):
    """Both tools, over a fake index, with telemetry pointed at tmp_path.

    Installed through history_index.install so the leg registry is exercised
    the way the server exercises it, rather than stubbed past.
    """
    monkeypatch.setattr(ht, "PROJECTS", PROJECTS)
    monkeypatch.setattr("agent.tools.reference_tools.PROJECTS", PROJECTS)
    monkeypatch.setattr(er, "LOG_PATH", tmp_path / "retrieval_events.jsonl")

    def build(hits=(), record=None, readable=("demo", "other"), own="demo", **kw):
        index = FakeIndex(hits, record)
        hi.install(index)
        made = ht.make_history_tools(own, list(readable) if readable is not None else None,
                                     None, **kw)
        return index, {t.name: t for t in made}

    yield build
    hi.install(None)


async def _search(tool, **kw):
    return await tool.ainvoke(kw)


# --- the budget -----------------------------------------------------------

def _tokens(text: str) -> float:
    """Characters over four -- the ordinary English approximation, and the
    one that makes this assertion readable without pinning a tokenizer this
    repo does not otherwise depend on."""
    return len(text) / 4


async def test_eight_hits_come_back_as_a_digest_a_seat_can_afford(tools):
    """Eight whole episodes are ~25,000 tokens. The whole reason there are
    two tools is that this number stays small enough to ask on a hunch."""
    hits = [_hit(f"/episodes/2026-07-0{n}T11:04:18Z-3f1c9ad2.json", score=0.5 - n / 20)
            for n in range(1, 9)]
    _, t = tools(hits)

    out = await _search(t["search_history"], query="fast-forward merge failed")

    assert out.count("\nref=") == 0 and out.count("    ref=") == 8
    assert _tokens(out) < 1000, f"the digest costs ~{_tokens(out):.0f} tokens"


async def test_one_hit_is_four_short_lines_and_not_the_record(tools):
    _, t = tools([_hit()])

    out = await _search(t["search_history"], query="merge")

    body = out.split("\n\n")[1]
    assert len(body.splitlines()) == 4
    assert "episode · 2026-07-02 · demo · escalated" in body
    assert "the goal, at length" not in out


async def test_the_footer_names_a_ref_read_history_takes_verbatim(tools):
    """A digest that ends in an example the next tool rejects is a wasted
    round trip, and the model's next move is to guess at the key shape."""
    hit = _hit()
    index, t = tools([hit], record=_record())

    out = await _search(t["search_history"], query="merge")
    ref = out.split("read_history(ref=")[1].split(")")[0].strip("'\"")

    assert ref == hit.ref
    assert "ERROR" not in await _search(t["read_history"], ref=ref)
    assert index.fetched == [("episode", "demo", hit.ref.split(":", 2)[2])]


# --- what the digest says -------------------------------------------------

async def test_a_record_the_pruner_deleted_says_so_in_the_digest(tools):
    """It is still readable, but nothing else in the system remembers it --
    a model that goes looking for the task in the dashboard finds nothing."""
    _, t = tools([_hit(live=False)])

    out = await _search(t["search_history"], query="merge")

    assert "archived" in out and "pruned" in out


async def test_the_outcome_is_printed_once_not_twice(tools):
    """The stored label leads with the outcome because that is what weight B
    should carry; the header prints it too, and both is noise per hit."""
    _, t = tools([_hit()])

    out = await _search(t["search_history"], query="merge")

    assert out.count("escalated") == 1


async def test_nothing_found_says_what_to_try_rather_than_just_no(tools):
    """A bare "no results" gets rephrased and re-run. The two things worth
    saying are what this index actually covers, and that two misses is an
    answer."""
    _, t = tools([])

    out = await _search(t["search_history"], query="a thing that never happened")

    assert "No history matched" in out
    assert "not the code" in out


# --- the gate -------------------------------------------------------------

async def test_the_default_scope_is_this_task_own_project(tools):
    index, t = tools([_hit()])
    await _search(t["search_history"], query="merge")
    assert index.searches[0]["repos"] == ["demo"]


async def test_a_star_searches_every_project_this_task_may_read(tools):
    index, t = tools([_hit(), _hit("/e2.json", repo="other")])

    out = await _search(t["search_history"], query="merge", repo="*")

    assert index.searches[0]["repos"] == ["demo", "other"]
    assert "third" not in out


async def test_a_star_never_reaches_a_project_outside_the_allow_list(tools):
    """The higher-ranked match being in an excluded project is exactly when
    a scope bug would be invisible."""
    index, t = tools([_hit("/best.json", repo="third", score=0.9), _hit(score=0.1)],
                     readable=("demo", "other"))

    out = await _search(t["search_history"], query="merge", repo="*")

    assert "third" not in index.searches[0]["repos"]
    assert "/best.json" not in out


async def test_naming_a_project_this_task_may_not_read_is_refused_by_name(tools):
    _, t = tools([_hit()], readable=("demo",))

    out = await _search(t["search_history"], query="merge", repo="other")

    assert out.startswith("ERROR:") and "may not read 'other'" in out and "['demo']" in out


async def test_a_project_that_does_not_exist_is_refused_with_the_list(tools):
    _, t = tools([_hit()])
    out = await _search(t["search_history"], query="merge", repo="nope")
    assert "unknown project 'nope'" in out


async def test_a_ref_is_a_name_and_not_a_capability(tools):
    """A ref can reach a seat through a plan, a checkpoint or a summary
    written when the task had a wider scope, so the project in it is checked
    on the way in rather than trusted because a search produced it."""
    index, t = tools([], record=_record(repo="third"), readable=("demo",))

    out = await _search(t["read_history"], ref="episode:third:/e1.json")

    assert "may not read 'third'" in out
    assert index.fetched == [], "refused before the database is asked anything"


async def test_a_task_with_no_reference_scope_still_searches_its_own_history(tools):
    """The inversion from the reference tools: there, own repo is refused;
    here it is the default and the common case."""
    index, t = tools([_hit()], readable=[])
    await _search(t["search_history"], query="merge", repo="*")
    assert index.searches[0]["repos"] == ["demo"]


def test_an_admin_planner_may_search_every_project(monkeypatch):
    """`allowed_repos` is None for an admin, which the planner passes with
    none_means_all -- the build task's empty list must not mean the same."""
    monkeypatch.setattr(ht, "PROJECTS", PROJECTS)
    assert ht.readable_projects("demo", None, none_means_all=True) == ["demo", "other", "third"]
    assert ht.readable_projects("demo", None) == ["demo"]


def test_a_project_that_has_since_been_removed_drops_out_of_the_scope(monkeypatch):
    monkeypatch.setattr(ht, "PROJECTS", PROJECTS)
    assert ht.readable_projects("demo", ["other", "gone"]) == ["demo", "other"]


# --- arguments ------------------------------------------------------------

@pytest.mark.parametrize("word,expected", [
    ("all", hi.CORPORA),
    ("episodes", (hi.CORPUS_EPISODE,)),
    ("episode", (hi.CORPUS_EPISODE,)),
    ("tasks", (hi.CORPUS_TASK,)),
    ("build_logs", (hi.CORPUS_TASK_LOG,)),
    ("task_log", (hi.CORPUS_TASK_LOG,)),
    ("LOGS", (hi.CORPUS_TASK_LOG,)),
])
async def test_the_corpus_names_a_model_reaches_for_are_accepted(tools, word, expected):
    index, t = tools([_hit()])
    await _search(t["search_history"], query="merge", corpus=word)
    assert index.searches[0]["corpora"] == expected


async def test_an_unknown_corpus_is_refused_with_the_choices(tools):
    _, t = tools([_hit()])
    out = await _search(t["search_history"], query="merge", corpus="planning")
    assert "unknown corpus" in out and "episodes" in out


async def test_a_day_floor_becomes_a_timestamp(tools):
    index, t = tools([_hit()])
    await _search(t["search_history"], query="merge", since_days=30)
    since = index.searches[0]["since"]
    assert 29 < (datetime.now(UTC) - since).days < 31


async def test_no_floor_by_default(tools):
    index, t = tools([_hit()])
    await _search(t["search_history"], query="merge")
    assert index.searches[0]["since"] is None


# --- opening one ----------------------------------------------------------

async def test_reading_one_gives_the_failure_text_above_the_rest(tools):
    _, t = tools([], record=_record())

    out = await _search(t["read_history"], ref="episode:demo:/e1.json")

    assert out.index("FAILED WITH") < out.index("the goal, at length")
    assert "escalated" in out and "2026-07-02" in out


async def test_reading_a_record_whose_store_row_is_gone_serves_the_archive(tools):
    _, t = tools([], record=_record(live=False))
    out = await _search(t["read_history"], ref="episode:demo:/e1.json")
    assert "pruned" in out and "the goal, at length" in out


async def test_a_ref_that_names_nothing_says_so(tools):
    _, t = tools([], record=None)
    out = await _search(t["read_history"], ref="episode:demo:/gone.json")
    assert out.startswith("ERROR:") and "no history record" in out


async def test_a_malformed_ref_is_refused_before_anything_is_queried(tools):
    index, t = tools([], record=_record())
    out = await _search(t["read_history"], ref="just some words")
    assert "not a history ref" in out
    assert index.fetched == []


async def test_a_ref_naming_a_corpus_that_is_not_indexed_says_which_are(tools):
    """planning_log was cut from the first version, and a model that read
    about it somewhere should be told, not given an empty result."""
    index, t = tools([], record=_record())
    out = await _search(t["read_history"], ref="planning_log:demo:/p1.json")
    assert "unknown history corpus" in out and "episode" in out
    assert index.fetched == []


async def test_a_long_record_truncates_and_the_offered_window_is_the_next_one(tools):
    """The failure this message exists for: a model whose read came back
    truncated asks for the same thing again, and again, forever."""
    text = "\n".join(f"line {n} of the transcript" for n in range(4000))
    _, t = tools([], record=_record(text=text, err=""))

    first = await _search(t["read_history"], ref="episode:demo:/e1.json")

    assert "TRUNCATED" in first and "returns this SAME text" in first
    shown = int(first.split("you have seen lines 1-")[1].split(".")[0])
    nxt = await _search(t["read_history"], ref="episode:demo:/e1.json",
                        offset=shown, limit=800)
    assert nxt.splitlines()[0].startswith(f"{shown}\t"), "the window starts where the text ran out"
    assert f"line {shown + 400} of the transcript" in nxt, \
        "and it reaches text the first read never showed"
    assert "more after this]" in nxt and "TRUNCATED" not in nxt


async def test_a_paged_window_is_never_a_fifty_line_slice(tools):
    """Small windows just loop; the reference reader learned this first."""
    text = "\n".join(f"line {n}" for n in range(4000))
    _, t = tools([], record=_record(text=text, err=""))

    out = await _search(t["read_history"], ref="episode:demo:/e1.json", offset=1, limit=50)

    assert len(out.splitlines()) > ht._READ_MIN_WINDOW


async def test_paging_past_the_end_says_how_long_it_actually_is(tools):
    _, t = tools([], record=_record(text="one line"))
    out = await _search(t["read_history"], ref="episode:demo:/e1.json", offset=9999)
    assert "no lines at offset" in out


# --- telemetry ------------------------------------------------------------

async def test_a_search_records_what_it_offered(tools, tmp_path):
    """The measurement that decides whether a second retrieval leg is worth
    building. It has to start now, because by the time the question is asked
    the four weeks have to already have happened."""
    _, t = tools([_hit()], task_id="task-7")

    await _search(t["search_history"], query="fast-forward merge failed")

    import json
    events = [json.loads(ln) for ln in (tmp_path / "retrieval_events.jsonl").read_text().splitlines()]
    assert events[0]["event"] == "query"
    assert events[0]["query"] == "fast-forward merge failed"
    assert events[0]["task_id"] == "task-7"
    assert events[0]["refs"] == [_hit().ref]


async def test_a_search_records_which_leg_found_each_hit(tools, tmp_path):
    """`legs` says what was running; this says what each one CONTRIBUTED.
    The difference is the whole question about the second leg -- a vector
    leg whose every hit full text also found has earned nothing, however
    many searches it ran in."""
    _, t = tools([_hit()])

    await _search(t["search_history"], query="fast-forward merge failed")

    import json
    events = [json.loads(ln) for ln in (tmp_path / "retrieval_events.jsonl").read_text().splitlines()]
    assert events[0]["found_by"] == {_hit().ref: ["fts"]}


async def test_a_search_that_found_nothing_is_still_recorded(tools, tmp_path):
    """A search that returns nothing IS the measurement -- recording only
    the successful ones answers the opposite question."""
    _, t = tools([])
    await _search(t["search_history"], query="never happened")
    assert (tmp_path / "retrieval_events.jsonl").read_text().count('"event": "query"') == 1


async def test_reading_a_record_records_that_it_was_actually_read(tools, tmp_path):
    _, t = tools([], record=_record(), task_id="task-7")

    await _search(t["read_history"], ref="episode:demo:/e1.json")

    import json
    events = [json.loads(ln) for ln in (tmp_path / "retrieval_events.jsonl").read_text().splitlines()]
    assert events[-1] == {**events[-1], "event": "use", "ref": "episode:demo:/e1.json",
                          "repo": "demo", "task_id": "task-7"}


async def test_a_refused_read_is_not_recorded_as_a_use(tools, tmp_path):
    _, t = tools([], record=_record(repo="third"), readable=("demo",))
    await _search(t["read_history"], ref="episode:third:/e1.json")
    log = tmp_path / "retrieval_events.jsonl"
    assert not log.exists() or '"use"' not in log.read_text()


# --- absence --------------------------------------------------------------

def test_an_installation_with_no_index_gets_no_tools_and_no_prompt(monkeypatch):
    """The capability convention: a seat is never handed a tool whose only
    outcome is reporting that the feature is not here."""
    monkeypatch.setattr(ht, "PROJECTS", PROJECTS)
    hi.install(None)
    assert ht.available() is False
    assert ht.make_history_tools("demo", ["other"], None) == []
    assert ht.guidance("demo", ["other"]) == ""


def test_the_prompt_note_says_when_to_search_not_to_search_often(monkeypatch, tools):
    """Each call is a model call and real money, and a coordinator that
    searches history at every step is the failure this wording avoids."""
    monkeypatch.setattr(ht, "PROJECTS", PROJECTS)
    tools([])
    note = ht.guidance("demo", ["other"])
    assert "WHAT PAST TASKS RAN INTO" in note
    assert "Two moments, not more" in note
    assert "When something FAILS" in note and "before committing to an approach" in note
    assert "often" not in note
    assert "other" in note, "the projects it can reach are named, not left to be guessed"


def test_the_prompt_note_does_not_offer_cross_project_search_to_a_scoped_task(monkeypatch, tools):
    monkeypatch.setattr(ht, "PROJECTS", PROJECTS)
    tools([])
    assert "repo='*'" not in ht.guidance("demo", [])


# --- what the digest has to say about itself ------------------------------

async def test_a_widened_hit_says_so(tools):
    """Hit.stage was computed, carried through the leg and into the extra
    dict, and then shown to nobody. Without it a question the system has
    never seen comes back looking exactly like one it has: on the live index
    "kubernetes helm chart canary rollout" produced eight hits about
    chart-drawing UI, printed as "8 match(es) ... best first"."""
    import dataclasses

    _, t = tools([dataclasses.replace(_hit(), stage="widened")])

    out = await _search(t["search_history"], query="kubernetes helm chart canary rollout")

    assert "widened" in out
    assert "Nothing matched every term" in out


async def test_a_precise_page_says_nothing_about_widening(tools):
    """The caveat has to be worth reading when it appears."""
    _, t = tools([_hit()])

    out = await _search(t["search_history"], query="fast-forward merge failed")

    assert "widened" not in out


async def test_the_other_corpora_of_one_task_are_named_not_given_slots(tools):
    """An episode, its near-duplicate and the task row are one piece of
    work. search() folds them; this is the digest printing what it folded."""
    import dataclasses

    _, t = tools([dataclasses.replace(_hit(), also=(hi.CORPUS_TASK, hi.CORPUS_TASK_LOG))])

    out = await _search(t["search_history"], query="merge")

    assert "episode (+task, build log)" in out


# --- a query is bounded, and a failure is not a miss ----------------------

async def test_a_query_past_the_cap_is_cut_and_the_model_is_told(tools):
    """Silently truncating leaves a model with no way to know why it got the
    wrong answer -- and cost is superlinear in query length: 1,200
    characters measured at 0.19s against 24,000 at 23.67s, on the same pool
    that answers logins."""
    index, t = tools([_hit()])

    out = await _search(t["search_history"], query="merge failed " * 4000)

    assert len(index.searches[0]["query"]) <= hi.MAX_QUERY_CHARS
    assert str(hi.MAX_QUERY_CHARS) in out


async def test_a_search_that_did_not_complete_is_not_reported_as_no_history(tools, monkeypatch):
    """Reported as "no history matched" it is indistinguishable from a
    corpus that holds nothing, and the model's correct response to the two
    is opposite: retry narrower, or stop asking."""
    class Timing(FakeIndex):
        async def search(self, query, **kw):
            raise hi.SearchTimeout("the history search took longer than 5000ms")

    _, t = tools([])
    hi.install(Timing())

    out = await _search(t["search_history"], query="anything")

    assert out.startswith("ERROR:")
    assert "did not complete" in out
    assert "No history matched" not in out
