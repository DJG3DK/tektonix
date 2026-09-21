"""What the two seats with memory actually render, and what they can fetch.

The one that matters is the first: with no sections in the store -- which is
every project until the data migration runs, and permanently for one small
enough not to need this -- both prompts must be what they were before any of
this existed, byte for byte. That is what makes the reader safe to deploy
ahead of the data. It is asserted here rather than eyeballed because "looks
the same" is exactly how a trailing newline gets into a cached prompt prefix
and quietly costs a cache write on every call.

The second is that the coordinator and the planner stay the same as each
other. The planner is where a durable fact gets written down and the
coordinator is where it has to be obeyed; a section pinned in one seat and
indexed in the other is a plan written against rules the build cannot see.
"""

import json
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from deepagents.backends import StoreBackend

import agent.deep_agent as da
import agent.planning_chat as pc
from agent import episode_recall, memory_sections as ms
from agent.config import load_config
from agent.memory_freshness import memory_with_freshness
from agent.tools.memory_tools import make_memory_tools
from agent.tools.memory_tools import ALL as ALL_SECTIONS
from tests.fixture_memory import EXAMPLE_MEMORY

REPO = "demo"


@pytest.fixture
def seats(tmp_path, monkeypatch):
    projects = {REPO: {"sandbox": str(tmp_path / REPO)}}
    (tmp_path / REPO).mkdir()
    monkeypatch.setattr(da, "PROJECTS", projects, raising=False)
    monkeypatch.setattr(pc, "PROJECTS", projects, raising=False)
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", projects)
    monkeypatch.setattr("agent.tools.reference_tools.PROJECTS", projects)
    return load_config(), InMemorySaver(), InMemoryStore()


def _capture(monkeypatch, module):
    captured = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(module, "create_deep_agent", fake_create_deep_agent)
    monkeypatch.setattr(module, "llm_for_role", lambda *a, **k: FakeListChatModel(responses=["x"]))
    return captured


def _backend(store) -> StoreBackend:
    return StoreBackend(namespace=da.project_namespace(REPO), store=store)


async def _write_whole_memory(store, text: str = EXAMPLE_MEMORY) -> None:
    await _backend(store).awrite(da.route_local_path("/memories/", da.MEMORY_PATH), text)


async def _split_memory_into_the_store(store, text: str = EXAMPLE_MEMORY, *, record_source: bool = False) -> list:
    """What the data migration does, in four lines. The migration itself (with
    its byte-for-byte assertion, its locking and its dry run) is
    scripts/migrate_memory_sections.py; the reader has to work against the
    shape it writes.

    `record_source` is the digest the real migration always records. Off by
    default here so that most of these tests exercise the index shape alone,
    and on where the point is what the reader does when /AGENTS.md has moved
    on since the split.
    """
    backend = _backend(store)
    preamble, sections = ms.split_sections(text)
    entries = ms.build_index(sections, preamble=preamble)
    meta = {"source_sha256": ms.source_digest(text)} if record_source else {}
    await backend.awrite(da.route_local_path("/memories/", ms.SECTIONS_CORE_PATH), preamble)
    for section in sections:
        await backend.awrite(da.route_local_path("/memories/", ms.section_path(section.slug)), section.body)
    await backend.awrite(da.route_local_path("/memories/", ms.SECTIONS_INDEX_PATH),
                         ms.index_to_json(entries, **meta))
    return entries


def _legacy_loader():
    """The exact expression both seats used before sections existed.

    A pin against future drift, not evidence about what HEAD did: it is a
    reimplementation living in this file, so the byte-identical tests below
    would pass even if the real loader had never matched the old one. What
    established that was rendering both prompts out of a worktree at the
    foundation commit and diffing the strings. This keeps them equal from
    here on.
    """
    async def load_project_memory(repo, store, *, task_id=None):
        backend = StoreBackend(namespace=da.project_namespace(repo), store=store)
        text = await da.read_memory_or_empty(backend, da.route_local_path("/memories/", da.MEMORY_PATH))
        return da.ProjectMemory(content=await memory_with_freshness(backend, text), entries=[])

    return load_project_memory


