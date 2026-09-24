"""The router asks for the fastest host of the model the operator pinned.

93-99% of an agent task is waiting on the model, and the same weights ran at
9 tok/s on one host and 104 on another. OpenRouter's own "throughput" sort
picked a host its own stats put at a quarter of the top three, so the router
ranks the endpoints itself and names them in order.
"""

from __future__ import annotations

import asyncio

import httpx

from router import fastest
from router.upstream import build_body


def ep(tag, tp, lat_ms, status=0, uptime=99.9, name=None):
    return {"tag": tag, "provider_name": name or tag.split("/")[0].title(), "status": status,
            "uptime_last_30m": uptime, "throughput_last_30m": {"p50": tp}, "latency_last_30m": {"p50": lat_ms}}


def test_fastest_for_a_typical_answer_first_and_the_unhealthy_left_out():
    order = fastest.rank([
        ep("slow/fp4", 24, 2800),
        ep("together", 90, 1300),
        ep("coreweave/nvfp4", 112, 960),
        ep("quickstart", 39, 400),        # first token fast, then slow: 26 s for 1000 tokens
        ep("degraded", 300, 100, status=-2),
        ep("flaky", 300, 100, uptime=75),
        ep("nostats", None, 100),
        ep("venice", 500, 100),
    ], ignore=["Venice"])
    assert order == ["coreweave/nvfp4", "together", "quickstart", "slow/fp4"]


def test_only_the_top_few_are_named():
    assert len(fastest.rank([ep(f"p{i}", 10 + i, 500) for i in range(20)])) == fastest.TOP_N


def test_a_deployment_that_says_how_to_choose_is_left_alone():
    f = fastest.FastestProviders()
    for provider in ({"order": ["x"]}, {"only": ["x"]}, {"sort": "price"}):
        extra = {"provider": provider}
        assert f.extra_body_for(None, "k", "m", extra) is extra


def test_the_ranking_is_fetched_in_the_background_and_then_used():
    """No call waits on the stats: the first goes out with build_body's
    throughput default, later ones name the ranked hosts, with fallbacks --
    the fast hosts' shared pools do return 429s."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"endpoints": [ep("together", 90, 1300), ep("venice", 500, 50),
                                                                ep("baseten/fp8", 105, 600)]}})

    async def go():
        f = fastest.FastestProviders()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            extra = {"provider": {"require_parameters": True, "ignore": ["Venice"]}}
            first = f.extra_body_for(client, "k", "z/glm", extra)
            assert first is extra
            assert build_body({"model": "a"}, "z/glm", first)["provider"]["sort"] == "throughput"
            await asyncio.sleep(0.05)
            later = f.extra_body_for(client, "k", "z/glm", extra)
            f.extra_body_for(client, "k", "z/glm", extra)
        return later
    later = asyncio.run(go())
    assert later["provider"] == {"require_parameters": True, "ignore": ["Venice"],
                                 "order": ["baseten/fp8", "together"], "allow_fallbacks": True}
    assert len(seen) == 1 and seen[0].endswith("/models/z/glm/endpoints"), "refreshed once, not per call"
    body = build_body({"model": "a"}, "z/glm", later)
    assert "sort" not in body["provider"], "a named order is not overridden by the default"


def test_a_stats_outage_touches_no_call():
    async def go():
        f = fastest.FastestProviders()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
            extra = {"provider": {"require_parameters": True}}
            f.extra_body_for(client, "k", "m", extra)
            await asyncio.sleep(0.05)
            return f.extra_body_for(client, "k", "m", extra) is extra
    assert asyncio.run(go())


def test_a_host_that_runs_away_is_charged_for_it():
    """Fast on a typical answer, but 2% of its calls ran to the output cap:
    the expected call is slower than on a steadier, slower host."""
    eps = [ep("fast-but-flaky", 126, 480, name="Flaky"), ep("steady/fp8", 98, 1391, name="Steady")]
    assert fastest.rank(eps, model="m")[0] == "fast-but-flaky", "no evidence yet: speed decides"
    stats = {("m", "flaky"): (1639, 37), ("m", "steady"): (137, 0)}
    assert fastest.rank(eps, model="m", stats=stats) == ["steady/fp8", "fast-but-flaky"], "still listed, as a fallback"
    assert fastest.runaway_rate({("m", "new"): (1, 1)}, "m", "New") < 0.01, "one bad call does not condemn a new host"


def test_runaways_are_counted_from_the_router_s_own_ledger(tmp_path):
    import json
    import time
    now = time.time()
    rows = [
        {"ts": now, "routed_model": "m", "provider": "Flaky", "completion_tokens": 32768},
        {"ts": now, "routed_model": "m", "provider": "Flaky", "completion_tokens": 900},
        {"ts": now, "routed_model": "m", "provider": "Flaky", "completion_tokens": 131072, "error": True},
        {"ts": now - 30 * 86400, "routed_model": "m", "provider": "Flaky", "completion_tokens": 131072},
        {"ts": now, "routed_model": "m", "provider": "Steady", "completion_tokens": 400},
    ]
    path = tmp_path / "routing.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert fastest.ledger_stats(path, now) == {("m", "flaky"): (2, 1), ("m", "steady"): (1, 0)}
    assert fastest.ledger_stats(tmp_path / "missing.jsonl") == {}
