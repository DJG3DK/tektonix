"""Entry point for scheduled (cron) memory consolidation -- the
"background compute" half of the memory system, separate from any task's
own hot path. Run this periodically (e.g. daily) per project:

    .venv/bin/python scripts/run_consolidation.py [repo ...]

With no arguments, runs for every project in PROJECTS.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import history_index
from agent.config import PROJECTS, load_config
from agent.consolidation import run_consolidation
from agent.graph import open_checkpointer, open_store


async def main(repos: list[str]) -> int:
    """Returns the number of projects that failed, for the exit code."""
    config = load_config()
    failures: list[tuple[str, str]] = []
    async with open_checkpointer(config) as checkpointer, open_store(config) as store:
        # This process writes the history index as well as memory, and
        # nothing installs one for it -- the index is never opened on
        # demand, so a job that does not ask for one indexes nothing. The
        # per-project summary printed below reports what it wrote, which is
        # where a silently-missing index would show up.
        if await history_index.install_for(config) is None:
            print("[consolidation] history index unavailable -- memory will still be "
                  "consolidated, but nothing will be indexed", flush=True)
        for repo in repos:
            print(f"[consolidation] {repo}: running...", flush=True)
            try:
                summary = await run_consolidation(config, repo, checkpointer, store)
            except Exception as e:  # noqa: BLE001 -- one project's failure shouldn't abort the rest
                print(f"[consolidation] {repo}: FAILED -- {e}", flush=True)
                failures.append((repo, str(e)))
                continue
            print(f"[consolidation] {repo}: {summary}", flush=True)
            # A failed index sync is a failure of this job, not a detail of
            # it. Without this the line above printed history_rows_written:
            # 0 -- which is also exactly what a healthy night with nothing
            # new prints -- the run exited 0, and the banner below, added
            # because a provider incompatibility went unnoticed for months,
            # did not cover the index at all. It is also the state in which
            # episodes are NOT pruned, so the next operator to look is
            # looking at a store that is growing for a reason.
            if summary.get("history_failed"):
                failures.append((repo, "history index: "
                                 + (summary.get("episodes_not_pruned")
                                    or f"failed for {summary['history_failed']}")))

    # Fail loudly. This used to print a line and exit 0, so cron stayed silent
    # and a broken nightly run looked identical to a healthy one -- the reason a
    # provider incompatibility went unnoticed for months. A non-zero exit gives
    # cron something to report, and the banner is greppable in the log.
    if failures:
        print("", flush=True)
        print("=" * 72, flush=True)
        print(f"CONSOLIDATION FAILED for {len(failures)} of {len(repos)} project(s):", flush=True)
        for repo, err in failures:
            print(f"  - {repo}: {err[:400]}", flush=True)
        print("=" * 72, flush=True)
        print("Memory was NOT updated, or its history was NOT indexed, for the projects", flush=True)
        print("above. This is not a no-op — episodes stay unconsolidated, and unpruned,", flush=True)
        print("until this is fixed and re-run.", flush=True)
    return len(failures)


if __name__ == "__main__":
    repos = sys.argv[1:] or list(PROJECTS.keys())
    sys.exit(1 if asyncio.run(main(repos)) else 0)