async def _coordinator_prompt(seats, monkeypatch) -> str:
    cfg, cp, store = seats
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, REPO, 5.0, cp, store)
    return captured["system_prompt"]


async def _planner_prompt(seats, monkeypatch) -> str:
    cfg, cp, store = seats
    captured = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, REPO, cp, store)
    return captured["system_prompt"]


# --- nothing changes until the data does -----------------------------------

async def test_the_coordinator_prompt_is_byte_identical_with_no_sections(seats, monkeypatch):
    cfg, cp, store = seats
    await _write_whole_memory(store)
    now = await _coordinator_prompt(seats, monkeypatch)

    monkeypatch.setattr(da, "load_project_memory", _legacy_loader())
    before = await _coordinator_prompt(seats, monkeypatch)
    assert now == before


async def test_the_planner_prompt_is_byte_identical_with_no_sections(seats, monkeypatch):
    cfg, cp, store = seats
    await _write_whole_memory(store)
    now = await _planner_prompt(seats, monkeypatch)

    monkeypatch.setattr(pc, "load_project_memory", _legacy_loader())
    before = await _planner_prompt(seats, monkeypatch)
    assert now == before


async def test_an_empty_memory_still_reads_as_it_always_did(seats, monkeypatch):
    """A project that has recorded nothing yet -- the onboarding case."""
    cfg, cp, store = seats
    memory = await da.load_project_memory(REPO, store)
    assert memory.content == "(nothing recorded yet)"
    assert memory.entries == []


async def test_neither_seat_carries_the_tool_when_there_are_no_sections(seats, monkeypatch):
    """The whole memory is already in the prompt, so the tool could only hand
    back text the model is looking at -- and every call would pay for its
    description to be told so."""
    cfg, cp, store = seats
    await _write_whole_memory(store)
    coordinator = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, REPO, 5.0, cp, store)
    planner = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, REPO, cp, store)
    assert "read_memory_section" not in {t.name for t in coordinator["tools"]}
    assert "read_memory_section" not in {t.name for t in planner["tools"]}


async def test_a_memory_under_the_rule_0_floor_renders_whole(seats, monkeypatch):
    """Rule 0: a small memory is not split, so there is no index to read and
    the prompt carries the file. Asserted through the loader, because the
    floor has to hold on the read path too -- not only in the migration that
    decided not to split."""
    small = "# demo\n\nSmall.\n\n## Testing\n\nRun `.venv/bin/pytest`.\n"
    assert not ms.is_worth_splitting(small)
    cfg, cp, store = seats
    await _write_whole_memory(store, small)
    memory = await da.load_project_memory(REPO, store)
    assert memory.content == small
    assert memory.entries == []


# --- once the sections exist -----------------------------------------------

async def test_both_seats_render_the_same_memory_block(seats, monkeypatch):
    cfg, cp, store = seats
    await _write_whole_memory(store)
    await _split_memory_into_the_store(store)

    memory = await da.load_project_memory(REPO, store)
    coordinator = await _coordinator_prompt(seats, monkeypatch)
    planner = await _planner_prompt(seats, monkeypatch)
    assert memory.content in coordinator
    assert memory.content in planner


async def test_the_block_pins_the_silent_rules_and_indexes_the_rest(seats):
    cfg, cp, store = seats
    await _split_memory_into_the_store(store)
    content = (await da.load_project_memory(REPO, store)).content

    assert "A bare `pytest` resolves to a system" in content          # testing, pinned
    assert "Two different `Catalogue` classes exist" in content       # collisions, pinned
    assert "Money is integer cents" not in content                    # conventions, indexed
    assert "conventions (~666 tok)" in content                        # ...and advertised
    assert "--- MEMORY INDEX ---" in content
    assert 'read_memory_section("all")' in content                    # the escape hatch is stated


