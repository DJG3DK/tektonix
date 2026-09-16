"""Unit tests for the LLM router config-backed cost-estimation fallback
(agent/tools/model_rates.py) -- the path BudgetGuardMiddleware falls back to
now that the router's cost annotation doesn't survive streaming (see
budget_guard.py's module docstring). Uses the real router config on this
host (not a fixture) -- this module has no fixture-injection point by design
(a single process-lifetime cache), so these tests exercise the actual file
the system reads in production rather than mocking the one thing that would
make a bug in the real file invisible.
"""

import pytest

import agent.tools.model_rates as model_rates

CONFIG_INPUT = 0.00000132   # config.yaml model_info for "openrouter/deepseek/deepseek-v4-pro-0813"
CONFIG_OUTPUT = 0.00000396
# A plausible cached-input rate for the same model; the catalog fixtures below
# supply it, so it does not have to match anything real.
CONFIG_CACHE_READ = 0.00000026


@pytest.fixture(autouse=True)
def offline_catalog(request, monkeypatch):
    """Every test here gets a fresh rate table built from config.yaml alone,
    unless it opts out with @pytest.mark.real_fetchers (the one real network
    test) or installs its own catalog. Live rates now take precedence over
    config.yaml when OpenRouter is reachable, so a test that asserts the
    config figure must not be at the mercy of a price change."""
    monkeypatch.setattr(model_rates, "_rates", None)
    if "real_fetchers" in request.keywords:
        return
    monkeypatch.setattr(model_rates, "_fetch_catalog_rates", lambda: {})
    monkeypatch.setattr(model_rates, "_fetch_endpoint_rates", lambda model_id: None)


def test_estimate_cost_returns_zero_for_unknown_model():
    assert model_rates.estimate_cost("totally/made-up-model", 1000, 1000) == 0.0


def test_estimate_cost_returns_zero_for_none_model_name():
    assert model_rates.estimate_cost(None, 1000, 1000) == 0.0


def test_estimate_cost_computes_from_real_config_rates():
    # deepseek-v4-pro's real config rates: input_cost_per_token=0.00000132,
    # output_cost_per_token=0.00000396 (the router config's model_info for
    # "openrouter/deepseek/deepseek-v4-pro-0813").
    cost = model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 1000, 500)
    expected = 1000 * CONFIG_INPUT + 500 * CONFIG_OUTPUT
    assert cost == expected


def test_estimate_cost_scales_linearly_with_tokens():
    cost_1x = model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 1000, 1000)
    cost_2x = model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 2000, 2000)
    assert cost_2x == cost_1x * 2


def test_estimate_cost_zero_tokens_is_zero_cost():
    assert model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 0, 0) == 0.0


def test_rates_cache_populates_once_and_is_reused(monkeypatch):
    load_calls = []
    real_load = model_rates._load_rates

    def counting_load():
        load_calls.append(1)
        return real_load()

    monkeypatch.setattr(model_rates, "_load_rates", counting_load)

    model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 100, 100)
    model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 200, 200)
    model_rates.estimate_cost("anthropic/claude-haiku-4.5", 100, 100)

    assert len(load_calls) == 1, "config.yaml should only be parsed once, then cached for the process lifetime"


# ---------------------------------------------------------------------------
# cache-read-aware pricing: a long tool-calling conversation resends nearly
# the same prefix every turn, so most of a later call's "input tokens" are
# cache reads, billed at a steep discount off the base input rate -- treating
# all of them as full-price input (the old behavior) overestimated real cost
# by several times on exactly this shape of conversation. The network fetch
# to OpenRouter's pricing endpoint is monkeypatched here (not hit for real)
# to keep this deterministic and independent of network access.
# ---------------------------------------------------------------------------


def test_cache_read_tokens_billed_at_the_discounted_rate(monkeypatch):
    monkeypatch.setattr(model_rates, "_fetch_catalog_rates", lambda: {
        "deepseek/deepseek-v4-pro-0813": {"input": CONFIG_INPUT, "output": CONFIG_OUTPUT, "cache_read": CONFIG_CACHE_READ},
    })

    cost = model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 118294, 376, cache_read_tokens=117888)
    fresh_input = 118294 - 117888
    expected = fresh_input * CONFIG_INPUT + 117888 * CONFIG_CACHE_READ + 376 * CONFIG_OUTPUT
    assert cost == expected
    # Sanity check against the old (pre-fix) all-input-at-full-price
    # behavior -- the cache-aware cost must be substantially lower for this
    # shape of call, not a rounding-level difference.
    naive = 118294 * CONFIG_INPUT + 376 * CONFIG_OUTPUT
    assert cost < naive * 0.3


