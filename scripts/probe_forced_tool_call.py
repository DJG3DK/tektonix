#!/usr/bin/env python3
"""Probe OpenRouter models for FORCED tool-call support in thinking mode.

Why this exists: OpenRouter's catalog cannot answer the question. `qwen3.8-max`
advertises tools, tool_choice, structured_outputs, response_format AND reasoning
-- byte-identical to gemini-3.1-pro-preview, which works -- yet it fails with
"The tool_choice parameter does not support being set to required or object in
thinking mode". `supported_parameters` is a flat union across providers, so it
can say "supports tool_choice" and "supports reasoning" while the COMBINATION is
refused. Per-endpoint data claims the same. Only a real request settles it, and
that silent gap cost months of nightly memory consolidation.

So: send the actual shape a forced-tool-call role sends -- tools plus an object
tool_choice, with reasoning enabled -- and record what happens.

Writes data/forced_tool_call_probe.json, which the Models page reads to filter
the dropdown for roles that force a tool call.

CAVEAT, learned the hard way: compliance is PER-PROVIDER, and OpenRouter
load-balances, so the same model can pass one run and fail the next depending on
who answers. Observed directly -- z-ai/glm-5.2 returned 200 with the forced call
silently ignored on one provider, then passed on Phala minutes later; the same
model works reliably through our own router only because that pin excludes a
known-bad provider. So treat this cache as a strong filter, not a guarantee:
"ok" means at least one provider honoured it, "fail" means the one we drew did
not. For a role that must not break, pin the provider explicitly in the router
(see the extra_body provider settings in model-router/config.yaml).

  python scripts/probe_forced_tool_call.py --limit 25          # cheap sample
  python scripts/probe_forced_tool_call.py --all               # full sweep
  python scripts/probe_forced_tool_call.py --models a/b,c/d    # specific ones
"""
import sys
from pathlib import Path

# The only script here that lacked this: without it `python scripts/...` dies
# with ModuleNotFoundError: No module named 'agent', including when the
# dashboard's "Refresh model list" button spawns it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse, asyncio, json, os, sys, time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from agent import paths

load_dotenv(paths.SERVICES_DIR / "model-router" / ".env")
KEY = os.environ.get("OPENROUTER_API_KEY")
if not KEY:
    sys.exit(f"OPENROUTER_API_KEY not found (looked in {paths.SERVICES_DIR / 'model-router' / '.env'})")

CATALOG = "https://openrouter.ai/api/v1/models"
CHAT = "https://openrouter.ai/api/v1/chat/completions"
OUT = Path(__file__).resolve().parent.parent / "data" / "forced_tool_call_probe.json"

TOOL = {
    "type": "function",
    "function": {
        "name": "record_result",
        "description": "Record the structured result.",
        "parameters": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "description": "always true"},
                "note": {"type": "string", "description": "a short note"},
            },
            "required": ["ok", "note"],
        },
    },
}

REQUIRED_PARAMS = {"tools", "tool_choice", "structured_outputs"}