async def test_the_block_is_much_smaller_than_the_file(seats):
    cfg, cp, store = seats
    await _split_memory_into_the_store(store)
    content = (await da.load_project_memory(REPO, store)).content
    assert ms.estimate_tokens(content) < ms.estimate_tokens(EXAMPLE_MEMORY) * 0.6


async def test_the_coordinator_and_the_planner_get_the_tool(seats, monkeypatch):
    cfg, cp, store = seats
    await _split_memory_into_the_store(store)
    coordinator = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, REPO, 5.0, cp, store)
    planner = _capture(monkeypatch, pc)
    await pc.build_planning_agent(cfg, REPO, cp, store)
    assert "read_memory_section" in {t.name for t in coordinator["tools"]}
    assert "read_memory_section" in {t.name for t in planner["tools"]}


async def test_the_seats_without_memory_do_not_get_the_tool(seats, monkeypatch):
    """A tool nobody's prompt mentions is how the investigator ended up
    describing a preview_app it did not have. These three seats are given the
    task, not the project's memory."""
    cfg, cp, store = seats
    await _split_memory_into_the_store(store)
    captured = _capture(monkeypatch, da)
    await da.build_deep_agent(cfg, REPO, 5.0, cp, store)
    for seat in captured["subagents"]:
        assert "read_memory_section" not in {t.name for t in seat["tools"]}, seat["name"]


async def test_turning_progressive_disclosure_off_restores_the_whole_file(seats, monkeypatch):
    cfg, cp, store = seats
    await _write_whole_memory(store)
    await _split_memory_into_the_store(store)
    monkeypatch.setitem(da._rs._values, "memory_progressive_disclosure", 0.0)

    memory = await da.load_project_memory(REPO, store)
    assert memory.content == EXAMPLE_MEMORY
    assert memory.entries == []


# The rollback is documented as "the toggle restores the whole-file behaviour
# by reassembling from the sections, without touching data". It was
# implemented as "read /memories/AGENTS.md", which is the same thing for
# exactly one more commit: the next one rewrites that file into a pointer
# stub. An emergency switch that hands the agent a stub instead of its memory
# is worse than no switch, because it will be thrown in the middle of the
# incident it was built for.

POINTER_STUB = "# Project memory\n\nThis file has been split. See /memories/sections/_index.json.\n"


async def _stub_the_source(store, *, text: str = EXAMPLE_MEMORY) -> None:
    """What the pointer-stub commit will do: /AGENTS.md replaced, and the
    index's record of the source updated with it so the split still reads as
    faithful (see _sections_match_source)."""
    backend = _backend(store)
    await backend.awrite(da.route_local_path("/memories/", da.MEMORY_PATH), POINTER_STUB)
    entries = ms.build_index(ms.split_sections(text)[1], preamble=ms.split_sections(text)[0])
    await backend.awrite(da.route_local_path("/memories/", ms.SECTIONS_INDEX_PATH),
                         ms.index_to_json(entries, source_sha256=ms.source_digest(POINTER_STUB)))


async def test_the_rollback_toggle_survives_the_pointer_stub(seats, monkeypatch):
    cfg, cp, store = seats
    await _write_whole_memory(store)
    await _split_memory_into_the_store(store)
    await _stub_the_source(store)
    monkeypatch.setitem(da._rs._values, "memory_progressive_disclosure", 0.0)

    memory = await da.load_project_memory(REPO, store)
    assert memory.content == EXAMPLE_MEMORY
    assert memory.entries == []


async def test_a_broken_index_after_the_stub_falls_back_to_the_sections(seats):
    """The other fallback branch, and the same trap: an index that does not
    parse must not leave the agent with the stub."""
    cfg, cp, store = seats
    await _write_whole_memory(store)
    await _split_memory_into_the_store(store)
    await _stub_the_source(store)
    entries = (await da.load_memory_index(_backend(store))).entries

    content = await da._whole_memory(_backend(store), entries)
    assert content == EXAMPLE_MEMORY