def test_cache_read_tokens_clamped_to_input_tokens(monkeypatch):
    monkeypatch.setattr(model_rates, "_fetch_catalog_rates", lambda: {
        "deepseek/deepseek-v4-pro-0813": {"input": CONFIG_INPUT, "output": CONFIG_OUTPUT, "cache_read": CONFIG_CACHE_READ},
    })

    # A malformed/inconsistent usage report (cache_read > input_tokens)
    # must never go negative on the "fresh" portion.
    cost = model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 100, 50, cache_read_tokens=9999)
    expected = 100 * CONFIG_CACHE_READ + 50 * CONFIG_OUTPUT
    assert cost == expected


def test_model_without_published_cache_rate_falls_back_to_full_input_price(monkeypatch):
    # no OpenRouter data for this model (the autouse fixture's empty catalog)

    cost = model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 1000, 500, cache_read_tokens=800)
    expected = 1000 * CONFIG_INPUT + 500 * CONFIG_OUTPUT  # cache tokens charged at full input rate, no discount
    assert cost == expected


@pytest.mark.real_fetchers
def test_real_openrouter_fetch_returns_a_well_formed_rate_table():
    """One real network call against OpenRouter's actual pricing endpoint,
    matching this project's own preference for verifying against real infra
    over mocking the one thing that would make a real-world format change
    invisible. Only checks shape/plausibility, not exact values (OpenRouter's
    published prices can change), so this doesn't get flaky over a routine
    price update.
    """
    rates = model_rates._fetch_catalog_rates()
    assert isinstance(rates, dict)
    if not rates:
        pytest.skip("OpenRouter pricing endpoint unreachable from this environment")
    assert "z-ai/glm-5.3" in rates
    glm = rates["z-ai/glm-5.3"]
    assert set(glm) == {"input", "output", "cache_read"}
    assert 0 < glm["cache_read"] <= glm["input"] < 0.00001  # plausible per-token dollar range, not a unit-mixup


# ---------------------------------------------------------------------------
# Live rates over config rates. The router is billed OpenRouter's usage.cost,
# not config.yaml's model_info, so when the two disagree the estimate must
# follow OpenRouter: on 2026-09-08 config.yaml carried $1.32/M for a model
# OpenRouter had repriced to $0.58/M.
# ---------------------------------------------------------------------------


def test_live_catalog_rate_takes_precedence_over_config(monkeypatch):
    monkeypatch.setattr(model_rates, "_fetch_catalog_rates", lambda: {
        "deepseek/deepseek-v4-pro-0813": {"input": 0.0000005, "output": 0.000001, "cache_read": 0.0000001},
    })
    cost = model_rates.estimate_cost("deepseek/deepseek-v4-pro-0813", 1000, 500, cache_read_tokens=400)
    assert cost == pytest.approx(600 * 0.0000005 + 400 * 0.0000001 + 500 * 0.000001)


def test_alias_gets_the_same_live_rates_as_its_raw_id(monkeypatch):
    monkeypatch.setattr(model_rates, "_fetch_catalog_rates", lambda: {
        "deepseek/deepseek-v4-pro-0813": {"input": 0.0000005, "output": 0.000001, "cache_read": 0.0000001},
    })
    rates = model_rates._load_rates()
    aliases = [k for k, v in rates.items() if v is rates["deepseek/deepseek-v4-pro-0813"] and k != "deepseek/deepseek-v4-pro-0813"]
    assert aliases, "the config alias pinned to glm-5.2 should share its rate entry"


