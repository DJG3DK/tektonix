"""A planning session's transcript, kept where it survives the process.

Without this (until 2026-09-12) a session's scrollback lived in two places, both lossy.
The checkpoint holds the message list -- but SummarizationMiddleware rewrites
that list when a long turn compacts, discarding the earlier exchanges
wholesale. The in-memory buffer holds the full scrollback -- until the process
exits, which on this box has meant a pm2 memory kill, a deploy, or a crash.

So the one question an operator actually asks about a long turn -- "what is it
doing, and why does it look confused?" -- had no durable answer. Asked on
2026-09-12 of a 78-minute turn, 68 model calls in, the honest reply was that
the plumbing looked healthy and the conversation was unreadable: 413
checkpoints, every one with an empty messages channel, and a live buffer
belonging to a process that had been restarted.

This writes the translated entries -- the same shape the dashboard renders --
into the same Postgres store as everything else, so a turn can be read back
afterwards, from another process, after a restart, and after compaction has
thrown the model's own copy away.

Batched, not per-event: a busy turn publishes several entries a second, and a
store write each would be a write amplification for telemetry. Entries
accumulate and flush on a count or a deadline, plus once when the turn ends.

Capped per session. A transcript is for reading, and the tail is what gets
read; an unbounded one turns a stuck turn into an unbounded row.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("tektonix")

NAMESPACE = "planning_log"
TASK_NAMESPACE = "task_log"
# Build-task entries are trimmed harder than planning ones -- see Recorder.
TASK_DETAIL_CAP = 600

# Keep the newest N entries per session. A long HARD turn produces a few
# hundred; this is several turns' worth, and each entry is capped at ~2KB of
# detail by the translator that produced it.
MAX_ENTRIES = 2000

# Flush when either is reached, so a chatty turn writes on volume and a quiet
# one still lands its last entries promptly.
FLUSH_EVERY = 25
FLUSH_AFTER_S = 20.0


class Recorder:
    """Buffers a session's entries and writes them to the store.

    One per session, held by the server for as long as the session is being
    streamed. `add` is cheap and never touches the store; `flush` is the only
    thing that writes, and it swallows its own failures -- a transcript that
    cannot be written must not break the turn it is describing.
    """

    def __init__(self, repo: str, session_id: str, store, namespace: str = NAMESPACE,
                 detail_cap: int | None = None):
        self.repo = repo
        self.session_id = session_id
        self._store = store
        # Build tasks keep their transcript under their own namespace. Same
        # machinery, different drawer -- a task and a planning session can
        # share an id space without colliding, and either can be dropped
        # without touching the other.
        self.namespace = namespace
        # Build tasks are an order of magnitude chattier than planning turns
        # (754 tool calls on task 3ee0d030), so the durable copy trims each
        # entry's detail. The live view keeps the full text while the process
        # holds it; what survives a restart is the shape of the run, which is
        # what "what was it doing?" actually needs.
        self.detail_cap = detail_cap
        self._pending: list[dict] = []
        self._last_flush = time.monotonic()

    def add(self, entry: dict) -> bool:
        """Buffer one entry. Returns whether a flush is now due."""
        if not isinstance(entry, dict):
            return False
        if self.detail_cap is not None and isinstance(entry.get("detail"), str):
            detail = entry["detail"]
            if len(detail) > self.detail_cap:
                entry = {**entry, "detail": detail[:self.detail_cap] + " …[trimmed]"}
        self._pending.append(entry)
        return (len(self._pending) >= FLUSH_EVERY
                or time.monotonic() - self._last_flush >= FLUSH_AFTER_S)

    async def flush(self) -> int:
        """Append what is buffered. Returns how many entries were written."""
        if not self._pending or self._store is None:
            return 0
        batch, self._pending = self._pending, []
        self._last_flush = time.monotonic()
        try:
            existing = await _read(self._store, self.repo, self.session_id, self.namespace)
            if existing is None:
                # The read failed, which is NOT the same as "there is nothing
                # there". Writing the batch alone would replace the whole
                # transcript with its last few entries -- a transient store
                # hiccup silently eating the history it exists to keep. Put
                # the batch back and try again on the next flush.
                self._pending = batch + self._pending
                return 0
            entries = (existing + batch)[-MAX_ENTRIES:]
            await self._store.aput((self.namespace, self.repo), self.session_id,
                                   {"session_id": self.session_id, "entries": entries,
                                    "updated_at": time.time()})
            return len(batch)
        except Exception as e:  # noqa: BLE001 -- never break a turn over its own transcript
            logger.warning("planning transcript not written for %s: %s", self.session_id, e)
            return 0


async def _read(store, repo: str, session_id: str, namespace: str = NAMESPACE) -> list[dict] | None:
    """The stored entries, or None when they could not be read -- a
    distinction the writer depends on."""
    if store is None:
        return []
    try:
        item = await store.aget((namespace, repo), session_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("planning transcript unreadable for %s: %s", session_id, e)
        return None
    entries = (item.value or {}).get("entries") if item else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


async def load(store, repo: str, session_id: str, namespace: str = NAMESPACE) -> list[dict]:
    """The durable transcript, oldest first. Empty when there is none, and
    empty when it could not be read -- a reader wants a list either way."""
    return await _read(store, repo, session_id, namespace) or []


async def forget(store, repo: str, session_id: str, namespace: str = NAMESPACE) -> None:
    """Drop a session's transcript -- for an archived or deleted session, so
    the store does not keep what the operator asked to be rid of."""
    if store is None:
        return
    try:
        await store.adelete((namespace, repo), session_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("planning transcript not deleted for %s: %s", session_id, e)