async def test_a_source_written_since_the_split_wins_over_the_sections(seats):
    """/memories/AGENTS.md stays the authoritative copy until the stub commit,
    and both the nightly consolidator and the agent's own file-edit tool write
    to it. The moment an index exists those writes land where no prompt reads
    -- silently, and it is the memory subsystem losing memory. A source that
    no longer matches the digest the split recorded turns this back into a
    whole-file read until the next migration re-splits it."""
    cfg, cp, store = seats
    await _split_memory_into_the_store(store, record_source=True)
    moved_on = EXAMPLE_MEMORY + "\n## Rate limiting\n\nThe supplier cap is 20 requests a second.\n"
    await _write_whole_memory(store, moved_on)

    memory = await da.load_project_memory(REPO, store)
    assert memory.content == moved_on
    assert memory.entries == []


async def test_a_source_that_still_matches_reads_from_the_sections(seats):
    cfg, cp, store = seats
    await _write_whole_memory(store)
    await _split_memory_into_the_store(store, record_source=True)

    memory = await da.load_project_memory(REPO, store)
    assert memory.entries
    assert "--- MEMORY INDEX ---" in memory.content


async def test_an_index_whose_sections_are_missing_falls_back_to_the_file(seats):
    """The one way this could silently amputate a project's memory: a
    confident list of sections with nothing behind it."""
    cfg, cp, store = seats
    await _write_whole_memory(store)
    entries = ms.build_index(ms.split_sections(EXAMPLE_MEMORY)[1])
    await _backend(store).awrite(
        da.route_local_path("/memories/", ms.SECTIONS_INDEX_PATH), ms.index_to_json(entries),
    )
    memory = await da.load_project_memory(REPO, store)
    assert memory.content == EXAMPLE_MEMORY
    assert memory.entries == []


async def test_lowering_the_budget_shrinks_the_block_without_hiding_anything(seats, monkeypatch):
    """The floor is only a floor if something holds it where the tokens are
    actually spent. The migration picks the pinned set against the budget of
    the day; the knob has to be able to take it back without a second
    migration -- and a section it takes back must become an indexed line the
    model can still fetch, not a hole."""
    cfg, cp, store = seats
    entries = await _split_memory_into_the_store(store)
    full = (await da.load_project_memory(REPO, store)).content

    monkeypatch.setitem(da._rs._values, "memory_inline_token_budget", 900.0)
    lean = (await da.load_project_memory(REPO, store)).content

    assert ms.estimate_tokens(lean) < ms.estimate_tokens(full)
    assert lean.count("already above") < full.count("already above")
    for entry in entries:
        assert f"- {entry.slug} " in lean, entry.slug


# --- reading a section -----------------------------------------------------

async def _tool(store, entries, **kwargs):
    tools = make_memory_tools(REPO, store, entries, **kwargs)
    return tools[0]


async def test_a_section_is_returned_whole(seats):
    cfg, cp, store = seats
    entries = await _split_memory_into_the_store(store)
    tool = await _tool(store, entries)
    body = await tool.ainvoke({"section": "conventions"})
    assert body.startswith("## Conventions")
    assert "Money is integer cents" in body


async def test_all_returns_the_whole_file_byte_for_byte(seats):
    """The escape hatch: when a model is unsure, one expensive call that
    definitely contains the answer beats three cheap ones that might not."""
    cfg, cp, store = seats
    entries = await _split_memory_into_the_store(store)
    tool = await _tool(store, entries)
    assert await tool.ainvoke({"section": "all"}) == EXAMPLE_MEMORY


async def test_an_unknown_slug_lists_the_ones_that_exist(seats):
    """A model told only "not found" guesses again, and the index it needs is
    a thousand tokens back up its own context."""
    cfg, cp, store = seats
    entries = await _split_memory_into_the_store(store)
    tool = await _tool(store, entries)
    answer = await tool.ainvoke({"section": "convention"})
    assert "conventions" in answer and "testing" in answer
    assert 'read_memory_section("all")' in answer


