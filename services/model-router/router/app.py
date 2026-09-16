"""The router's HTTP surface.

Deliberately small. Three routes are all anything in this deployment calls:

  POST /v1/chat/completions   every model call, from four services
  GET  /health/liveliness     agent/health.py, unauthenticated by design
  GET  /v1/model/info         the deployment table, for tooling

The compatibility contract is not a matter of taste -- each item below is
something that breaks a specific caller if it changes:

  * `x-router-call-id` on every response. agent/middleware/budget_guard.py
    reads that exact header name (CALL_ID_HEADER) to match a model call to its
    billed cost in the ledger. Renaming it silently reverts every task to
    estimated spend.
  * `metadata.agent_task_id` / `agent_session_id` in the request body, put
    there by deep_agent._call_metadata, recorded and NOT forwarded upstream.
    Without them the ledger can price a call but never total a task.
  * Bearer auth against the same master key the proxy used.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from router import ledger, upstream
from router import stats as stats_mod
from router.config import Registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("model-router")

MASTER_KEY = os.environ.get("MODEL_ROUTER_KEY") or ""


def _parse_consumer_keys(raw: str) -> dict[str, str]:
    """`label=key,label=key` -> {key: label}.

    One key per caller, replacing a single shared master key. Without
    it every consumer on the box -- the mail agent, the demo bot, the trading
    gate -- would have to hold a credential that can call any alias, and
    revoking one would mean rotating all of them.

    Keyed BY THE SECRET so a lookup is one dict hit and never a loop that
    leaks timing per configured label.
    """
    out: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        label, key = part.split("=", 1)
        label, key = label.strip(), key.strip()
        if label and key:
            out[key] = label
    return out


CONSUMER_KEYS = _parse_consumer_keys(os.environ.get("MODEL_ROUTER_CONSUMER_KEYS", ""))
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# budget_guard matches on this by name (CALL_ID_HEADER); the two move together.
CALL_ID_HEADER = "x-router-call-id"
# Extra tries on the SAME deployment before falling back, for transient
# failures only (see upstream.is_transient).
RETRIES_PER_DEPLOYMENT = int(os.environ.get("MODEL_ROUTER_RETRIES", "2"))
BACKOFF_S = float(os.environ.get("MODEL_ROUTER_BACKOFF_S", "0.6"))

registry = Registry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pooled client for the process. Connection reuse is most of the
    # difference between a 200ms call and a 700ms one at this volume.
    app.state.http = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=40),
        headers={"HTTP-Referer": "https://tektonix.io", "X-Title": "Tektonix"},
    )
    logger.info("model-router up: %d deployments", len(registry.table.deployments))
    yield
    await app.state.http.aclose()


app = FastAPI(title="Tektonix model router", lifespan=lifespan)


def _authorise(authorization: str | None) -> str:
    """Returns the CALLER LABEL, which the ledger records.

    Unset master key still means an unguarded local dev run. A consumer key
    authorises exactly the same surface as the master key today -- the point
    of the split is attribution and independent revocation, not a narrower
    grant. Per-alias scoping can hang off the same label later if it is ever
    wanted.
    """
    if not MASTER_KEY and not CONSUMER_KEYS:
        return "dev"
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if MASTER_KEY and token == MASTER_KEY:
        return "master"
    label = CONSUMER_KEYS.get(token)
    if label:
        return label
    raise HTTPException(401, "invalid token")


@app.get("/health/liveliness")
async def liveliness():
    """Unauthenticated on purpose: agent/health.py polls it with no key and no
    model call, and a health probe that needs a secret is a health probe that
    stops being run."""
    t = registry.table
    return {"status": "alive", "deployments": len(t.deployments), "config_loaded_at": t.loaded_at}


@app.get("/health/readiness")
async def readiness():
    t = registry.table
    ok = bool(t.deployments) and bool(OPENROUTER_KEY)
    return JSONResponse({"status": "ready" if ok else "degraded",
                         "deployments": len(t.deployments),
                         "upstream_key": bool(OPENROUTER_KEY)},
                        status_code=200 if ok else 503)


@app.get("/v1/model/info")
async def model_info(authorization: str | None = Header(default=None)):
    _authorise(authorization)
    t = registry.table
    return {"data": [
        {"model_name": d.alias,
         "params": {"model": f"openrouter/{d.model}"},
         "model_info": {"input_cost_per_token": d.input_cost_per_token,
                        "output_cost_per_token": d.output_cost_per_token,
                        "timeout_s": d.timeout_s}}
        for d in t.deployments.values()]}


@app.get("/v1/stats")
async def stats(window_minutes: int = 60, authorization: str | None = Header(default=None)):
    """What the router has been doing, read back out of its own ledger.

    Per alias: call count, error rate, p50/p95/max latency, spend, cache hit
    rate, retry count, and which providers actually served it -- none of which
    was visible before without parsing the file by hand.
    """
    _authorise(authorization)
    window = max(1, min(int(window_minutes), 24 * 60)) * 60
    return stats_mod.summarise(ledger.LOG_PATH, window)


@app.get("/v1/models")
async def models(authorization: str | None = Header(default=None)):
    _authorise(authorization)
    t = registry.table
    return {"object": "list",
            "data": [{"id": a, "object": "model", "owned_by": "tektonix"} for a in t.deployments]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
    caller = _authorise(authorization)
    body = await request.json()
    alias = body.get("model")
    if not alias:
        raise HTTPException(400, "no model given")

    table = registry.table
    if alias not in table.deployments:
        raise HTTPException(404, f"unknown model {alias!r}")

    meta = body.get("metadata") or {}
    task_id = meta.get("agent_task_id")
    session_id = meta.get("agent_session_id")
    call_id = str(uuid.uuid4())
    client: httpx.AsyncClient = request.app.state.http

    if body.get("stream"):
        return await _streamed(client, table, alias, body, call_id, task_id, session_id, caller)

    # Buffered: walk the fallback chain, retrying TRANSIENT failures on each
    # deployment before moving on. Moving to a fallback on the first 429 throws
    # away the model the operator pinned because a provider asked us to wait a
    # moment -- and the fallback is, by definition, not their first choice.
    chain = table.chain(alias)
    last: upstream.Attempt | None = None
    attempt_no = 0
    for name in chain:
        dep = table.deployments[name]
        for retry in range(RETRIES_PER_DEPLOYMENT + 1):
            attempt_no += 1
            att = await upstream.call_once(client, OPENROUTER_KEY, body, dep.model,
                                           dep.extra_body, dep.timeout_s)
            att.alias = name
            last = att
            ledger.record(
                caller=caller,
                call_id=call_id, alias=alias, model=att.usage.model or dep.model,
                prompt_tokens=att.usage.prompt_tokens, completion_tokens=att.usage.completion_tokens,
                cached_tokens=att.usage.cached_tokens, cost=att.usage.cost,
                duration_s=att.duration_s, task_id=task_id, session_id=session_id,
                provider=att.usage.provider, attempt=attempt_no,
                error=not att.ok, error_detail=att.error,
            )
            if att.ok:
                headers = {CALL_ID_HEADER: call_id, "x-router-deployment": name,
                           "x-router-attempt": str(attempt_no)}
                return JSONResponse(att.payload, headers=headers)

            transient = upstream.is_transient(att.status, att.error)
            logger.warning("attempt %d on %s failed (%s, %s): %s", attempt_no, name,
                           att.status, "transient" if transient else "permanent", att.error)
            if not transient:
                break          # a 400 will fail the same way twice
            if retry < RETRIES_PER_DEPLOYMENT:
                # Short backoff with jitter. Long enough to clear a rate-limit
                # burst, short enough that a real outage still reaches the
                # fallback while the caller is waiting.
                await asyncio.sleep(BACKOFF_S * (2 ** retry) * (0.5 + random.random()))

    detail = last.error if last else "no deployment answered"
    return JSONResponse({"error": {"message": detail, "type": "upstream_error",
                                   "chain": chain, "call_id": call_id}},
                        status_code=502, headers={CALL_ID_HEADER: call_id})


async def _streamed(client, table, alias, body, call_id, task_id, session_id, caller):
    """Streaming, with the fallback chain available up to the first byte.

    The rule is not "streams cannot fall back" -- it is that a response becomes
    committed the moment a byte reaches the client. An upstream that refuses
    with a 429 does so BEFORE any of its body exists, and falling back there is
    as safe as it is for a buffered call. Only a failure part-way through a
    stream is unrecoverable, because retrying would splice two different
    completions into one response.

    So: walk the chain while nothing has been emitted, and the instant the
    first chunk goes out, commit to that deployment. The roles that stream here
    -- summarizer, cartographer, consolidator -- all have fallbacks configured
    precisely because their providers have thrown 429s before.
    """
    chain = table.chain(alias)
    started = time.monotonic()

    async def body_iter():
        attempt_no = 0
        for name in chain:
            dep = table.deployments[name]
            for retry in range(RETRIES_PER_DEPLOYMENT + 1):
                attempt_no += 1
                usage = upstream.Usage()
                emitted = False
                t0 = time.monotonic()
                try:
                    async for chunk in upstream.stream_once(client, OPENROUTER_KEY, body, dep.model,
                                                            dep.extra_body, dep.timeout_s, usage):
                        emitted = True
                        yield chunk
                except Exception as e:  # noqa: BLE001
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    ledger.record(caller=caller, call_id=call_id, alias=alias, model=dep.model,
                                  duration_s=time.monotonic() - t0, task_id=task_id,
                                  session_id=session_id, attempt=attempt_no, error=True,
                                  error_detail=f"{type(e).__name__}: {str(e)[:300]}")
                    if emitted:
                        # Committed. Splicing a second completion onto a partial
                        # one would be worse than the truncation.
                        logger.error("stream failed mid-body on %s, cannot retry: %s", name, e)
                        return
                    transient = upstream.is_transient(status, str(e))
                    logger.warning("stream attempt %d on %s failed before first byte (%s): %s",
                                   attempt_no, name, "transient" if transient else "permanent", e)
                    if not transient:
                        break               # move to the next deployment
                    if retry < RETRIES_PER_DEPLOYMENT:
                        await asyncio.sleep(BACKOFF_S * (2 ** retry) * (0.5 + random.random()))
                    continue
                ledger.record(
                    caller=caller,
                    call_id=call_id, alias=alias, model=usage.model or dep.model,
                    prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                    cached_tokens=usage.cached_tokens, cost=usage.cost,
                    duration_s=time.monotonic() - started, task_id=task_id,
                    session_id=session_id, provider=usage.provider, attempt=attempt_no,
                )
                return
        logger.error("every deployment in %s failed before streaming", chain)

    return StreamingResponse(body_iter(), media_type="text/event-stream",
                             headers={CALL_ID_HEADER: call_id, "x-router-deployment": alias,
                                      "cache-control": "no-cache"})
