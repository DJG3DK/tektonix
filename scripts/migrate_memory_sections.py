"""Split each project's /memories/AGENTS.md into section files plus an index.

DRY RUN BY DEFAULT. Writing needs --write.

This is the only step of the memory overhaul that modifies live data the
running system puts into every model call, and its failure mode is silent: an
agent whose conventions quietly vanished does not crash, it carries on and
starts violating them, and nobody connects that to a migration run three days
earlier. Everything unusual about this script is that sentence.

  * Nothing is rewritten. Only NEW keys are written -- /AGENTS.md is left
    exactly as it is, so both the old reader (whole file) and the new one
    (sections) are correct against the store at every moment of the run, and
    rolling the whole thing back is deleting keys nobody reads.
  * The split is asserted to be a partition BEFORE any write: preamble plus
    every section body, joined in index order, has to equal the original byte
    for byte. A project that fails that is skipped and reported, never
    written. Nothing can be lost by a split that never happens.
  * The index is written LAST, so an interruption leaves an unreferenced
    section file -- which reads as "not split yet" -- rather than an index
    entry pointing at a key that does not exist.
  * The original is archived first, under a timestamped key in the same
    namespace, and kept. project_removal.collect archives every key in the
    namespace, so it rides along into project archive/restore by itself.
  * Running it twice changes nothing the second time, and a section file that
    someone has edited in between is kept and reported rather than clobbered.
  * project_lock per project, so a running task cannot interleave its own
    memory write with the split. A long task blocks the migration, which is
    the correct way round.

The dry run is the point of the script, not a safety feature bolted onto it.
It prints the split, which sections land in the always-block and why, the
token floor that results, and the before/after prompt cost -- the operator
reads that, adjusts memory_inline_token_budget or the policy in
agent/memory_sections.py, and runs it again. The numbers it prints are the
ones the runtime is held to: the same budget is spent again at render time.

Run:
  .venv/bin/python scripts/migrate_memory_sections.py                 # dry run
  .venv/bin/python scripts/migrate_memory_sections.py --project demo  # one project
  .venv/bin/python scripts/migrate_memory_sections.py --write --yes   # for real
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepagents.backends import StoreBackend
from deepagents.backends.utils import file_data_to_string

from agent import memory_sections as ms
from agent import runtime_settings
from agent.config import PROJECTS, load_config
from agent.deep_agent import MEMORY_PATH, project_namespace, route_local_path
from agent.graph import open_store, project_lock

ARCHIVE_PREFIX = MEMORY_PATH + ".archived-"


@dataclass
class Plan:
    """What the run intends to do to one project, decided entirely before
    anything is written. A plan that is not `ok` is a project the run skips:
    the decision to leave a project alone is made once, here, rather than
    halfway through its writes."""

    repo: str
    budget_tokens: int = ms.INLINE_TOKEN_BUDGET
    original: str = ""
    preamble: str = ""
    sections: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    skip: str = ""
    conflicts: list = field(default_factory=list)
    already: bool = False

    @property
    def ok(self) -> bool:
        return not self.skip and not self.conflicts


def _key(path: str) -> str:
    return route_local_path("/memories/", path)


def _backend(repo: str, store) -> StoreBackend:
    return StoreBackend(namespace=project_namespace(repo), store=store)


async def _read(backend: StoreBackend, path: str) -> str | None:
    result = await backend.aread(_key(path))
    if result.error is not None or not result.file_data:
        return None
    return file_data_to_string(result.file_data)


async def plan_project(repo: str, store, budget_tokens: int) -> Plan:
    """Everything the run needs to decide about one project, with no writes.

    The order of the checks is the order of the guarantees: is there anything
    to split, is it big enough to be worth splitting, does the split
    reassemble byte for byte, and only then what would be pinned.
    """
    plan = Plan(repo=repo, budget_tokens=budget_tokens)
    backend = _backend(repo, store)
    original = await _read(backend, MEMORY_PATH)
    if not original:
        plan.skip = "no /memories/AGENTS.md yet"
        return plan
    plan.original = original

    # Rule 0. The index costs prompt tokens and a section costs a tool call,
    # so below this the machinery costs more than the file it is saving --
    # and a brand-new project, whose memory is a starter stub, must not change
    # shape at all.
    if not ms.is_worth_splitting(original):
        plan.skip = (f"under the {ms.SPLIT_FLOOR_CHARS:,}-char floor "
                     f"({len(original):,} chars) -- not split, by policy")
        return plan

    plan.preamble, plan.sections = ms.split_sections(original)
    # The no-loss guarantee, asserted before anything is written rather than
    # after. A section is a SLICE of the file; if joining the slices is not
    # the file, the split is not a partition and this project does not get
    # migrated at all.
    if ms.render_full(plan.preamble, plan.sections) != original:
        plan.skip = "REASSEMBLY MISMATCH -- the split is not a partition of the file; skipped"
        return plan

    plan.entries = ms.build_index(plan.sections, preamble=plan.preamble, budget_tokens=budget_tokens)

    index = await _read(backend, ms.SECTIONS_INDEX_PATH)
    if index is not None and ms.parse_index_document(index).source_sha256 == ms.source_digest(original):
        plan.already = True

    # Never clobber. An existing section file whose contents differ from what
    # the split produces was written by something else -- a consolidator edit,
    # an operator, a half-finished earlier run against a file that has since
    # changed -- and overwriting it is the one way this script can destroy
    # something. It is reported instead, and the project is skipped, because
    # an index whose sections do not reassemble to the original is exactly the
    # silent loss the rest of this file is built to prevent.
    for section in plan.sections:
        existing = await _read(backend, ms.section_path(section.slug))
        if existing is not None and existing != section.body:
            plan.conflicts.append(section.slug)
    core = await _read(backend, ms.SECTIONS_CORE_PATH)
    if core is not None and core != plan.preamble:
        plan.conflicts.append(ms.SECTIONS_CORE_PATH)
    return plan


async def apply_plan(plan: Plan, store, config) -> list[str]:
    """Write one project's split, in the order that makes every interruption
    survivable: archive, then sections, then the index LAST."""
    backend = _backend(plan.repo, store)
    written: list[str] = []
    async with project_lock(plan.repo, config.dsn):
        stamp = datetime.now(UTC)
        archive = ARCHIVE_PREFIX + stamp.strftime("%Y-%m-%d")
        existing = await _read(backend, archive)
        if existing is not None and existing != plan.original:
            # Two different originals archived on one day: a re-split after
            # the file moved on. Keep both -- the whole value of an archive is
            # that nothing in this script ever overwrites one.
            archive = ARCHIVE_PREFIX + stamp.strftime("%Y-%m-%dT%H%M%SZ")
        if existing is None or existing != plan.original:
            await backend.awrite(_key(archive), plan.original)
            written.append(archive)

        await backend.awrite(_key(ms.SECTIONS_CORE_PATH), plan.preamble)
        written.append(ms.SECTIONS_CORE_PATH)
        for section in plan.sections:
            await backend.awrite(_key(ms.section_path(section.slug)), section.body)
            written.append(ms.section_path(section.slug))

        # Last, and carrying what it was cut from: the reader compares that
        # digest against /AGENTS.md and falls back to reading the file whole
        # if anything has written to it since, which is what keeps a
        # consolidator run landing mid-migration from disappearing.
        await backend.awrite(_key(ms.SECTIONS_INDEX_PATH), ms.index_to_json(
            plan.entries,
            source_sha256=ms.source_digest(plan.original),
            migrated_at=stamp.isoformat(timespec="seconds"),
            budget_tokens=plan.budget_tokens,
        ))
        written.append(ms.SECTIONS_INDEX_PATH)
    return written


async def verify(plan: Plan, store) -> str:
    """Read the project back through the store and reassemble it. The whole
    migration is one claim -- the sections ARE the file -- and this is that
    claim checked against what is actually in the database, not against what
    the script thinks it wrote."""
    backend = _backend(plan.repo, store)
    core = await _read(backend, ms.SECTIONS_CORE_PATH)
    if core is None:
        return "FAILED: no /sections/_core.md after the write"
    entries = ms.parse_index_document(await _read(backend, ms.SECTIONS_INDEX_PATH) or "").entries
    parts = [core]
    for entry in entries:
        body = await _read(backend, ms.section_path(entry.slug))
        if body is None:
            return f"FAILED: /sections/{entry.slug}.md is indexed but not readable"
        parts.append(body)
    if "".join(parts) != plan.original:
        return "FAILED: reassembly from the store does not equal the original"
    return f"verified: {len(entries)} sections reassemble to the original byte for byte"


def report(plan: Plan, budget_tokens: int) -> None:
    """The dry run's whole output for one project. The operator reads this and
    argues with it before anything is written, which is why every pinned
    section prints the reason it was pinned rather than just the fact."""
    print(f"\n=== {plan.repo} ===")
    if plan.skip:
        print(f"  SKIPPED: {plan.skip}")
        return
    print(f"  /memories/AGENTS.md: {len(plan.original):,} chars "
          f"(~{ms.estimate_tokens(plan.original):,} tok), {len(plan.sections)} sections")
    if plan.already:
        print("  already split from this exact file -- a re-run writes nothing")
    if plan.conflicts:
        print(f"  CONFLICT: these keys exist and differ from the split: {', '.join(plan.conflicts)}")
        print("  SKIPPED: nothing is overwritten. Inspect those keys, delete them if they are stale, "
              "and run again.")
        return

    bodies = {s.slug: s.body for s in plan.sections}
    resident = ms.fit_to_budget(plan.preamble, plan.entries, bodies, budget_tokens)
    print(f"  preamble: {len(plan.preamble):,} chars (always resident, never indexed away)")
    for entry in plan.entries:
        mark = "ALWAYS" if entry.slug in resident else "index "
        print(f"    {mark} {entry.slug:<44} {entry.chars:>6,} chars (~{entry.tokens:>4,} tok)")
        if entry.slug in resident:
            print(f"           ^ {entry.reason}")
        else:
            print(f"           -> {entry.summary}")

    block = ms.render_prompt_block(plan.preamble, plan.entries, bodies, budget_tokens)
    floor = sum(len(bodies[s]) for s in resident)
    before, after = ms.estimate_tokens(plan.original), ms.estimate_tokens(block)
    print(f"  always-block floor: {len(resident)} sections, {floor:,} chars "
          f"(~{ms.estimate_tokens_from_chars(floor):,} tok)")
    print(f"  index: ~{ms.estimate_tokens(ms.render_index_block(plan.entries)):,} tok"
          f"   budget: {budget_tokens:,} tok")
    print(f"  prompt cost: ~{before:,} tok -> ~{after:,} tok "
          f"({(1 - after / before) * 100:.0f}% off every model call of every task)")
    if after > budget_tokens:
        print(f"  WARNING: the rendered block is over the {budget_tokens:,}-token budget")


async def main(args) -> int:
    config = load_config()
    repos = [args.project] if args.project else list(PROJECTS)
    unknown = [r for r in repos if r not in PROJECTS]
    if unknown:
        print(f"unknown project(s): {', '.join(unknown)}")
        return 2

    async with open_store(config) as store:
        # The operator's own budget, not this file's default: the floor
        # printed below has to be the floor the runtime will hold.
        await runtime_settings.load(store)
        budget = args.budget or int(runtime_settings.value("memory_inline_token_budget"))
        print(f"{'WRITING' if args.write else 'DRY RUN -- nothing is written'}"
              f" | inline budget {budget:,} tok | {len(repos)} project(s)")

        plans = []
        for repo in repos:
            plan = await plan_project(repo, store, budget)
            report(plan, budget)
            plans.append(plan)

        if not args.write:
            print("\nDry run. Re-run with --write to apply, after checking the always-block above.")
            return 0

        print()
        for plan in plans:
            if not plan.ok:
                continue
            if plan.already:
                print(f"{plan.repo}: unchanged since the last split -- nothing written")
                continue
            written = await apply_plan(plan, store, config)
            print(f"{plan.repo}: wrote {len(written)} keys ({written[0]} first, {written[-1]} last)")
            print(f"{plan.repo}: {await verify(plan, store)}")
        print("\n/memories/AGENTS.md is untouched, so both readers are still correct. "
              "The pointer stub is a separate commit, after these prompts have been checked.")
        print("Note for that check: stale-memory flags still cite repo paths only, so a flag can "
              "quote a line that now lives in a section the prompt is not carrying -- the "
              "section-attribution half of agent/memory_freshness.py lands with it.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="apply the split; without it nothing is written")
    ap.add_argument("--project", help="one project, by the name it has in projects.json")
    ap.add_argument("--budget", type=int, help="override memory_inline_token_budget for this run")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parsed = ap.parse_args()
    if parsed.write and not parsed.yes:
        answer = input("This writes new keys to every project's live memory. Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            raise SystemExit("aborted")
    raise SystemExit(asyncio.run(main(parsed)))