async def test_a_slug_written_as_a_path_or_with_an_extension_still_resolves(seats):
    cfg, cp, store = seats
    entries = await _split_memory_into_the_store(store)
    tool = await _tool(store, entries)
    assert (await tool.ainvoke({"section": "Testing.md"})).startswith("## Testing")
    assert (await tool.ainvoke({"section": "/testing"})).startswith("## Testing")


async def test_an_indexed_section_whose_file_vanished_says_so(seats):
    cfg, cp, store = seats
    entries = await _split_memory_into_the_store(store)
    await store.adelete((REPO,), ms.section_path("conventions").replace("/memories", ""))
    tool = await _tool(store, entries)
    answer = await tool.ainvoke({"section": "conventions"})
    assert 'read_memory_section("all")' in answer


# --- telemetry -------------------------------------------------------------

async def test_the_sections_offered_and_the_sections_read_are_recorded(seats, monkeypatch, tmp_path):
    """Without this there is no way to tell whether the index works -- an
    entry offered a hundred times and never read is either a section nobody
    needs or, far more likely, an entry that does not say what it is for."""
    log = tmp_path / "retrieval_events.jsonl"
    monkeypatch.setattr(episode_recall, "LOG_PATH", log)
    cfg, cp, store = seats
    entries = await _split_memory_into_the_store(store)

    await da.load_project_memory(REPO, store, task_id="t-1")
    tool = await _tool(store, entries, task_id="t-1")
    await tool.ainvoke({"section": "conventions"})

    events = [json.loads(line) for line in log.read_text().splitlines()]
    offered = next(e for e in events if e["event"] == "memory_offered")
    assert "conventions" in offered["sections"] and offered["task_id"] == "t-1"
    assert "testing" in offered["always"]
    read = next(e for e in events if e["event"] == "memory_read")
    assert read["section"] == "conventions" and read["repo"] == REPO


async def test_nothing_is_recorded_for_a_project_with_no_sections(seats, monkeypatch, tmp_path):
    """A project whose memory was never split would otherwise write a line on
    every prompt build saying nothing was offered."""
    log = tmp_path / "retrieval_events.jsonl"
    monkeypatch.setattr(episode_recall, "LOG_PATH", log)
    cfg, cp, store = seats
    await _write_whole_memory(store)
    await da.load_project_memory(REPO, store, task_id="t-2")
    assert not log.exists()


# --- a half-written split ---------------------------------------------------
#
# The migration writes the index LAST, so an interrupted run leaves sections
# with no index -- which reads as an unsplit project and is therefore safe by
# construction (asserted below). The dangerous half is the other one: an index
# that outlives one of the files it names. It does not need a bad migration to
# happen -- a restore that replays part of a namespace, an operator deleting a
# section by hand to force a re-split, a later consolidator edit whose index
# entry lands and whose section write does not -- and it used to be invisible,
# because the prompt still rendered and still looked complete.

async def _split_and_drop(store, slug: str) -> None:
    await _split_memory_into_the_store(store)
    await store.adelete((REPO,), da.route_local_path("/memories/", ms.section_path(slug)))


async def test_a_pinned_section_that_lost_its_file_falls_back_to_the_whole_file(seats):
    """The worst shape this subsystem can take, and the one that reads as
    healthy: the body is gone from the prompt AND the index line next to the
    gap says "already above, do not re-read", so the model is told not to go
    looking for it. The sections that get pinned are pinned BECAUSE they fail
    silently, so what is lost here is a rule whose violation nothing reports.
    """
    cfg, cp, store = seats
    await _write_whole_memory(store)
    pinned = next(e.slug for e in ms.build_index(ms.split_sections(EXAMPLE_MEMORY)[1]) if e.always)
    await _split_and_drop(store, pinned)

    memory = await da.load_project_memory(REPO, store)
    assert memory.content == EXAMPLE_MEMORY
    assert memory.entries == []