def test_undated_pin_missing_from_catalog_is_resolved_through_endpoints(monkeypatch):
    """The 2026-09-08 miss: config pins qwen/qwen3.8-max, the catalog lists
    only qwen3.8-max-0902, and the cache-read discount silently vanished."""
    asked = []

    def endpoints(model_id):
        asked.append(model_id)
        return {"input": 0.000002, "output": 0.000006, "cache_read": 0.00000025}

    # A non-empty catalog that lacks the pinned id: the endpoints lookup runs.
    monkeypatch.setattr(model_rates, "_fetch_catalog_rates", lambda: {"some/other-model": {"input": 1e-6, "output": 2e-6, "cache_read": 1e-7}})
    monkeypatch.setattr(model_rates, "_fetch_endpoint_rates", endpoints)
    rates = model_rates._load_rates()
    assert "deepseek/deepseek-v4-pro-0813" in asked
    assert rates["deepseek/deepseek-v4-pro-0813"]["cache_read"] == 0.00000025


def test_unreachable_catalog_skips_endpoint_lookups_and_uses_config(monkeypatch):
    asked = []
    monkeypatch.setattr(model_rates, "_fetch_catalog_rates", lambda: {})
    monkeypatch.setattr(model_rates, "_fetch_endpoint_rates", lambda model_id: asked.append(model_id))
    rates = model_rates._load_rates()
    assert asked == [], "no catalog at all means OpenRouter is down; do not fan out one request per model"
    assert rates["deepseek/deepseek-v4-pro-0813"] == {"input": CONFIG_INPUT, "output": CONFIG_OUTPUT, "cache_read": CONFIG_INPUT}


@pytest.mark.real_fetchers  # stubs httpx itself, so the real fetcher must stay in place
def test_endpoint_rates_take_the_dearest_provider():
    payload = {"data": {"endpoints": [
        {"pricing": {"prompt": "0.000001", "completion": "0.000004", "input_cache_read": "0.0000001"}},
        {"pricing": {"prompt": "0.000002", "completion": "0.000003"}},  # no cache pricing: cache reads at input rate
    ]}}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return payload

    import httpx
    real_get = httpx.get
    httpx.get = lambda url, timeout=10: FakeResp()
    try:
        rates = model_rates._fetch_endpoint_rates("vendor/model")
    finally:
        httpx.get = real_get
    assert rates == {"input": 0.000002, "output": 0.000004, "cache_read": 0.000002}


def test_rate_table_reloads_when_config_yaml_changes(monkeypatch, tmp_path):
    """Pins change from the Models page without an agent restart; a table
    cached at startup would price a repinned alias at the old model's rate."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model_list:\n  - model_name: agent-planner\n    params:\n      model: openrouter/vendor/old\n    model_info:\n      input_cost_per_token: 0.000001\n      output_cost_per_token: 0.000002\n")
    monkeypatch.setattr(model_rates, "ROUTER_CONFIG_PATH", cfg)
    monkeypatch.setattr(model_rates, "_rates", None)  # a fresh load records the mtime
    assert model_rates.estimate_cost("agent-planner", 1000, 0) == 1000 * 0.000001
    cfg.write_text("model_list:\n  - model_name: agent-planner\n    params:\n      model: openrouter/vendor/new\n    model_info:\n      input_cost_per_token: 0.000005\n      output_cost_per_token: 0.000002\n")
    import os
    os.utime(cfg, (cfg.stat().st_atime, cfg.stat().st_mtime + 5))
    assert model_rates.estimate_cost("agent-planner", 1000, 0) == 1000 * 0.000005, "repinned alias must be priced at the new model's rate"
    assert "vendor/new" in model_rates._table()


def test_a_legacy_litellm_params_entry_is_still_priced(tmp_path, monkeypatch):
    """The rate table has to keep reading an operator's pre-rename config.yaml
    -- see the same back-compat case in the router's own test_config.py."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model_list:\n"
        "  - model_name: agent-planner\n"
        "    litellm_params:\n"
        "      model: openrouter/z-ai/glm-5.3-flash\n"
        "    model_info:\n"
        "      input_cost_per_token: 0.0000001\n"
        "      output_cost_per_token: 0.0000002\n"
    )
    monkeypatch.setattr(model_rates, "ROUTER_CONFIG_PATH", cfg)
    monkeypatch.setattr(model_rates, "_rates", None)
    assert model_rates.estimate_cost("agent-planner", 1000, 0) == 1000 * 0.0000001
    assert "z-ai/glm-5.3-flash" in model_rates._table()
