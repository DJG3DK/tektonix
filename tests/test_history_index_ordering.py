"""The most dangerous ordering in the repository.

agent/consolidation.py's `_prune_consolidated_episodes` permanently DELETEs
store rows. agent/history_index.py's `sync_project` is the only thing that
copies those rows anywhere else. If the second ever runs after the first,
every episode the pruner takes is gone -- not corrupted, not recoverable
from a later pass, gone -- and nothing crashes, nothing logs, and the first
sign is a search that finds nothing from before whenever the reorder landed.

So the order is asserted as OBSERVED CALLS, not as source lines. A
source-order test passes a refactor that moves one of the two calls into a
helper, which is exactly the shape of change that would cause this.
"""
from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

import agent.consolidation as c
from agent.config import load_config
from agent.deep_agent import EPISODES_ROUTE, episodes_namespace


class _Recorder:
    """Every call this test cares about, in the order they happened."""

    def __init__(self):
        self.calls: list[str] = []

    def stub(self, name: str, result):
        recorder = self

        async def call(*args, **kwargs):
            recorder.calls.append(name)
            return result

        return call


class _Result:
    """The consolidation agent's structured answer, without a model."""

    reasoning = "nothing new"

    def __init__(self, memory: str):
        self.updated_memory = memory


class _Agent:
    def __init__(self, memory: str):
        self._memory = memory

    async def ainvoke(self, *args, **kwargs):
        return {"structured_response": _Result(self._memory)}


@pytest.fixture
def recorder(monkeypatch):
    """A consolidation run with no model, no tools, and both dangerous calls
    replaced by recorders."""
    rec = _Recorder()
    monkeypatch.setattr(c.history_index, "sync_project",
                        rec.stub("sync_project", c.history_index.SyncResult()))
    monkeypatch.setattr(c, "_prune_consolidated_episodes", rec.stub("prune", 0))
    monkeypatch.setattr(c, "ChatOpenAI", lambda **kw: object())
    monkeypatch.setattr(c, "make_agent_tools", lambda root: ([], None))
    monkeypatch.setattr(c, "create_deep_agent", lambda **kw: _Agent("# memory\n"))
    return rec


async def _seed(store, repo, n):
    ns = episodes_namespace(repo)(None)
    for i in range(n):
        await store.aput(ns, f"{EPISODES_ROUTE}2026-01-01T00:00:{i:02d}Z-{i:04d}.json",
                         {"content": "{}"})


async def test_history_is_indexed_before_any_episode_is_pruned(recorder):
    """The whole point of the file. Swap the two calls in run_consolidation
    and this is what goes red."""
    store = InMemoryStore()
    await _seed(store, "test-repo", 3)

    await c.run_consolidation(load_config(), "test-repo", None, store)

    assert "sync_project" in recorder.calls, "history was never indexed at all"
    assert "prune" in recorder.calls, "this test no longer exercises the pruner"
    assert recorder.calls.index("sync_project") < recorder.calls.index("prune"), (
        f"episodes were pruned before they were indexed: {recorder.calls}")


async def test_the_index_is_reconciled_even_when_there_is_nothing_to_consolidate(recorder):
    """A project with no new episodes still has task rows and build
    transcripts that changed today, and this nightly pass is the only thing
    that reconciles them. An early return that skips the sync means a busy
    project whose last episode was a fortnight ago indexes nothing."""
    store = InMemoryStore()

    summary = await c.run_consolidation(load_config(), "test-repo", None, store)

    assert recorder.calls == ["sync_project"], recorder.calls
    assert "history_rows_written" in summary


async def test_the_summary_reports_what_was_indexed(recorder, monkeypatch):
    """A zero every night is how an operator finds out the index stopped
    being written, rather than finding out from a search that returns
    nothing months later."""
    written = c.history_index.SyncResult(items=4, rows=9, written=9, demoted=2)
    monkeypatch.setattr(c.history_index, "sync_project", recorder.stub("sync_project", written))
    store = InMemoryStore()
    await _seed(store, "test-repo", 2)

    summary = await c.run_consolidation(load_config(), "test-repo", None, store)

    assert summary["history_rows_written"] == 9
    assert summary["history_rows_demoted"] == 2


