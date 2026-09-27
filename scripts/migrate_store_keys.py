"""One-time migration: move agent-visible store files from full-path keys to
route-stripped keys.

Background: app code (seeding, prompt reads) used full agent-visible paths as
raw store keys ('/memories/AGENTS.md'), while the agent's own file tools go
through the CompositeBackend, whose route stripping produces '/AGENTS.md'
(see agent/project_memory.py's route_local_path). The two halves could not see
each other's writes -- agent-written memory was invisible to prompts, and
seeded skills were unreadable by the agent. App code now uses stripped keys
everywhere; this migrates any existing full-path data over.

Rules:
- Copy full-path value -> stripped key only if the stripped key is absent
  (an agent-written file at the stripped key is newer than the seed and is
  never clobbered).
- Delete the full-path key afterward either way, so stale copies can't be
  read by accident again.

Run: .venv/bin/python scripts/migrate_store_keys.py
"""

import asyncio

DRY_RUN = False
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import PROJECTS, load_config
from agent.graph import open_store

# (namespace, route prefix) pairs whose keys may hold full-path entries.
NAMESPACES = (
    [(("org",), "/org-memory/")]
    + [((project,), "/memories/") for project in PROJECTS]
    + [(("skills", project), "/skills/") for project in PROJECTS]
)


async def main() -> None:
    config = load_config()
    async with open_store(config) as store:
        for namespace, route in NAMESPACES:
            items = await store.asearch(namespace, limit=10_000)
            for item in items:
                if not item.key.startswith(route):
                    continue  # already stripped (or something else entirely) -- leave it
                stripped = "/" + item.key[len(route):]
                existing = await store.aget(namespace, stripped)
                if existing is None:
                    await store.aput(namespace, stripped, item.value)
                    print(f"{namespace}: migrated {item.key!r} -> {stripped!r}")
                else:
                    print(f"{namespace}: kept newer {stripped!r}, dropping stale seed at {item.key!r}")
                if DRY_RUN:
                    print(f"  [dry-run] would delete {item.key!r}")
                else:
                    await store.adelete(namespace, item.key)

        print("\n=== post-migration state ===")
        for namespace, _route in NAMESPACES:
            items = await store.asearch(namespace, limit=10_000)
            for item in items:
                print(f"{namespace}: {item.key!r} ({len(str(item.value))} chars)")


if __name__ == "__main__":
    # This DELETES store keys. It ran on import with no __main__ guard, so
    # merely importing the module performed the migration -- and it offered no
    # way to see what it would do first.
    import argparse

    _ap = argparse.ArgumentParser(description=__doc__)
    _ap.add_argument("--dry-run", action="store_true",
                     help="print what would change; delete nothing")
    _ap.add_argument("--yes", action="store_true",
                     help="skip the confirmation prompt")
    _args = _ap.parse_args()
    DRY_RUN = _args.dry_run
    if not DRY_RUN and not _args.yes:
        _answer = input("This deletes store keys. Proceed? [y/N] ").strip().lower()
        if _answer not in ("y", "yes"):
            raise SystemExit("aborted")
    asyncio.run(main())
