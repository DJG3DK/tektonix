"""One line per model call, in the schema the rest of the system already reads.

This file is load-bearing in three places, so its shape is a contract rather
than a convenience:

  * agent/tools/router_ledger.py reads `cost` back by `call_id` -- that is how
    BudgetGuard charges a task the REAL billed figure instead of its own
    estimate, and why a killed pass no longer loses the money it spent.
  * agent/metrics.py builds the whole Analytics page from it, keying the role
    off `alias` and the model off `requested_model`/`routed_model`.
  * agent/tools/model_rates.py falls back to it when the catalog is unreachable.

So the field names below are copied from what the previous writer used, not
chosen. Three fields are new and additive: `provider` (which backend OpenRouter
actually used), `attempt` (which link in a fallback chain answered) and
`caller` (which credential made the call, once the router started issuing a
key per consumer instead of one shared master key). Additive because the three
readers above key off names they already know; an unknown extra field is
ignored by all of them, which is what made adding these safe.

Costs are OpenRouter's own `usage.cost`, never computed from the rate table in
config.yaml. The estimate and the bill disagree -- that is the entire reason
this ledger exists.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("model-router")

LOG_PATH = Path(
    os.environ.get("MODEL_ROUTER_LEDGER")
    or str(Path(__file__).resolve().parents[1] / "logs" / "routing.jsonl")
)
MAX_BYTES = int(os.environ.get("MODEL_ROUTER_LEDGER_MAX_BYTES", 50_000_000))
_KEEP_FRACTION = 0.5

_lock = threading.Lock()


def record(
    *,
    call_id: str,
    alias: str,
    model: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cached_tokens: int | None = None,
    cost: float | None = None,
    duration_s: float | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    provider: str | None = None,
    attempt: int = 1,
    caller: str | None = None,
    error: bool = False,
    error_detail: str | None = None,
    path: Path | None = None,
) -> None:
    """Append one call. Never raises: a ledger write must not be able to fail
    the request it is describing."""
    entry = {
        "ts": time.time(),
        "call_id": call_id,
        # Both carry the underlying model, and `alias` carries the role. The
        # old writer crossed these -- requested_model held the RESOLVED
        # deployment and routed_model whatever came back -- which left most
        # lines unattributable to a role (see agent/metrics.py's own note).
        "requested_model": model,
        "routed_model": model,
        "alias": alias,
        "tier": None,              # kept: metrics and older tooling read it
        "cause": None,
        "matched_keyword": None,
        "classifier_model": None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "task_id": task_id,
        "session_id": session_id,
        "cost": cost,
        "duration_s": duration_s,
        # New, additive.
        "provider": provider,
        "attempt": attempt,
        "caller": caller,
    }
    if error:
        entry["error"] = True
        entry["error_detail"] = (error_detail or "")[:400] or None

    target = path or LOG_PATH
    try:
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a") as f:
                f.write(json.dumps(entry) + "\n")
            _trim(target)
    except Exception as e:  # noqa: BLE001
        logger.debug("ledger write failed: %s", e)


def _trim(path: Path) -> None:
    """Halve the file past the cap, keeping the newest lines.

    Size-trimmed rather than rotated because every reader globs one path, and
    losing the oldest lines costs a shorter window -- never correctness.
    """
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        data = path.read_bytes()
        keep = data[int(len(data) * (1 - _KEEP_FRACTION)):]
        keep = keep[keep.find(b"\n") + 1:] if b"\n" in keep else b""
        path.write_bytes(keep)
    except Exception as e:  # noqa: BLE001
        logger.debug("ledger trim failed: %s", e)
