"""The fastest providers of each pinned model, kept current.

93-99% of an agent task is spent waiting on the model (measured on SWE-bench
tasks, 2026-09-24), and one model runs at very different speeds on different
hosts: 9 tok/s on one provider, 104 on another, for the same weights. Unasked,
OpenRouter balances by price. The operator controls cost by the MODEL they
pin; which host serves it is ours to choose, and we choose speed.

OpenRouter's own `sort: "throughput"` is not used for this: on 2026-09-24 it
sent a model to a provider its own endpoint stats listed at a quarter of the
speed of the top three. So the router ranks the endpoints itself, from those
stats (`/models/<model>/endpoints`, last 30 minutes), and names them in order
with fallbacks allowed -- the fast hosts' shared pools do throw 429s, and the
next one on the list is the answer to that, not the slowest.

Never on the request path: a call uses whatever ranking is cached, and a stale
or missing one is refreshed in the background. Until the first ranking lands,
build_body's `sort: "throughput"` default stands in.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger("model-router")

ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"
REFRESH_S = 600
TOP_N = 6
# A typical agent answer, for the time estimate: the delay before the first
# token plus this many tokens at the endpoint's median speed.
TYPICAL_OUTPUT_TOKENS = 1000
MIN_UPTIME_PCT = 98.0


def _p50(stat) -> float | None:
    if isinstance(stat, dict):
        v = stat.get("p50")
        return float(v) if isinstance(v, (int, float)) and v > 0 else None
    return None


def rank(endpoints: list[dict], ignore: list[str] | None = None) -> list[str]:
    """Endpoint tags, fastest first, for a typical answer.

    Out: anything OpenRouter marks degraded (status < 0), under 98% uptime,
    without speed stats, or in the deployment's `ignore` list."""
    ignored = {s.lower() for s in (ignore or [])}
    scored = []
    for e in endpoints:
        tag = e.get("tag") or ""
        slug = tag.split("/")[0].lower()
        name = (e.get("provider_name") or "").lower()
        if not tag or slug in ignored or name in ignored or tag.lower() in ignored:
            continue
        if (e.get("status") or 0) < 0:
            continue
        uptime = e.get("uptime_last_30m")
        if isinstance(uptime, (int, float)) and uptime < MIN_UPTIME_PCT:
            continue
        tp, lat_ms = _p50(e.get("throughput_last_30m")), _p50(e.get("latency_last_30m"))
        if tp is None or lat_ms is None:
            continue
        scored.append((lat_ms / 1000 + TYPICAL_OUTPUT_TOKENS / tp, tag))
    scored.sort()
    out: list[str] = []
    for _, tag in scored:
        if tag not in out:
            out.append(tag)
    return out[:TOP_N]


class FastestProviders:
    def __init__(self) -> None:
        # Keyed by model AND its ignore list: two deployments of one model
        # can exclude different hosts.
        self._order: dict[tuple, list[str]] = {}
        self._fetched: dict[tuple, float] = {}
        self._refreshing: set[tuple] = set()
        self._tasks: set[asyncio.Task] = set()     # held, or a pending refresh can be collected

    def extra_body_for(self, client: httpx.AsyncClient | None, api_key: str,
                       model: str, extra_body: dict) -> dict:
        """The deployment's extra_body with the fastest providers named first.

        A deployment that already says how to choose (`order`, `only` or
        `sort`) is left exactly as configured."""
        provider = extra_body.get("provider") if isinstance(extra_body.get("provider"), dict) else {}
        if any(k in provider for k in ("order", "only", "sort")):
            return extra_body
        ignore = tuple(sorted(provider.get("ignore") or []))
        key = (model, ignore)
        if client is not None and time.monotonic() - self._fetched.get(key, -1e9) > REFRESH_S \
                and key not in self._refreshing:
            self._refreshing.add(key)
            task = asyncio.get_running_loop().create_task(self._refresh(client, api_key, key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        order = self._order.get(key)
        if not order:
            return extra_body
        return {**extra_body, "provider": {**provider, "order": order, "allow_fallbacks": True}}

    async def _refresh(self, client: httpx.AsyncClient, api_key: str, key: tuple) -> None:
        model, ignore = key
        try:
            r = await client.get(ENDPOINTS_URL.format(model=model),
                                 headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
            r.raise_for_status()
            order = rank((r.json().get("data") or {}).get("endpoints") or [], list(ignore))
            if order:
                if order != self._order.get(key):
                    logger.info("fastest providers for %s: %s", model, ", ".join(order))
                self._order[key] = order
        except Exception as e:  # noqa: BLE001 -- a stats outage must never touch a call
            logger.warning("provider ranking for %s not refreshed: %s", model, e)
        finally:
            # Also after a failure: retry on the next refresh, not on every call.
            self._fetched[key] = time.monotonic()
            self._refreshing.discard(key)

    def snapshot(self) -> dict[str, list[str]]:
        return {m + (f" (ignore {', '.join(i)})" if i else ""): o for (m, i), o in self._order.items()}