# ---------------------------------------------------------------------------
# the ordering was never the guarantee
# ---------------------------------------------------------------------------

async def test_nothing_is_pruned_when_the_index_sync_failed(recorder, monkeypatch):
    """The ordering was right and it was not the guarantee.

    `_prune_consolidated_episodes` ran unconditionally: run_consolidation
    read only `written` and `demoted` off the SyncResult and never looked at
    `failed`, while `sync_project` is documented to never raise and swallows
    every exception into `result.failed`. Probed against the real module
    with 210 seeded episodes and a sync that fails the way sync_project
    itself fails: calls were ['sync_failed', 'prune'], 10 episodes deleted,
    and the summary said history_rows_written: 0 with no mention of the
    failure. Skipping a prune costs one night of extra rows; running it
    costs history that exists nowhere else.
    """
    failed = c.history_index.SyncResult(failed=["test-repo"])
    monkeypatch.setattr(c.history_index, "sync_project", recorder.stub("sync_project", failed))
    store = InMemoryStore()
    await _seed(store, "test-repo", 3)

    summary = await c.run_consolidation(load_config(), "test-repo", None, store)

    assert recorder.calls == ["sync_project"], (
        f"episodes were pruned behind a sync that failed: {recorder.calls}")
    assert summary["episodes_pruned"] == 0
    assert summary["history_failed"] == ["test-repo"]
    assert summary["episodes_not_pruned"], "an operator has to be told why the store is growing"


async def test_nothing_is_pruned_when_no_index_could_be_opened(recorder, monkeypatch):
    """The worse half, because it does not look like a failure at all.

    With no index object installed -- install_for failed in the cron, or the
    server's lifespan install failed -- sync_project returned an entirely
    clean SyncResult with failed == [], byte-identical to the one a SQLite
    installation legitimately returns. A guard on `failed` alone would still
    have pruned. SyncResult carries the third state now, and this is it.
    """
    unavailable = c.history_index.SyncResult(skipped="unavailable")
    monkeypatch.setattr(c.history_index, "sync_project",
                        recorder.stub("sync_project", unavailable))
    store = InMemoryStore()
    await _seed(store, "test-repo", 3)

    summary = await c.run_consolidation(load_config(), "test-repo", None, store)

    assert recorder.calls == ["sync_project"], recorder.calls
    assert summary["episodes_pruned"] == 0


async def test_an_installation_with_no_index_by_design_still_prunes(recorder, monkeypatch):
    """A SQLite box has no index and never will. Refusing to prune there
    would grow the store forever on the backend where that matters most."""
    by_design = c.history_index.SyncResult(skipped="no-index")
    monkeypatch.setattr(c.history_index, "sync_project", recorder.stub("sync_project", by_design))
    store = InMemoryStore()
    await _seed(store, "test-repo", 3)

    await c.run_consolidation(load_config(), "test-repo", None, store)

    assert recorder.calls == ["sync_project", "prune"]


async def test_the_pruner_reads_the_whole_namespace_and_not_one_page(monkeypatch):
    """The most destructive function in the repository was hand-rolling its
    paging: `store.asearch(ns, limit=1000)`, while `all_items` was already
    imported in the file and used 190 lines above it. Past 1000 episodes
    both of its tests -- "are there more than the retention window" and
    "which are the newest EPISODE_RETENTION" -- were computed against an
    arbitrary window of an updated_at ordering.
    """
    seen: list[dict] = []
    real = c.all_items

    async def watched(store, ns, **kw):
        seen.append({"ns": ns})
        return await real(store, ns, **kw)

    monkeypatch.setattr(c, "all_items", watched)
    store = InMemoryStore()
    await _seed(store, "test-repo", 3)

    await c._prune_consolidated_episodes(store, "test-repo", "zzz")

    assert seen, "the pruner is paging by hand again"


