"""Minimal in-memory rate limiter for authentication endpoints (audit H-7).

No new dependency and no external store: a single-process FastAPI app with one
uvicorn worker (how this deploys) can hold attempt counters in a dict. A sliding
window per (client-ip, action) with a lockout once the threshold is exceeded.
Deliberately fails OPEN on its own errors -- a bug here must never lock every
user out of their own dashboard -- but that is the only case it permits through,
and it says so in the log every time (a limiter that silently stops limiting is
indistinguishable from one that works).

SINGLE PROCESS IS AN ASSUMPTION, not a detail. The counters live in this
process's memory: they reset on every restart (a deploy clears a lockout), and
with more than one uvicorn worker each worker would count on its own, so the
effective limit would multiply by the worker count. The app runs one worker
(pm2 on a host install, one uvicorn in the bundle). Raising that means moving
these counters into Postgres, or failing closed -- in the same change.

Keyed on the client IP. audit N-1: the IP is taken from X-Real-IP (which nginx
sets authoritatively from $remote_addr, OVERWRITING any client-supplied value),
not from the first hop of X-Forwarded-For -- nginx uses
$proxy_add_x_forwarded_for, which APPENDS the real peer, so the client-supplied
first hop is attacker-controlled and rotating it defeated the limiter entirely.
Falls back to the LAST hop of XFF (the one our own proxy appended) and finally
the socket peer. Not perfect against a botnet, but it turns the measured
"25 password guesses in a few seconds, all processed" into "5 then locked".
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import HTTPException, Request

logger = logging.getLogger("tektonix")

# action -> (max_attempts, window_seconds, lockout_seconds)
_LIMITS = {
    "login": (5, 60, 300),
    "verify-2fa": (5, 60, 300),
    "reset-password": (5, 300, 900),
    "reset-request": (3, 300, 900),
    # The approve link's POST (agent/routers/github.py): unauthenticated by design --
    # the token is the credential -- so it gets the same brake as a password
    # guess. Each token is single-use; this bounds how fast anyone can try.
    "github-approve": (10, 60, 300),
}

# (ip, action) -> list[timestamp]  and  (ip, action) -> locked_until
_attempts: dict[tuple[str, str], list[float]] = defaultdict(list)
_locked_until: dict[tuple[str, str], float] = {}


def client_ip(request: Request) -> str:
    # audit N-1: X-Real-IP is set by nginx to $remote_addr and overwrites any
    # header the client sent, so it is the real peer and cannot be spoofed
    # through the proxy. Prefer it.
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    # Fall back to the LAST hop of X-Forwarded-For -- with nginx's
    # $proxy_add_x_forwarded_for the real peer is appended LAST, so the last
    # entry is the one our proxy added (the first is client-controllable).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


_last_evict = 0.0
_EVICT_INTERVAL = 300  # sweep at most every 5 min


def _maybe_evict(now: float) -> None:
    # audit N-1: bound the dicts. Without this, rotating the IP key (the very
    # thing the spoofing fix prevents, but also legitimate IP churn over time)
    # grew _attempts / _locked_until without limit -- the same unbounded-growth
    # shape flagged elsewhere. Sweep expired entries opportunistically.
    global _last_evict
    if now - _last_evict < _EVICT_INTERVAL:
        return
    _last_evict = now
    for key in list(_attempts.keys()):
        action = key[1]
        limit = _LIMITS.get(action)
        window = limit[1] if limit else 900
        if not any(now - t < window for t in _attempts[key]):
            _attempts.pop(key, None)
    for key in list(_locked_until.keys()):
        if now >= _locked_until[key]:
            _locked_until.pop(key, None)


def check_rate_limit(request: Request, action: str) -> None:
    """Raise 429 if this (ip, action) is over its limit or locked out. Call at
    the TOP of the endpoint, before any expensive work."""
    try:
        limit = _LIMITS.get(action)
        if not limit:
            return
        max_attempts, window, lockout = limit
        ip = client_ip(request)
        key = (ip, action)
        now = time.time()
        _maybe_evict(now)

        locked = _locked_until.get(key)
        if locked and now < locked:
            raise HTTPException(429, f"too many attempts; try again in {int(locked - now)}s")

        recent = [t for t in _attempts[key] if now - t < window]
        if len(recent) >= max_attempts:
            _locked_until[key] = now + lockout
            _attempts[key] = []
            raise HTTPException(429, f"too many attempts; locked for {lockout}s")

        recent.append(now)
        _attempts[key] = recent
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 -- fail OPEN on an internal limiter bug
        logger.exception("rate limiter failed for %r -- request allowed through unlimited", action)
        return


def clear_rate_limit(request: Request, action: str) -> None:
    """Call on a SUCCESSFUL auth so a legitimate user's own earlier fat-finger
    attempts don't count toward a later lockout."""
    try:
        key = (client_ip(request), action)
        _attempts.pop(key, None)
        _locked_until.pop(key, None)
    except Exception:  # noqa: BLE001
        logger.exception("rate limiter could not clear %r", action)
        return
