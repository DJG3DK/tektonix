"""One line per tool result, so tool reliability is ours to compute.

The Analytics page's tool panel read LangSmith's `run_type="tool"` runs. That
made an optional off-box service the only record of whether this agent's own
tools work -- and the tracing needed to produce those runs costs a core (see
agent/observability.py). The work node already sees every tool result as it
streams; writing a line per result is the cheap half of what tracing was
doing, with none of the payload.

Deliberately NOT a copy of the tool's output. The name, whether it succeeded,
the task, and the time are what a reliability panel needs; the output is the
part that carries secrets, costs bytes, and already exists in the task stream.

Append-only and size-trimmed like the router's own log, because this is
telemetry: losing the oldest lines costs a longer window, never correctness.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("tektonix")

LOG_PATH = Path(
    os.environ.get("AGENT_TOOL_EVENTS_LOG")
    or (Path(__file__).resolve().parents[1] / "logs" / "tool_events.jsonl")
)
MAX_BYTES = 5_000_000
_KEEP_FRACTION = 0.5
# A process that is not production -- a benchmark runner -- writes its events
# beside its own run instead (2026-09-26: one 50-task sample wrote 16,776
# rows into the production log, which trimmed away every production day
# before it and made the reliability chart one day wide).
_override: Path | None = None


def redirect(path: Path | None) -> None:
    """Send every event this process records to `path` (None: production's)."""
    global _override
    _override = Path(path) if path else None


def record(tool: str, ok: bool, task_id: str | None = None, repo: str | None = None,
           detail: str | None = None, nudge: str | None = None, path: Path | None = None) -> None:
    """Append one tool result. Never raises: telemetry must not be able to
    break the pass it is describing.

    `nudge` marks a call the harness pointed at a cheaper tool (see
    agent/tools/bash_advice.py). It is a FIELD rather than a tool name of its
    own: a flagged bash call is still one bash call, and writing it as
    "bash-as-read" both invented a tool nobody has and added a phantom call to
    the reliability panel's count.
    """
    target = path or _override or LOG_PATH
    entry = {
        "ts": time.time(),
        "tool": tool,
        "ok": bool(ok),
        "task_id": task_id,
        "repo": repo,
        "nudge": nudge or None,
        # A short reason when it failed -- enough to group failures, never the
        # output itself.
        "detail": (detail or "")[:200] or None,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _trim(target)
    except Exception as e:  # noqa: BLE001
        logger.debug("tool event not recorded: %s", e)


def _trim(path: Path) -> None:
    """Halve the file when it passes the cap, keeping the newest lines."""
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        with open(path, "rb") as f:
            data = f.read()
        keep = data[int(len(data) * (1 - _KEEP_FRACTION)):]
        keep = keep[keep.find(b"\n") + 1:] if b"\n" in keep else b""
        with open(path, "wb") as f:
            f.write(keep)
    except Exception as e:  # noqa: BLE001
        logger.debug("tool event log not trimmed: %s", e)