async def test_a_pinned_section_is_never_advertised_as_present_when_it_is_not(seats):
    """The assertion above passes for a trivially wrong reason too (a prompt
    with no memory at all), so this one names the failure directly: whatever
    is rendered, the index must not claim a body is above it that is not."""
    cfg, cp, store = seats
    await _write_whole_memory(store)
    entries = ms.build_index(ms.split_sections(EXAMPLE_MEMORY)[1])
    pinned = next(e for e in entries if e.always)
    body = next(s.body for s in ms.split_sections(EXAMPLE_MEMORY)[1] if s.slug == pinned.slug)
    await _split_and_drop(store, pinned.slug)

    content = (await da.load_project_memory(REPO, store)).content
    assert (f"- {pinned.slug} -- " in content) <= (body.strip() in content)


async def test_a_split_with_no_core_falls_back_rather_than_dropping_the_preamble(seats):
    """The preamble says what the project IS. It is the one part of a memory
    that is never indexed away, so a missing _core.md is not something to
    render around."""
    cfg, cp, store = seats
    await _write_whole_memory(store)
    await _split_memory_into_the_store(store)
    await store.adelete((REPO,), da.route_local_path("/memories/", ms.SECTIONS_CORE_PATH))

    memory = await da.load_project_memory(REPO, store)
    assert memory.content == EXAMPLE_MEMORY


async def test_an_empty_preamble_is_not_read_as_a_missing_one(seats):
    """...and the fallback above must not fire on a memory that legitimately
    opens straight into `## `, which has no preamble at all. The store tells
    an empty file from an absent one; this asserts that this code does too,
    because the cheap version of the check above would silently un-split
    every such project."""
    cfg, cp, store = seats
    text = EXAMPLE_MEMORY[EXAMPLE_MEMORY.index("## "):]
    await _write_whole_memory(store, text)
    await _split_memory_into_the_store(store, text)

    memory = await da.load_project_memory(REPO, store)
    assert memory.entries
    assert "--- MEMORY INDEX ---" in memory.content


async def test_sections_without_an_index_read_as_an_unsplit_project(seats):
    """The interrupted migration proper. The index is written last precisely
    so that this is the half that can be left behind, and it has to mean
    "not split yet" rather than anything cleverer."""
    cfg, cp, store = seats
    await _write_whole_memory(store)
    await _split_memory_into_the_store(store)
    await store.adelete((REPO,), da.route_local_path("/memories/", ms.SECTIONS_INDEX_PATH))

    memory = await da.load_project_memory(REPO, store)
    assert memory.content == EXAMPLE_MEMORY
    assert memory.entries == []


async def test_the_escape_hatch_never_hands_back_a_file_with_a_hole_in_it(seats):
    """read_memory_section("all") is the call a model makes BECAUSE it does
    not know what it is looking for, so a quietly short answer is worse here
    than anywhere else -- it reads as "this project has no rule about that"
    rather than as "I could not find it"."""
    cfg, cp, store = seats
    await _write_whole_memory(store)
    entries = await _split_memory_into_the_store(store)
    await store.adelete((REPO,), da.route_local_path("/memories/", ms.section_path("conventions")))

    tool = await _tool(store, entries)
    assert await tool.ainvoke({"section": ALL_SECTIONS}) == EXAMPLE_MEMORY


async def test_a_hole_with_no_whole_file_left_to_fall_back_on_says_so(seats):
    """Once /AGENTS.md is a pointer stub there is nothing to fall back to, and
    the only honest answer left is to name the gap. Silence here would be the
    model reasoning from a memory it has been told is complete."""
    cfg, cp, store = seats
    entries = await _split_memory_into_the_store(store)
    await store.adelete((REPO,), da.route_local_path("/memories/", ms.section_path("conventions")))

    tool = await _tool(store, entries)
    answer = await tool.ainvoke({"section": ALL_SECTIONS})
    assert "INCOMPLETE" in answer and "conventions" in answer
    assert "A bare `pytest` resolves to a system" in answer
