"""Fill the history index from what is already in the store.

The index is written forward from now on -- an episode as it is written, the
other corpora by the nightly consolidation pass. Everything that happened
before that code existed is in the store and in no index, which is the whole
corpus this subsystem was built for. This is the one-time pass that reads it.

    .venv/bin/python scripts/backfill_history_index.py            # dry run
    .venv/bin/python scripts/backfill_history_index.py --apply
    .venv/bin/python scripts/backfill_history_index.py --apply myproject

With no project named, every project in PROJECTS.

Dry run by default, like every other migration script here. It is also the
most useful mode: it prints how many rows each corpus WOULD produce without
opening a write, which is the number to sanity-check before letting an
extractor loose on a live database.

Safe to run while the server is live, and safe to run twice:

* It never writes to, deletes from or locks the `store`. It pages each
  namespace with the ordinary search the rest of the system uses, and every
  write goes to agent_history_fts, a table of ours that no task touches.
* The upsert only updates a row whose text actually changed, so a second
  run reports zero rows written -- that is the idempotence claim, and the
  --apply output is where it is checked rather than asserted.

Deliberately NOT under project_lock, unlike the other backfills in this
directory. Those write into the store, where a running task is a genuine
second writer. This one does not, and taking the project lock would stall a
live build for the duration of a read-only pass over its history.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import history_index
from agent.config import PROJECTS, load_config
from agent.graph import open_store
from agent.store_paging import all_items


async def _dry_run(repo: str, store) -> None:
    """What each corpus would produce, without opening a write."""
    total_items = total_rows = total_bad = 0
    for corpus in history_index.CORPORA:
        namespace = history_index.namespaces_for(corpus, repo)
        items = await all_items(store, namespace)
        rows = []
        bad = 0
        for item in items:
            # None is an extraction that FAILED, and it is the number worth
            # reading here: a record the extractor cannot read is a record
            # the nightly pass will leave with whatever chunks it already
            # has, so a dry run that reported it as "0 chunks" would be
            # reporting a hole as a shrug.
            extracted = history_index.rows_for_item(corpus, repo, item)
            if extracted is None:
                bad += 1
                continue
            rows.extend(extracted)
        chars = sum(len(r.err) + len(r.label) + len(r.body) for r in rows)
        with_err = sum(1 for r in rows if r.err)
        print(f"[backfill] {repo}: {corpus}: {len(items)} item(s) -> {len(rows)} chunk(s), "
              f"{with_err} carrying error text, {chars:,} characters"
              + (f", {bad} item(s) UNREADABLE" if bad else ""))
        total_items += len(items)
        total_rows += len(rows)
        total_bad += bad
    print(f"[backfill] {repo}: TOTAL {total_items} item(s) -> {total_rows} chunk(s) (dry run)"
          + (f" -- {total_bad} item(s) could not be extracted" if total_bad else ""))


async def main(repos: list[str], apply: bool) -> int:
    """Returns the number of projects that failed, for the exit code."""
    config = load_config()
    failures: list[str] = []
    async with open_store(config) as store:
        index = await history_index.install_for(config) if apply else None
        if apply and index is None:
            print("[backfill] no history index on this installation -- nothing was written")
            return 1
        for repo in repos:
            try:
                if not apply:
                    await _dry_run(repo, store)
                    continue
                result = await history_index.sync_project(config, repo, store, index=index)
                if result.failed:
                    failures.append(repo)
                print(f"[backfill] {repo}: {result.items} item(s) -> {result.rows} chunk(s), "
                      f"{result.written} written, {result.dropped} stale chunk(s) dropped, "
                      f"{result.demoted} demoted to index-only"
                      + (f" -- FAILED for {result.failed}" if result.failed else ""))
            except Exception as e:  # noqa: BLE001 -- one project must not abort the rest
                print(f"[backfill] {repo}: FAILED -- {e}")
                failures.append(repo)
        if apply and index is not None:
            print(f"[backfill] index now holds: {await index.counts()}")
    if failures:
        print(f"[backfill] {len(failures)} of {len(repos)} project(s) FAILED: {failures}")
    return len(failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repos", nargs="*", help="projects to index (default: all)")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; without it this only reports what it would write")
    args = parser.parse_args()
    sys.exit(1 if asyncio.run(main(args.repos or list(PROJECTS), args.apply)) else 0)
