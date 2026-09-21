"""The one place an episode is written.

An episode is the structured record of how a task ended: the goal, the
outcome, what it cost, and -- when it went wrong -- why. It is not loaded
into any task's context. It is read by the consolidation agent, which
distills patterns across episodes into the semantic memory that IS loaded.

This module exists because that 25-line writer was about to be edited by
three separate pieces of work at once: one wanting an embedding digest on
the record, one wanting the record handed to a search index as it is
written, one wanting extra telemetry fields on it. Three edits to one small
function in three commits is three chances to lose one of them in a merge,
and the thing being merged is the only durable record of what this system
has done.

A word of warning for whoever adds the first of those, because it is not
visible from here. The write goes through StoreBackend.awrite, which stores
a FileData document -- content, encoding, and two timestamps -- and rebuilds
that document from scratch on the way in. Any key added to the record that
is not part of that shape is dropped silently on write, and the readers on
the other side (consolidation.py's _read_episode) parse the content field
back out again. So an extra top-level key is not a one-line change: it
changes the stored value's shape, and the reader has to move in the same
commit as the writer.
"""

from __future__ import annotations

import json
import uuid

from deepagents.backends import StoreBackend
from langgraph.store.base import BaseStore

from agent.config import Config
from agent.deep_agent import EPISODES_ROUTE, episodes_namespace


async def write_episode(store: BaseStore, config: Config, repo: str, record: dict) -> str:
    """Append-only: each episode gets its own timestamped key, never
    overwritten. namespace is (episodes, repo) -- shared across every task
    for this project, so the consolidation agent can list and read them all
    with one backend call.

    Returns the key it wrote, which is what a caller needs to refer to this
    episode later without guessing at the key shape.

    `config` is unused today and is in the signature on purpose: the index
    hook and the embedding digest both need it, and agreeing the signature
    once is the entire point of this module. A later commit fills it in
    without touching a single call site.
    """
    backend = StoreBackend(namespace=episodes_namespace(repo), store=store)
    path = f"{EPISODES_ROUTE}{record['timestamp']}-{uuid.uuid4().hex[:8]}.json"
    await backend.awrite(path, json.dumps(record, indent=2))
    return path
