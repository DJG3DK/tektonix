"""The data migration: the one step of this overhaul that writes to memory
the running system loads into every model call.

Its failure mode is silence. A project whose conventions quietly stopped
being in the prompt does not raise anything -- the next task just starts
breaking rules nobody can see any more, and three days later nothing connects
that to a migration run. So the assertions here are mostly about what the
script REFUSES to do: refuses to write when the split is not a partition of
the file, refuses to overwrite a section somebody else wrote, refuses to leave
an index behind that points at a key that is not there, and refuses to touch
anything at all in a dry run.

The fixtures are the shapes a real memory turns out to have -- no headings at
all, a heading inside a code fence, two headings with the same text, CRLF,
trailing whitespace, an empty section body -- because the byte-for-byte
guarantee is only worth anything if it holds on the file the store actually
has, not on the one that was imagined.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
from langgraph.store.memory import InMemoryStore

import agent.deep_agent as da
from agent import memory_sections as ms
from tests.fixture_memory import EXAMPLE_MEMORY

REPO = "demo"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "migrate_memory_sections", REPO_ROOT / "scripts" / "migrate_memory_sections.py",
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the script defines a dataclass, and
    # dataclasses resolves annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mig = _load_script()


class _Config:
    dsn = None  # no database: project_lock falls back to the in-process lock


@pytest.fixture
def store():
    return InMemoryStore()


def _key(path: str) -> str:
    return da.route_local_path("/memories/", path)


async def _write_memory(store, text: str, repo: str = REPO) -> None:
    await store.aput((repo,), _key(da.MEMORY_PATH), {"content": text})


async def _read(store, path: str, repo: str = REPO) -> str | None:
    item = await store.aget((repo,), _key(path))
    return None if item is None else item.value["content"]


async def _keys(store, repo: str = REPO) -> list[str]:
    return sorted(item.key for item in await store.asearch((repo,), limit=1000))


async def _migrate(store, text: str = EXAMPLE_MEMORY, *, budget: int = ms.INLINE_TOKEN_BUDGET):
    await _write_memory(store, text)
    plan = await mig.plan_project(REPO, store, budget)
    if plan.ok:
        await mig.apply_plan(plan, store, _Config())
    return plan


# --- nothing is lost -------------------------------------------------------

async def test_the_sections_in_the_store_reassemble_to_the_original(store):
    plan = await _migrate(store)
    assert "verified" in await mig.verify(plan, store)

    entries = ms.parse_index_document(await _read(store, ms.SECTIONS_INDEX_PATH)).entries
    parts = [await _read(store, ms.SECTIONS_CORE_PATH)]
    for entry in entries:
        parts.append(await _read(store, ms.section_path(entry.slug)))
    assert "".join(parts) == EXAMPLE_MEMORY


@pytest.mark.parametrize("name,text", [
    ("crlf and trailing whitespace",
     "Preamble.   \r\n\r\n## One   \r\n\r\nBody one.  \r\n\r\n## Two\r\n\r\nBody two.\r\n"),
    ("duplicate headings",
     "Preamble.\n\n## Notes\n\nFirst.\n\n## Notes\n\nSecond.\n\n## Notes\n\nThird.\n"),
    ("a heading inside a fence",
     "Preamble.\n\n## Real\n\n```markdown\n## Not a section\n```\n\n## Also real\n\nBody.\n"),
    ("an empty section body", "Preamble.\n\n## Empty\n\n## Next\n\nBody.\n"),
    ("no preamble at all", "## First\n\nBody.\n\n## Second\n\nBody.\n"),
    ("no trailing newline", "Preamble.\n\n## One\n\nBody one.\n\n## Two\n\nBody two."),
])
async def test_the_nasty_shapes_still_reassemble(store, name, text):
    """These are not hypotheticals -- every one of them is in the fixture or
    in a real memory. A split that loses a byte on any of them loses it
    silently, because the prompt still renders."""
    padded = text + "\n" + ("filler line to clear the floor.\n" * 400)
    plan = await _migrate(store, padded)
    assert plan.ok, plan.skip
    assert "verified" in await mig.verify(plan, store)


async def test_a_memory_with_no_headings_is_left_alone(store):
    """Nothing to partition. The file is one blob, and a single section plus
    an index entry pointing at it is strictly worse than the file."""
    plan = await _migrate(store, "A memory with no headings at all.\n" * 400)
    assert not plan.ok
    assert "floor" in plan.skip or "partition" in plan.skip
    assert await _read(store, ms.SECTIONS_INDEX_PATH) is None


async def test_a_split_that_is_not_a_partition_is_never_written(store, monkeypatch):
    """The byte-for-byte assertion, made to fail on purpose. If joining the
    slices is not the file, this project does not get migrated at all -- the
    one guarantee that makes the rest of the script safe to run."""
    real_split = ms.split_sections

    def lossy(text):
        preamble, sections = real_split(text)
        return preamble, sections[:-1]

    monkeypatch.setattr(mig.ms, "split_sections", lossy)
    plan = await _migrate(store)
    assert not plan.ok
    assert "REASSEMBLY MISMATCH" in plan.skip
    assert await _keys(store) == [_key(da.MEMORY_PATH)]


# --- Rule 0 ----------------------------------------------------------------

async def test_a_small_memory_is_skipped_and_says_so(store):
    small = "# demo\n\nSmall.\n\n## Testing\n\nRun `.venv/bin/pytest`.\n"
    plan = await _migrate(store, small)
    assert not plan.ok
    assert "floor" in plan.skip and f"{len(small):,}" in plan.skip
    assert await _keys(store) == [_key(da.MEMORY_PATH)]


async def test_a_project_with_no_memory_yet_is_skipped(store):
    plan = await mig.plan_project(REPO, store, ms.INLINE_TOKEN_BUDGET)
    assert not plan.ok
    assert "no /memories/AGENTS.md" in plan.skip


# --- what it writes, and in what order -------------------------------------

async def test_the_source_file_is_never_touched(store):
    """Only new keys. Both the old reader (whole file) and the new one
    (sections) stay correct at every moment of the run, which is what makes
    this a no-op to roll back."""
    await _migrate(store)
    assert await _read(store, da.MEMORY_PATH) == EXAMPLE_MEMORY


async def test_the_original_is_archived_before_anything_else(store):
    plan = await _migrate(store)
    archived = [k for k in await _keys(store) if ".archived-" in k]
    assert len(archived) == 1
    item = await store.aget((REPO,), archived[0])
    assert item.value["content"] == plan.original == EXAMPLE_MEMORY


async def test_the_index_is_written_last(store):
    """An interruption has to leave an unreferenced section file, which reads
    as "not split yet", rather than an index entry pointing at a key that does
    not exist -- which reads as a section the model is told exists and cannot
    fetch."""
    await _write_memory(store, EXAMPLE_MEMORY)
    plan = await mig.plan_project(REPO, store, ms.INLINE_TOKEN_BUDGET)
    written = await mig.apply_plan(plan, store, _Config())
    assert written[-1] == ms.SECTIONS_INDEX_PATH
    assert written[0].startswith(da.MEMORY_PATH + ".archived-")
    assert ms.SECTIONS_CORE_PATH in written


async def test_the_index_records_what_it_was_cut_from(store):
    """So the reader can tell a faithful split from one that a later write to
    /AGENTS.md has left behind."""
    await _migrate(store)
    doc = ms.parse_index_document(await _read(store, ms.SECTIONS_INDEX_PATH))
    assert doc.source_sha256 == ms.source_digest(EXAMPLE_MEMORY)
    assert doc.migrated_at


# --- running it twice ------------------------------------------------------

async def test_running_it_twice_writes_nothing_the_second_time(store):
    await _migrate(store)
    before = {item.key: item.value["content"] for item in await store.asearch((REPO,), limit=1000)}

    plan = await mig.plan_project(REPO, store, ms.INLINE_TOKEN_BUDGET)
    assert plan.already
    after = {item.key: item.value["content"] for item in await store.asearch((REPO,), limit=1000)}
    assert after == before


async def test_a_section_somebody_edited_is_kept_and_reported(store):
    """An existing section that differs was written by something else -- a
    consolidator edit, an operator, an earlier run against a file that has
    since changed. Overwriting it is the one way this script can destroy
    something, so the project is skipped instead."""
    await _write_memory(store, EXAMPLE_MEMORY)
    await store.aput((REPO,), _key(ms.section_path("testing")), {"content": "## Testing\n\nSomething else.\n"})

    plan = await mig.plan_project(REPO, store, ms.INLINE_TOKEN_BUDGET)
    assert not plan.ok
    assert plan.conflicts == ["testing"]
    assert await _read(store, ms.section_path("testing")) == "## Testing\n\nSomething else.\n"
    assert await _read(store, ms.SECTIONS_INDEX_PATH) is None


# --- the dry run -----------------------------------------------------------

async def test_the_dry_run_writes_nothing(store, capsys):
    """The default. Planning a project touches the store only to read it."""
    await _write_memory(store, EXAMPLE_MEMORY)
    plan = await mig.plan_project(REPO, store, ms.INLINE_TOKEN_BUDGET)
    mig.report(plan, ms.INLINE_TOKEN_BUDGET)
    assert await _keys(store) == [_key(da.MEMORY_PATH)]


async def test_the_dry_run_prints_what_the_operator_has_to_approve(store, capsys):
    """The split, which sections are pinned AND why, the resulting floor, and
    the before/after prompt cost. The operator argues with the classification
    here, before anything is written -- which is the only reason the reason
    strings exist at all."""
    await _write_memory(store, EXAMPLE_MEMORY)
    plan = await mig.plan_project(REPO, store, ms.INLINE_TOKEN_BUDGET)
    mig.report(plan, ms.INLINE_TOKEN_BUDGET)
    out = capsys.readouterr().out

    assert "ALWAYS" in out and "index " in out
    assert "test wiring -- a suite that never ran still passes" in out
    assert "always-block floor" in out
    assert "prompt cost" in out and "off every model call" in out
    for entry in plan.entries:
        assert entry.slug in out


async def test_the_dry_run_says_when_a_project_is_under_the_floor(store, capsys):
    await _write_memory(store, "# demo\n\nSmall.\n\n## Testing\n\nRun it.\n")
    plan = await mig.plan_project(REPO, store, ms.INLINE_TOKEN_BUDGET)
    mig.report(plan, ms.INLINE_TOKEN_BUDGET)
    assert "SKIPPED" in capsys.readouterr().out


# --- what the prompt does with the result ----------------------------------

async def test_the_migrated_project_renders_a_prompt_inside_the_budget(store):
    """The floor the dry run promised, measured through the real loader rather
    than through the script's own arithmetic."""
    await _migrate(store)
    memory = await da.load_project_memory(REPO, store)
    assert memory.entries
    assert ms.estimate_tokens(memory.content) <= ms.INLINE_TOKEN_BUDGET
    assert ms.estimate_tokens(memory.content) < ms.estimate_tokens(EXAMPLE_MEMORY) / 2


async def test_everything_the_prompt_no_longer_carries_is_still_readable(store):
    """The other half of the promise: nothing is removed from what the agent
    can reach, only from what it is handed unasked."""
    from agent.tools.memory_tools import make_memory_tools  # noqa: PLC0415

    await _migrate(store)
    memory = await da.load_project_memory(REPO, store)
    tool = make_memory_tools(REPO, store, memory.entries)[0]
    assert "Money is integer cents" not in memory.content
    assert "Money is integer cents" in await tool.ainvoke({"section": "conventions"})
    assert await tool.ainvoke({"section": "all"}) == EXAMPLE_MEMORY
