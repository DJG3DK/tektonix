"""The consolidator must keep the section layer in step with what it writes.

The nightly job reads a project's whole memory, asks a model for an updated
whole memory, and writes it back to one key. Once that project's memory is
split, every seat's prompt reads the SECTIONS instead -- so a consolidation
that updates only the whole-file key goes on succeeding every night, reports
"memory_changed": true, and quietly stops reaching any agent.

Nothing fails. No test goes red, no alert fires, and the first sign is an
agent violating a convention it was told about three weeks earlier. That is
the exact failure the split exists to prevent, arriving from the other side,
which is why it gets its own file.
"""
from __future__ import annotations

import pytest

from agent import memory_sections as ms
from agent.deep_agent import (
    SECTIONS_CORE_PATH,
    SECTIONS_INDEX_PATH,
    load_memory_index,
    resplit_memory_sections,
    route_local_path,
)


def key(path: str) -> str:
    """The key as the store actually holds it. The backend routes
    /memories/... down to /..., and a test that writes the unrouted form is
    testing a store nobody has."""
    return route_local_path("/memories/", path)


class _Backend:
    """The three calls resplit needs, over a dict, answering the way the real
    StoreBackend does -- a ReadResult carrying either an error or file data,
    never None. A double that answers a shape the real one never returns
    tests the double."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.deleted: list[str] = []

    async def aread(self, path: str, offset: int = 0, limit: int = 2000):
        from deepagents.backends.store import FileData, ReadResult

        text = self.files.get(path)
        if text is None:
            return ReadResult(error=f"File '{path}' not found")
        return ReadResult(file_data=FileData(content=text))

    async def awrite(self, path: str, content: str) -> None:
        self.files[path] = content

    async def adelete(self, path: str) -> None:
        self.files.pop(path, None)
        self.deleted.append(path)


def _memory(*bodies: tuple[str, str], preamble: str = "# a project\n\nPreamble.\n") -> str:
    return preamble + "".join(f"\n## {t}\n\n{b}\n" for t, b in bodies)


ORIGINAL = _memory(
    ("Test wiring rules", "Get these wrong and the suite silently never runs.\n" * 12),
    ("Conventions", "A convention.\n" * 90),
    ("Deployment notes", "How it deploys.\n" * 40),
)


async def _split(backend: _Backend, text: str) -> None:
    """Put the backend in the state the migration leaves behind."""
    preamble, sections = ms.split_sections(text)
    entries = ms.build_index(sections, preamble=preamble)
    backend.files[key(SECTIONS_CORE_PATH)] = preamble
    for s in sections:
        backend.files[key(ms.section_path(s.slug))] = s.body
    backend.files[key(SECTIONS_INDEX_PATH)] = ms.index_to_json(entries)


async def test_a_consolidation_reaches_the_sections_the_prompt_reads():
    """The whole point. Without this the update lands on a key nothing reads."""
    backend = _Backend()
    await _split(backend, ORIGINAL)

    updated = ORIGINAL + "\n## Something learned last night\n\nA new durable fact.\n"
    written = await resplit_memory_sections(backend, updated)

    assert "something-learned-last-night" in written
    index = await load_memory_index(backend)
    assert "something-learned-last-night" in {e.slug for e in index.entries}
    assert "A new durable fact." in backend.files[key(ms.section_path("something-learned-last-night"))]


async def test_the_sections_still_reassemble_to_what_was_written():
    """A re-split that loses a byte is worse than one that does not run."""
    backend = _Backend()
    await _split(backend, ORIGINAL)
    updated = ORIGINAL.replace("A convention.", "A revised convention.")

    await resplit_memory_sections(backend, updated)

    index = await load_memory_index(backend)
    parts = [backend.files[key(SECTIONS_CORE_PATH)]]
    parts += [backend.files[key(ms.section_path(e.slug))] for e in index.entries]
    assert "".join(parts) == updated


async def test_a_section_the_consolidator_dropped_does_not_linger():
    """Left behind it is unreachable rather than harmful -- until a later
    read of everything picks it up and it reappears as memory nobody wrote."""
    backend = _Backend()
    await _split(backend, ORIGINAL)
    assert key(ms.section_path("deployment-notes")) in backend.files

    trimmed = _memory(
        ("Test wiring rules", "Get these wrong and the suite silently never runs.\n" * 12),
        ("Conventions", "A convention.\n" * 90),
    )
    await resplit_memory_sections(backend, trimmed)

    assert key(ms.section_path("deployment-notes")) not in backend.files
    index = await load_memory_index(backend)
    assert "deployment-notes" not in {e.slug for e in index.entries}


async def test_an_unsplit_project_is_left_alone():
    """Most projects are under the floor and have no section layer. Creating
    one behind the operator's back would split a memory the policy says to
    leave whole."""
    backend = _Backend({"/AGENTS.md": ORIGINAL})
    assert await resplit_memory_sections(backend, ORIGINAL) == []
    assert key(SECTIONS_INDEX_PATH) not in backend.files
    assert backend.files == {"/AGENTS.md": ORIGINAL}


async def test_the_index_is_written_after_the_bodies():
    """An interruption must leave an orphaned body, never an index entry
    pointing at a key that is not there -- the reader can survive the first
    and has to fall back on the second."""
    order: list[str] = []
    backend = _Backend()
    await _split(backend, ORIGINAL)
    real = backend.awrite

    async def recording(path: str, content: str) -> None:
        order.append(path)
        await real(path, content)

    backend.awrite = recording  # type: ignore[method-assign]
    await resplit_memory_sections(backend, ORIGINAL)

    assert order[-1] == key(SECTIONS_INDEX_PATH), order


async def test_a_smaller_resident_budget_pins_less_on_a_resplit():
    """The budget that bounds the prompt has to be the same one on every path
    that writes an index, or a re-split quietly re-pins what the operator
    lowered the budget to demote. Asserted as a comparison rather than an
    absolute count: what fits in a given budget is a property of the section
    sizes, and pinning it here would make this a test of the fixture."""
    small, large = _Backend(), _Backend()
    await _split(small, ORIGINAL)
    await _split(large, ORIGINAL)

    await resplit_memory_sections(small, ORIGINAL, budget_tokens=200)
    await resplit_memory_sections(large, ORIGINAL, budget_tokens=100_000)

    pinned_small = [e for e in (await load_memory_index(small)).entries if e.always]
    pinned_large = [e for e in (await load_memory_index(large)).entries if e.always]
    assert len(pinned_small) < len(pinned_large)
    assert pinned_large, "an unbounded budget must pin every qualifying section"
