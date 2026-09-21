"""The single episode writer.

Episodes are the only durable record of how past tasks ended. Three separate
pieces of work were each about to edit the 25-line function that wrote them,
so it moved here; these tests pin what it writes and pin the rule that keeps
it the only writer.
"""

import ast
import json
import pathlib

import pytest

from agent import episodes
from agent.deep_agent import EPISODES_ROUTE, episodes_namespace

AGENT_DIR = pathlib.Path(__file__).resolve().parent.parent / "agent"


class FakeStore:
    """Enough of a store for StoreBackend.awrite (aget then aput)."""

    def __init__(self):
        self.data: dict[tuple, object] = {}

    async def aget(self, namespace, key):
        return self.data.get((namespace, key))

    async def aput(self, namespace, key, value):
        self.data[(namespace, key)] = type("Item", (), {"value": value})()


def _config():
    """A real Config: write_episode's signature says Config, not None, and a
    test that passes None is a test that would not notice the parameter
    being dropped."""
    from agent.config import load_config

    return load_config()


def _record(**over):
    record = {
        "task_id": "t1",
        "goal": "do the thing",
        "outcome": "shipped",
        "escalation_reason": None,
        "review_verdict": "READY",
        "cost_usd": 0.4,
        "iteration_count": 2,
        "timestamp": "2026-09-21T10:00:00Z",
    }
    record.update(over)
    return record


async def _read(store, repo, key):
    from deepagents.backends import StoreBackend
    from deepagents.backends.utils import file_data_to_string

    backend = StoreBackend(namespace=episodes_namespace(repo), store=store)
    result = await backend.aread(key)
    return json.loads(file_data_to_string(result.file_data))


async def test_an_episode_lands_in_its_project_s_namespace_and_reads_back():
    store = FakeStore()
    key = await episodes.write_episode(store, _config(), "test-repo", _record())

    assert key.startswith(EPISODES_ROUTE)
    assert await _read(store, "test-repo", key) == _record()


async def test_two_episodes_written_in_the_same_second_do_not_overwrite_each_other():
    """Append-only is the whole contract: the consolidation agent reads
    every episode since a marker, and a collision would delete one."""
    store = FakeStore()
    first = await episodes.write_episode(store, _config(), "test-repo", _record())
    second = await episodes.write_episode(store, _config(), "test-repo", _record(task_id="t2"))

    assert first != second
    assert len(store.data) == 2


async def test_the_key_sorts_by_time():
    """consolidation.py orders episodes by KEY, not by updated_at, and says
    so in a comment. This is that assumption."""
    store = FakeStore()
    early = await episodes.write_episode(store, _config(), "test-repo", _record(timestamp="2026-09-01T00:00:00Z"))
    late = await episodes.write_episode(store, _config(), "test-repo", _record(timestamp="2026-09-21T00:00:00Z"))
    assert early < late


async def test_projects_do_not_share_a_namespace():
    store = FakeStore()
    await episodes.write_episode(store, _config(), "test-repo", _record())
    await episodes.write_episode(store, _config(), "other-repo", _record())
    namespaces = {ns for ns, _ in store.data}
    assert namespaces == {("episodes", "test-repo"), ("episodes", "other-repo")}


# --- the rule that keeps this the only writer ------------------------------

def _episode_backend_names(tree):
    """Local names bound to a StoreBackend over the episodes namespace."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if getattr(call.func, "id", None) != "StoreBackend":
            continue
        for kw in call.keywords:
            inner = kw.value
            if isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "episodes_namespace":
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_nothing_outside_the_writer_writes_an_episode():
    """Episodes are append-only, and every reader in the tree relies on it.

    A second writer would not announce itself: StoreBackend rebuilds the
    stored document from its own file shape on the way in, so an update
    from somewhere else silently drops whatever the writer here put on the
    record, and the failure surfaces much later as a search that matches
    text the episode no longer contains.
    """
    offenders = []
    for path in sorted(AGENT_DIR.rglob("*.py")):
        if path.name == "episodes.py":
            continue
        source = path.read_text()
        if "episodes_namespace" not in source:
            continue
        tree = ast.parse(source)
        backends = _episode_backend_names(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "awrite"
                    and getattr(node.func.value, "id", None) in backends):
                offenders.append(f"{path.relative_to(AGENT_DIR.parent)}:{node.lineno}")
    assert not offenders, (
        "episodes are written in agent/episodes.py and nowhere else; also here:\n  "
        + "\n  ".join(offenders)
    )
