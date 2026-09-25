"""Give the episodes that already exist a vector.

Episodes are embedded as they are written from now on -- agent/episodes.py
puts an `embed_text` key on the record and langgraph's store turns it into a
row in store_vectors. Everything written before that code existed has no
such key, which means the semantic leg of episode recall can see none of the
corpus it was built for. This is the one-time pass that fixes that.

    .venv/bin/python scripts/backfill_episode_embeddings.py            # dry run
    .venv/bin/python scripts/backfill_episode_embeddings.py --apply
    .venv/bin/python scripts/backfill_episode_embeddings.py --apply myproject

With no project named, every project in PROJECTS.

Dry run by default, like every other migration script here, and the dry run
is the useful mode: it prints how many episodes would be embedded and how
many characters that is, which is the number to look at before spending
anything.

Idempotent, and it has to be: langgraph has no change detection, so a
re-put of identical text pays for the embedding a second time. Every item
already carrying an `embed_digest` that matches the text this run would
produce is skipped without a request. A second run therefore embeds nothing
and reports a cost of zero, which is the idempotence claim and also where
it is checked.

Two things it is careful about, both learned from the design review rather
than from an incident:

* Key-ascending order, and the guarantee is PAGE-level, not row-level.
  Re-putting an episode bumps its `updated_at`. The writes go out a page at
  a time through one store.abatch, so a whole page of 32 shares a single
  `updated_at` and an `ORDER BY updated_at DESC` read is arbitrary INSIDE
  each page, however carefully ordered between them. (Measured after the
  live run: 146 episodes, 7 distinct `updated_at` values.) Nothing reads
  episodes that way -- agent/consolidation.py sorts them by KEY,
  deliberately -- so this costs nothing today. It is written down because the
  first version of this paragraph promised an ordering the batching never
  kept, and the next reader would have believed it.
* project_lock. Unlike the history-index backfill, this one writes into the
  store, where a running task is a genuine second writer. A live build
  blocks it, which is the correct failure.

Cost is reported from the router's own ledger -- the bytes routing.jsonl
grew by during this run -- and never from a rate table. That is the only
spend figure this system trusts, and an embedder that could not be billed
that way would have been a hole in it.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import embeddings, episode_vectors
from agent.config import PROJECTS, load_config
from agent.graph import open_store, project_lock
from agent.store_paging import all_items
from agent.tools.router_ledger import ROUTING_LOG_PATH

# One store.abatch per page, so a page of episodes is ONE embedding request:
# langgraph collects the vector values across a whole batch and asks for
# them together. Sized well under agent/embeddings.MAX_BATCH so the request
# body stays small and a failure throws away little.
PAGE = 32


def _pending(items) -> list[tuple[str, dict, dict]]:
    """(key, new value, record) for every episode that needs embedding.

    Sorted by key, which is what puts the oldest first -- an episode key
    starts with the ISO timestamp it was written at.
    """
    out = []
    for item in sorted(items, key=lambda i: i.key):
        value = dict(item.value or {})
        try:
            record = json.loads(value.get("content") or "{}")
        except (ValueError, TypeError):
            # A record this script cannot read is one it must not rewrite:
            # the stored content is the only copy.
            continue
        fields = episode_vectors.embed_fields(record)
        if not fields:
            continue
        if embeddings.unchanged(fields[episode_vectors.EMBED_FIELD],
                                value.get(episode_vectors.DIGEST_FIELD)):
            continue
        out.append((item.key, {**value, **fields}, record))
    return out


async def _dry_run(repo: str, store) -> int:
    items = await all_items(store, episode_vectors.episodes_namespace(repo))
    pending = _pending(items)
    chars = sum(len(v[episode_vectors.EMBED_FIELD]) for _, v, _ in pending)
    print(f"[embed] {repo}: {len(items)} episode(s), {len(pending)} to embed, "
          f"{chars:,} characters (dry run)")
    return len(pending)


async def _apply(repo: str, store, config) -> int:
    from langgraph.store.base import PutOp  # noqa: PLC0415 -- a script-only path

    namespace = episode_vectors.episodes_namespace(repo)
    items = await all_items(store, namespace)
    pending = _pending(items)
    written = 0
    async with project_lock(repo, config.dsn):
        for start in range(0, len(pending), PAGE):
            page = pending[start:start + PAGE]
            await store.abatch([PutOp(namespace, key, value) for key, value, _ in page])
            written += len(page)
    print(f"[embed] {repo}: {len(items)} episode(s), {written} embedded, "
          f"{len(items) - len(pending)} already current")
    return written


def _billed(alias: str, since_bytes: int) -> tuple[float, int]:
    """What the router billed for this run: (cost, calls).

    Read as the tail the ledger grew by rather than filtered by time, so a
    clock that disagrees with the file cannot make the figure wrong. Other
    callers' lines may land in the same window; the alias filter is what
    keeps them out.
    """
    try:
        with open(ROUTING_LOG_PATH, "rb") as f:
            f.seek(since_bytes)
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return 0.0, 0
    cost = 0.0
    calls = 0
    for line in tail.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("alias") != alias:
            continue
        calls += 1
        cost += float(entry.get("cost") or 0.0)
    return cost, calls


def _ledger_size() -> int:
    try:
        return ROUTING_LOG_PATH.stat().st_size
    except OSError:
        return 0


async def main(repos: list[str], apply: bool) -> int:
    """Returns the number of projects that failed, for the exit code."""
    config = load_config()
    if apply and not embeddings.available(config):
        print("[embed] this installation cannot embed anything -- run "
              ".venv/bin/python scripts/doctor.py to see which prerequisite is missing")
        return 1
    failures: list[str] = []
    before = _ledger_size()
    total = 0
    async with open_store(config) as store:
        if apply and getattr(store, "index_config", None) is None:
            # Without it every put below would write the key and no vector,
            # and the run would report success having stored nothing
            # searchable.
            print("[embed] the store opened with no vector index -- nothing was embedded")
            return 1
        for repo in repos:
            try:
                total += await (_apply(repo, store, config) if apply else _dry_run(repo, store))
            except Exception as e:  # noqa: BLE001 -- one project must not abort the rest
                print(f"[embed] {repo}: FAILED -- {e}")
                failures.append(repo)
    if apply:
        cost, calls = _billed(config.embedding_alias, before)
        print(f"[embed] {total} episode(s) embedded across {len(repos)} project(s); "
              f"the router billed ${cost:.6f} over {calls} call(s) "
              f"({ROUTING_LOG_PATH})")
    if failures:
        print(f"[embed] {len(failures)} of {len(repos)} project(s) FAILED: {failures}")
    return len(failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repos", nargs="*", help="projects to embed (default: all)")
    parser.add_argument("--apply", action="store_true",
                        help="actually write; without it this only reports what it would write")
    args = parser.parse_args()
    sys.exit(1 if asyncio.run(main(args.repos or list(PROJECTS), args.apply)) else 0)
