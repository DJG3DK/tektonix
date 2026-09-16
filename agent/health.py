"""What "up" means for the agent process, without asking a model anything.

Telegram tells an operator something happened. This answers a different
question, asked by a human at 3am or by a second box: is this process able to
do its job right now, and if not, which dependency is missing? Every check is
cheap, local and side-effect free, so it is safe to poll.

Deliberately NOT a model call. A health check that spends money, or that goes
red because a provider is slow, teaches people to ignore it.

Nothing here returns a secret's value -- only whether one is configured. The
endpoint is public (a monitoring box has no session), so every field has to be
safe to read from outside.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time

import httpx

logger = logging.getLogger("tektonix")

SANDBOX_IMAGE = "tektonix-sandbox:latest"
_TIMEOUT_S = 5

# Sandbox-image lookup shells out to docker, so it is cached: a monitoring box
# polling every 15s should not fork a process every time. Short enough that a
# rebuilt or pruned image is noticed within a minute.
_IMAGE_CACHE_TTL_S = 60
_image_cache: tuple[float, bool] | None = None


async def _check_postgres(pool) -> dict:
    """The checkpointer/store pool actually answering, not just configured.
    Without this the agent can serve its own dashboard while every task,
    session and memory read fails."""
    if pool is None:
        return {"ok": False, "detail": "no pool on app.state (still starting up?)"}
    try:
        async with asyncio.timeout(_TIMEOUT_S):
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 -- the reason is the payload
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}"}


def router_liveness_url(base_url: str) -> str:
    """MODEL_ROUTER_URL points at the OpenAI-compatible API root, which is
    ".../v1" in this deployment. The router serves liveness at the server ROOT,
    so appending to the configured value asks for /v1/health/liveliness and
    gets a 404 -- which the first run of this health check found immediately.
    Take the origin and ignore the path."""
    from urllib.parse import urlsplit
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}/health/liveliness"


async def _check_router(base_url: str) -> dict:
    """The router's liveness route: no key, no model call, no spend."""
    url = router_liveness_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            r = await client.get(url)
        if r.status_code == 200:
            return {"ok": True}
        return {"ok": False, "detail": f"HTTP {r.status_code} from {url}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:160]}"}


def _image_present() -> bool:
    r = subprocess.run(["docker", "image", "inspect", SANDBOX_IMAGE],
                       capture_output=True, timeout=_TIMEOUT_S)
    return r.returncode == 0


async def _check_sandbox_image() -> dict:
    """Every bash call the coder makes runs in this image. Missing, and tasks
    fail one tool call in, after the planning spend."""
    global _image_cache
    now = time.monotonic()
    if _image_cache and now - _image_cache[0] < _IMAGE_CACHE_TTL_S:
        return {"ok": _image_cache[1], "detail": None if _image_cache[1] else f"{SANDBOX_IMAGE} not built (cached)"}
    try:
        present = await asyncio.to_thread(_image_present)
    except Exception as e:  # noqa: BLE001 -- docker missing or not answering
        return {"ok": False, "detail": f"docker unavailable: {type(e).__name__}: {str(e)[:120]}"}
    _image_cache = (now, present)
    return {"ok": present, "detail": None if present else f"{SANDBOX_IMAGE} not built -- run docker/agent-sandbox/build.sh"}


def _check_review_secret() -> dict:
    """Configured, never echoed. Unset means every merge is refused at the
    end of a task that has already been paid for."""
    if os.environ.get("REVIEW_CONTROL_SECRET"):
        return {"ok": True}
    return {"ok": False, "detail": "REVIEW_CONTROL_SECRET unset: merge and deploy calls will be refused"}


async def collect(pool, router_base_url: str, projects: dict) -> dict:
    """Every check, concurrently. Returns the payload and whether it is ok."""
    postgres, router, sandbox = await asyncio.gather(
        _check_postgres(pool),
        _check_router(router_base_url),
        _check_sandbox_image(),
    )
    checks = {
        "postgres": postgres,
        "router": router,
        "sandbox_image": sandbox,
        "review_secret": _check_review_secret(),
    }
    return {
        "ok": all(c["ok"] for c in checks.values()),
        "service": "tektonix",
        "checks": checks,
        # A COUNT, not the names. This route is unauthenticated so a
        # monitoring box can reach it, and "the names are already on every
        # authenticated page" was never an argument for publishing them to
        # everyone else: a private repo's name is the one thing here that says
        # something about its owner rather than about this process. The count
        # answers the only operational question the payload needs to -- is
        # anything onboarded at all -- and says nothing about what.
        "project_count": len(projects),
    }