async def fetch_catalog(client):
    r = await client.get(CATALOG, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


async def probe(client, model_id, sem):
    """One real forced-tool-call request. Returns (status, detail)."""
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Call record_result with ok=true and note='probe'."}],
        "tools": [TOOL],
        # The object form -- the exact thing Alibaba rejects in thinking mode.
        "tool_choice": {"type": "function", "function": {"name": "record_result"}},
        "reasoning": {"effort": "low"},          # engage thinking where supported
        "max_tokens": 200,
        # Only route to providers that support every parameter above; without
        # this a provider can accept the request and fail deeper in.
        "provider": {"require_parameters": True},
    }
    async with sem:
        try:
            r = await client.post(
                CHAT, json=body,
                headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                timeout=90,
            )
        except Exception as e:
            return "error", f"{type(e).__name__}: {e}"[:180]

    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except Exception:
            msg = r.text
        msg = str(msg)
        low = msg.lower()
        # Not every non-200 means "cannot do a forced tool call". Lumping them
        # together produces false negatives that permanently hide a good model
        # from the picker -- a 429 is a busy provider, not an incompatibility.
        if r.status_code == 429 or "rate limit" in low:
            return "transient", f"HTTP {r.status_code}: rate limited — re-probe"
        if "batch api" in low:
            return "unavailable", "batch-only model, no sync endpoint"
        if r.status_code == 403:
            return "unavailable", f"requires account action: {msg[:120]}"
        if "no endpoints" in low and "tool_choice" not in low:
            return "unavailable", f"no serving endpoint: {msg[:120]}"
        return "fail", f"HTTP {r.status_code}: {msg[:180]}"

    try:
        data = r.json()
        choice = data["choices"][0]["message"]
        calls = choice.get("tool_calls") or []
        prov_used = data.get("provider") or "?"
        if not calls:
            # Worse than an error: the provider accepted the forced call and
            # silently ignored it. Record WHICH provider, because compliance is
            # provider-dependent -- glm-5.2 fails here on default routing but
            # works through our router, which pins away from a bad provider.
            return "fail", f"200 but no tool_call returned (provider={prov_used})"
        return "ok", f"tool_call ok (provider={prov_used})"
    except Exception as e:
        return "fail", f"unparseable response: {e}"[:180]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="probe every eligible model")
    ap.add_argument("--limit", type=int, default=0, help="probe only the N cheapest eligible")
    ap.add_argument("--models", type=str, default="", help="comma-separated model ids")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--retry-transient", action="store_true",
                    help="re-probe only models previously marked transient/error")
    args = ap.parse_args()

    async with httpx.AsyncClient() as client:
        catalog = await fetch_catalog(client)
        by_id = {m["id"]: m for m in catalog}

        if args.retry_transient:
            cached = {}
            if OUT.exists():
                cached = json.loads(OUT.read_text()).get("models", {})
            targets = [m for m, v in cached.items() if v.get("status") in ("transient", "error")]
            if not targets:
                sys.exit("nothing marked transient/error to retry")
        elif args.models:
            targets = [t.strip() for t in args.models.split(",") if t.strip()]
        else:
            eligible = [
                m for m in catalog
                if REQUIRED_PARAMS <= set(m.get("supported_parameters") or [])
                and "reasoning" in set(m.get("supported_parameters") or [])
            ]
            eligible.sort(key=lambda m: float(m.get("pricing", {}).get("prompt") or 0))
            if args.limit:
                eligible = eligible[: args.limit]
            elif not args.all:
                sys.exit("Pass --all, --limit N, or --models a/b")
            targets = [m["id"] for m in eligible]

        print(f"Probing {len(targets)} model(s), concurrency={args.concurrency}\n", flush=True)
        sem = asyncio.Semaphore(args.concurrency)
        results = {}
        done = 0

        async def run(mid):
            nonlocal done
            status, detail = await probe(client, mid, sem)
            done += 1
            mark = {"ok": "OK  ", "fail": "FAIL", "error": "ERR ",
                    "transient": "RETRY", "unavailable": "N/A "}.get(status, "?   ")
            print(f"  [{done:>3}/{len(targets)}] {mark} {mid:<48} {detail[:70]}", flush=True)
            results[mid] = {
                "status": status,
                "detail": detail,
                "pricing": by_id.get(mid, {}).get("pricing", {}),
                "name": by_id.get(mid, {}).get("name"),
            }

        await asyncio.gather(*(run(m) for m in targets))

    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text()).get("models", {})
        except Exception:
            pass
    prev.update(results)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "probe": "tools + object tool_choice + reasoning, require_parameters=true",
         "models": prev}, indent=2))

    from collections import Counter
    tally = Counter(v["status"] for v in results.values())
    print(f"\n  ok={tally['ok']}  incompatible={tally['fail']}  "
          f"unavailable={tally['unavailable']}  retry={tally['transient']}  error={tally['error']}")
    print(f"  Cache: {OUT} ({len(prev)} models total)")
    if tally["transient"]:
        print(f"  {tally['transient']} model(s) were rate-limited — re-run with --retry-transient")

asyncio.run(main())
