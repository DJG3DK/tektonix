"""What this process is doing right now.

Five registries that were module globals in agent/server.py, and the reason
they are here instead: server.py is being split into agent/routers/ one seam
at a time, and a route module cannot import back into server.py without
making the import a cycle. Every one of these is read or written by routes
that have moved or are about to.

They are MUTATED, never rebound -- `running_tasks[tid] = task`, not
`running_tasks = {}` -- which is what makes a shared module work where
`app.state` would also have done. A rebound global would need a `global`
statement and would give each importer its own name for a different object;
a mutated dict is one object however many modules hold a reference.

Nothing in here survives a restart, and nothing in here is authoritative. The
store holds what a task IS; these hold what is happening to it in this
process, which is why an orphaned task (backend restarted mid-run) is
detected by a store row with no entry here rather than the other way round.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import WebSocket

    from agent import planning_log

# task_id -> list of subscriber queues, for fanning live updates out to every
# connected WS client (a reconnect or a second browser tab both just get a
# new queue and the same event stream from that point forward).
subscribers: dict[str, list[tuple[asyncio.Queue, WebSocket]]] = {}
running_tasks: dict[str, asyncio.Task] = {}

# Same fan-out pattern, kept in its own dicts (not reusing the task ones
# above) -- a planning session_id and a task_id are both plain strings with
# no shared namespace, and keeping them separate avoids ever having to
# reason about whether an id collision between the two is possible.
planning_subscribers: dict[str, list[tuple[asyncio.Queue, WebSocket]]] = {}
running_planning_turns: dict[str, asyncio.Task] = {}

# task_id -> the transcript recorder for the run in flight. Popped and
# flushed when the task ends; a recorder left here is a transcript nobody
# will write.
task_recorders: dict[str, planning_log.Recorder] = {}

# session_id -> the same, for a planning session being streamed. Here rather
# than in server.py for the same reason as the rest: the planning routes that
# pop it on delete live in agent/routers/planning.py.
planning_recorders: dict[str, planning_log.Recorder] = {}

# Fire-and-forget work that must not be garbage collected mid-run. asyncio
# keeps only a WEAK reference to a bare create_task, so a background refresh
# could vanish halfway and silently never happen.
background_tasks: set[asyncio.Task] = set()