# ---------------------------------------------------------------------------
# the pin the ordering test above cannot be
# ---------------------------------------------------------------------------
#
# The observed-call test at the top of this file does its job for the pair it
# patches, and it can see nothing else. A SECOND delete path -- the dashboard's
# delete-task route was already one, deleting a task row and its build
# transcript with no ordering at all -- would leave it green while history was
# being destroyed elsewhere. So the delete paths themselves are enumerated.

import ast  # noqa: E402
import pathlib  # noqa: E402

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "agent"

# Every store namespace the history index extracts a corpus from. A delete
# against one of these is a delete of something whose only other copy is an
# index row -- or, worse, of something never copied at all.
# "namespaces(" is project_removal's canonical list of what a project owns,
# which is all three of them at once -- the removal path deletes generically
# and so names none of them literally.
_INDEXED = ("episodes_namespace", '"episodes"', '"tasks"', '"task_log"',
            "TASK_NAMESPACE", "namespaces(")

# Each function below deletes from one of those namespaces, and each one
# says what stands between it and losing the record. A new entry here is not
# a chore: it is the moment to decide which of the two this is.
_DELETE_PATHS = {
    "consolidation.py::_prune_consolidated_episodes":
        "guarded by SyncResult.copied at its only call site, run_consolidation",
    "project_removal.py::purge":
        "takes the index and calls forget_project; the route refuses without one",
    "server.py::delete_task":
        "calls history_index.index_task immediately before both deletes",
}

# planning_log.forget is deliberately absent: it deletes from whatever
# namespace it is handed and cannot know whether that one is indexed. Its
# TASK_NAMESPACE caller is server.py::delete_task, which is listed above and
# pinned by its own test at the bottom of this file.


def _deleting_functions() -> dict[str, str]:
    """Every function in agent/ that removes a row from an indexed namespace."""
    found: dict[str, str] = {}
    for path in sorted(AGENT_DIR.rglob("*.py")):
        source = path.read_text()
        if ".adelete(" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(source, node) or ""
            if ".adelete(" not in body:
                continue
            if not any(marker in body for marker in _INDEXED):
                continue
            found[f"{path.relative_to(AGENT_DIR)}::{node.name}"] = body
    return found


def test_every_path_that_deletes_indexed_history_is_accounted_for():
    """A stored record that is not searchable is a gap the next sync closes.
    A deleted record that was never indexed is gone. Only this list says
    which of the two each delete path is."""
    found = _deleting_functions()

    unlisted = sorted(set(found) - set(_DELETE_PATHS))
    assert not unlisted, (
        "these delete a row the history index is the only other copy of, and nothing "
        "says what protects them -- index before you delete, then add the reason here:\n  "
        + "\n  ".join(unlisted))
    gone = sorted(set(_DELETE_PATHS) - set(found))
    assert not gone, f"this list is describing functions that no longer delete anything: {gone}"


def test_only_one_function_in_the_tree_deletes_an_episode():
    """Episodes are the corpus the whole subsystem was argued for, and the
    one with a retention window pointed at it. Two functions deleting them
    is two orderings to get right."""
    episode_deleters = sorted(
        name for name, body in _deleting_functions().items()
        if "episodes_namespace" in body or '"episodes"' in body)

    assert episode_deleters == ["consolidation.py::_prune_consolidated_episodes"], (
        f"a second episode-delete path appeared: {episode_deleters}")


def test_the_delete_task_route_indexes_before_it_deletes():
    """Source order here, deliberately, because there is no seam to record
    calls through: the route is a FastAPI handler and both deletes are
    inline. The index call has to come first in the function text."""
    source = (AGENT_DIR / "server.py").read_text()
    tree = ast.parse(source)
    route = next(n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == "delete_task")
    body = ast.get_source_segment(source, route) or ""

    assert "history_index.index_task(" in body, (
        "a task and its build transcript are indexed ONLY by the nightly sync, so a task "
        "deleted before the next run is never indexed at all")
    assert body.index("history_index.index_task(") < body.index('store.adelete(("tasks"'), (
        "the task row was deleted before it was indexed")
    assert body.index("history_index.index_task(") < body.index("planning_log.forget("), (
        "the build transcript was deleted before it was indexed")
