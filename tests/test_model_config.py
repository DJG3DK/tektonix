"""Unit tests for agent/model_config.py -- the dashboard's model-picker
backend. Strictly scoped to this agent's own agent-* roles (MANAGED_ROLES);
every other entry in llm-router/config.yaml (the shared tier system,
reasoning-tier, smart-router) belongs to the review service and
must never be read or written here.

Uses a real temp config file with the same shape as the production one
(comments, non-agent-* entries included), not a minimal fixture -- the
surgical text-replace logic is the whole point of this module, and a
too-simple fixture would miss exactly the kind of formatting quirk (a
comment line inside a params block, a differently-named neighboring entry)
that could silently corrupt the real file.
"""

import pytest

import agent.model_config as model_config

FAKE_CONFIG = """\
# Shared router config -- some entries belong to another service entirely.
model_list:
  - model_name: glm-5.2
    params:
      model: openrouter/z-ai/glm-5.2
      api_key: os.environ/OPENROUTER_API_KEY
    model_info:
      input_cost_per_token: 0.00000119
      output_cost_per_token: 0.00000374

  - model_name: reasoning-tier
    params:
      model: openrouter/moonshotai/kimi-k3
      api_key: os.environ/OPENROUTER_API_KEY
    model_info:
      input_cost_per_token: 0.000003
      output_cost_per_token: 0.000015

  - model_name: agent-coder
    params:
      # A historical rationale comment, exactly like the real file has.
      model: openrouter/deepseek/deepseek-v4-pro
      api_key: os.environ/OPENROUTER_API_KEY
    model_info:
      input_cost_per_token: 0.0000016
      output_cost_per_token: 0.0000032
  - model_name: agent-planner
    params:
      model: openrouter/moonshotai/kimi-k2.7-code
      api_key: os.environ/OPENROUTER_API_KEY
    model_info:
      input_cost_per_token: 0.00000071
      output_cost_per_token: 0.0000035
  - model_name: smart-router
    params:
      model: auto_router/complexity_router
    model_info:
      supports_vision: true
"""

FAKE_CATALOG = [
    {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek V4 Pro", "context_length": 128000,
     "input_cost_per_token": 0.0000016, "output_cost_per_token": 0.0000032},
    {"id": "qwen/qwen3-coder-plus", "name": "Qwen3 Coder Plus", "context_length": 128000,
     "input_cost_per_token": 0.00000065, "output_cost_per_token": 0.00000325},
]


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(FAKE_CONFIG)
    monkeypatch.setattr(model_config, "ROUTER_CONFIG_PATH", path)
    return path


def test_get_current_pins_only_returns_managed_roles(config_path):
    pins = model_config.get_current_pins()
    assert set(pins) == {"agent-coder", "agent-planner"}
    assert pins["agent-coder"]["model"] == "deepseek/deepseek-v4-pro"
    assert pins["agent-coder"]["input_cost_per_token"] == 0.0000016


# ---------------------------------------------------------------------------
# resolve_alias -- a pinned role's response always echoes back the bare
# alias, never the real model, so this is the only way anything (the live
# chat badge, the analytics scan) learns which model actually answered.
# Deliberately uncached: a real incident showed a swapped pin still
# displaying the old model's name in the chat UI, because the resolver used
# there cached the alias map for the life of the process with nothing to
# invalidate it short of a restart.
# ---------------------------------------------------------------------------


def test_resolve_alias_returns_current_model_not_a_cached_one(config_path):
    assert model_config.resolve_alias("agent-coder") == "deepseek/deepseek-v4-pro"

    model_config.set_pins({"agent-coder": "qwen/qwen3-coder-plus"}, FAKE_CATALOG)

    # No restart, no cache-clearing call of any kind -- resolve_alias must
    # reflect the swap immediately, the same way it would have to for a
    # dashboard pin change to actually show up in the live chat right away.
    assert model_config.resolve_alias("agent-coder") == "qwen/qwen3-coder-plus"


def test_resolve_alias_passes_through_unknown_names_unchanged(config_path):
    assert model_config.resolve_alias("smart-router") == "smart-router"
    assert model_config.resolve_alias("z-ai/glm-5.2") == "z-ai/glm-5.2"


def test_resolve_alias_passes_through_none(config_path):
    assert model_config.resolve_alias(None) is None


def test_set_pins_updates_only_the_requested_role(config_path):
    original = config_path.read_text()

    model_config.set_pins({"agent-coder": "qwen/qwen3-coder-plus"}, FAKE_CATALOG)

    updated = config_path.read_text()
    pins = model_config.get_current_pins()
    assert pins["agent-coder"]["model"] == "qwen/qwen3-coder-plus"
    assert pins["agent-coder"]["input_cost_per_token"] == 0.00000065
    # agent-planner (a different role) must be completely untouched.
    assert pins["agent-planner"]["model"] == "moonshotai/kimi-k2.7-code"
    # Every line outside the agent-coder block -- including the comment
    # inside it -- must be byte-for-byte unchanged.
    assert "# A historical rationale comment, exactly like the real file has." in updated
    assert "model_name: glm-5.2" in updated
    assert "model_name: reasoning-tier" in updated
    assert "model_name: smart-router" in updated
    # Only the expected lines actually changed.
    orig_lines = set(original.splitlines())
    new_lines = set(updated.splitlines())
    assert (new_lines - orig_lines) == {
        "      model: openrouter/qwen/qwen3-coder-plus",
        "      input_cost_per_token: 0.00000065",
        "      output_cost_per_token: 0.00000325",
    }


def test_set_pins_rejects_role_outside_managed_set(config_path):
    with pytest.raises(model_config.UnknownRoleError):
        model_config.set_pins({"reasoning-tier": "moonshotai/kimi-k3"}, FAKE_CATALOG)
    # Must not have written anything.
    assert "openrouter/moonshotai/kimi-k3\n      api_key" in config_path.read_text()


def test_set_pins_rejects_role_not_present_in_smart_router_either(config_path):
    """smart-router and every other non-agent-* alias must be categorically
    unreachable through this API, not just accidentally never requested."""
    with pytest.raises(model_config.UnknownRoleError):
        model_config.set_pins({"smart-router": "deepseek/deepseek-v4-pro"}, FAKE_CATALOG)


def test_set_pins_rejects_model_not_in_catalog(config_path):
    with pytest.raises(model_config.ModelNotInCatalogError):
        model_config.set_pins({"agent-coder": "totally/made-up-model"}, FAKE_CATALOG)


def test_set_pins_raises_if_role_missing_from_config(config_path):
    with pytest.raises(model_config.PinBlockNotFoundError):
        model_config.set_pins({"agent-vision": "deepseek/deepseek-v4-pro"}, FAKE_CATALOG)


def test_format_rate_matches_the_files_own_decimal_style():
    assert model_config._format_rate(1.4e-06) == "0.0000014"
    assert model_config._format_rate(4.4e-06) == "0.0000044"
    assert model_config._format_rate(1.75e-06) == "0.00000175"
    assert model_config._format_rate(0.000014) == "0.000014"


def test_restart_llm_router_calls_pm2_with_hardcoded_name_only(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "restarted", "stderr": ""})()

    monkeypatch.setattr(model_config.subprocess, "run", fake_run)
    # This test is about the command, not about the router coming back -- and
    # without the stub it really does poll a dead placeholder URL for 25s.
    monkeypatch.setattr(model_config, "_wait_for_router", lambda url, limit_s=0: (True, 0.1))
    result = model_config.restart_llm_router()

    assert calls == [["pm2", "restart", "llm-router"]]
    assert result["ok"] is True


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def __call__(self, *a, **k):  # stands in for the AsyncClient constructor
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kwargs):
        return _FakeResp(self._payload)


async def test_catalog_surfaces_best_agents_arena_entry_and_cutoff(monkeypatch):
    """Educated-pin metrics (operator ask 2026-08-28): the picker shows the
    model's BEST agents-arena standing plus knowledge cutoff. A model with two
    agents entries surfaces the higher-ELO one; a model with only non-agents
    benchmarks (or none) gets arena=None, not a KeyError."""
    import httpx

    payload = {"data": [
        {"id": "qwen/qwen3.8-max", "name": "Qwen3.8 Max", "context_length": 262144,
         "pricing": {"prompt": "0.0000012", "completion": "0.000006"},
         "knowledge_cutoff": "2025-04-01",
         "benchmarks": {"design_arena": [
             {"arena": "agents", "category": "fullstack", "elo": 1331, "win_rate": 65.3, "rank": 4},
             {"arena": "agents", "category": "backend", "elo": 1200, "win_rate": 55.0, "rank": 12},
             {"arena": "design", "category": "web", "elo": 1500, "win_rate": 70.0, "rank": 1},
         ]}},
        {"id": "plain/model", "name": "Plain", "context_length": 8192,
         "pricing": {"prompt": "0.000001", "completion": "0.000002"},
         "benchmarks": {"design_arena": [
             {"arena": "design", "category": "web", "elo": 1400, "win_rate": 60.0, "rank": 3},
         ]}},
        {"id": "bare/model", "name": "Bare", "pricing": {}},
    ]}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient(payload))
    catalog = await model_config.fetch_model_catalog(force=True)
    try:
        qwen = next(m for m in catalog if m["id"] == "qwen/qwen3.8-max")
        assert qwen["arena"] == {"category": "fullstack", "elo": 1331, "rank": 4, "win_rate": 65.3}
        assert qwen["knowledge_cutoff"] == "2025-04-01"
        # design-arena-only and benchmark-less models degrade to None, present keys
        assert next(m for m in catalog if m["id"] == "plain/model")["arena"] is None
        bare = next(m for m in catalog if m["id"] == "bare/model")
        assert bare["arena"] is None and bare["knowledge_cutoff"] is None
    finally:
        model_config._catalog_cache["data"] = None  # don't poison the real-fetch test


async def test_endpoints_surface_latency_throughput_and_caching(monkeypatch):
    """Provider picker metrics: latency, throughput, uptime, implicit caching."""
    import httpx

    payload = {"data": {"endpoints": [
        {"provider_name": "StreamLake", "context_length": 131072,
         "pricing": {"prompt": "0.00000056", "completion": "0.00000168"},
         "quantization": "fp8", "uptime_last_30m": 99.7,
         # authenticated shape: percentile dicts, latency in ms
         "latency_last_30m": {"p50": 1420, "p75": 2100, "p90": 3000, "p99": 9000},
         "throughput_last_30m": {"p50": 68.3, "p75": 75, "p90": 80, "p99": 90},
         "supports_implicit_caching": True},
        {"provider_name": "Sparse", "pricing": {}},
    ]}}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient(payload))
    model_config._ENDPOINTS_CACHE.pop("x/metrics-test", None)
    eps = await model_config.fetch_model_endpoints("x/metrics-test")
    model_config._ENDPOINTS_CACHE.pop("x/metrics-test", None)
    assert eps[0]["latency_s"] == 1.42
    assert eps[0]["throughput_tps"] == 68.3
    assert eps[0]["uptime"] == 99.7
    assert eps[0]["implicit_caching"] is True
    # sparse endpoint: keys present, None/False -- never KeyError in the UI
    assert eps[1]["latency_s"] is None and eps[1]["implicit_caching"] is False


async def test_real_openrouter_catalog_fetch_returns_well_formed_entries():
    """One real network call, matching this project's own preference for
    verifying against real infra over mocking the one thing that would make
    a real-world format change invisible (see test_model_rates.py)."""
    catalog = await model_config.fetch_model_catalog(force=True)
    if not catalog:
        pytest.skip("OpenRouter pricing endpoint unreachable from this environment")
    assert any(m["id"] == "z-ai/glm-5.3" for m in catalog)
    sample = next(m for m in catalog if m["id"] == "z-ai/glm-5.3")
    assert 0 < sample["input_cost_per_token"] < 0.001
    assert sample["name"]
    # metric keys are always present (values may be None for unbenched models)
    assert "arena" in sample and "knowledge_cutoff" in sample


def test_get_current_pins_is_mtime_cached_but_reparses_on_change(config_path, monkeypatch):
    """audit H-22: repeated reads of an unchanged file don't re-parse the YAML,
    but a set_pins write (which bumps mtime) is reflected on the next call."""
    import agent.model_config as mc

    calls = {"n": 0}
    real_safe_load = mc.yaml.safe_load

    def counting_safe_load(text):
        calls["n"] += 1
        return real_safe_load(text)

    monkeypatch.setattr(mc.yaml, "safe_load", counting_safe_load)

    # First call parses; second call (file unchanged) is served from cache.
    mc.get_current_pins()
    first = calls["n"]
    mc.get_current_pins()
    assert calls["n"] == first, "unchanged file should not be re-parsed"

    # A pin change rewrites the file -> cache invalidates -> next read reflects it.
    mc.set_pins({"agent-coder": "qwen/qwen3-coder-plus"}, FAKE_CATALOG)
    assert mc.get_current_pins()["agent-coder"]["model"] == "qwen/qwen3-coder-plus"


# audit H-5: a block with comment lines between `model_info:` and the cost lines
# (the real agent-cartographer shape) made the old regex walk forward into a
# DIFFERENT role's model_info and reprice it. The tempered pattern must match
# each role's OWN block.
_H5_CONFIG = """\
model_list:
  - model_name: agent-cartographer
    params:
      # a long rationale comment, like the real file
      model: openrouter/mistralai/mistral-small-3.2-24b-instruct
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body: {"provider": {"require_parameters": true}}
    model_info:
      # Fallback only -- the dashboard reads price from the live catalog.
      # These hand-written blocks drift; kept for a catalog outage.
      input_cost_per_token: 0.000000075
      output_cost_per_token: 0.0000003
  - model_name: agent-consolidator
    params:
      model: openrouter/anthropic/claude-haiku-4.5
      api_key: os.environ/OPENROUTER_API_KEY
    model_info:
      input_cost_per_token: 0.000001
      output_cost_per_token: 0.000005
"""


def test_h5_block_pattern_matches_each_roles_own_costs_across_a_commented_block(monkeypatch, tmp_path):
    import agent.model_config as mc
    path = tmp_path / "config.yaml"
    path.write_text(_H5_CONFIG)
    monkeypatch.setattr(mc, "ROUTER_CONFIG_PATH", path)

    carto = mc._block_pattern("agent-cartographer").search(_H5_CONFIG)
    assert carto is not None
    # its OWN costs (7.5e-08 / 3e-07), not the consolidator's (1e-06 / 5e-06)
    assert carto.group("input_cost") == "0.000000075"
    assert carto.group("output_cost") == "0.0000003"

    consol = mc._block_pattern("agent-consolidator").search(_H5_CONFIG)
    assert consol.group("input_cost") == "0.000001"


def test_h5_set_pins_verification_rejects_a_mismatch(monkeypatch, tmp_path):
    # If the regex ever matched the wrong block again, the post-write parse+diff
    # must refuse rather than silently write a corrupted config. We simulate a
    # bad match by pointing set_pins at a role whose block cannot be found.
    import agent.model_config as mc
    path = tmp_path / "config.yaml"
    path.write_text(_H5_CONFIG)
    monkeypatch.setattr(mc, "ROUTER_CONFIG_PATH", path)
    monkeypatch.setattr(mc, "MANAGED_ROLES", {"agent-cartographer": "Cartographer"})
    catalog = [{"id": "mistralai/mistral-small-3.2-24b-instruct",
                "input_cost_per_token": 1e-7, "output_cost_per_token": 4e-7}]
    before = path.read_text()
    result = mc.set_pins({"agent-cartographer": "mistralai/mistral-small-3.2-24b-instruct"}, catalog)
    assert result["agent-cartographer"]["model"] == "mistralai/mistral-small-3.2-24b-instruct"
    # the consolidator block must be byte-for-byte untouched
    assert "input_cost_per_token: 0.000001" in path.read_text()


# ---------------------------------------------------------------------------
# family extras follow the pin (operator requirement 2026-08-28)
# ---------------------------------------------------------------------------
#
# A dashboard repin from Sonnet to qwen kept Sonnet's deployment extras: the
# leftover additional_drop_params silently stripped temperature=0 from every
# hard-planning call. Extras are properties of the MODEL FAMILY, not the
# role -- the editor now adds them on any Anthropic pin and removes them on
# any other, so the operator can bounce a role between Sonnet and qwen
# freely ("I may repin sonnet for super complicated stuff").

import yaml as _yaml
from agent.model_config import _is_anthropic, _normalize_family_extras, _requires_family_extras

_BLOCK_PLAIN = """  - model_name: agent-planning-chat-hard
    params:
      # some human comment that must survive
      model: openrouter/qwen/qwen3.8-max
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body: {"provider": {"require_parameters": true}}
    model_info:
      input_cost_per_token: 0.000002
      output_cost_per_token: 0.000006
"""

_BLOCK_WITH_EXTRAS = """  - model_name: agent-planning-chat-hard
    params:
      # some human comment that must survive
      model: openrouter/anthropic/claude-sonnet-5
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body: {"provider": {"require_parameters": true}}
      # [managed] family extras for an Anthropic pin -- auto-added by the
      # dashboard editor; auto-removed when this role is repinned off
      # Anthropic. Do not hand-edit: see _normalize_family_extras.
      additional_drop_params: ["temperature", "top_p", "top_k"]
      cache_control_injection_points:
        - location: message
          role: system
        - location: message
          index: -1
    model_info:
      input_cost_per_token: 0.000002
      output_cost_per_token: 0.00001
"""


def _params(block):
    return _yaml.safe_load(block)[0]["params"]


def test_is_anthropic_detects_both_slug_shapes():
    assert _is_anthropic("anthropic/claude-sonnet-5")
    assert _is_anthropic("openrouter/anthropic/claude-opus-5")
    assert not _is_anthropic("qwen/qwen3.8-max")


def test_extras_key_on_the_sampling_rejecting_generation_not_the_vendor():
    """haiku-4.5 (the reviewer's pin) ACCEPTS temperature -- force-dropping
    it would be the same un-pinning bug in the other direction."""
    assert _requires_family_extras("anthropic/claude-sonnet-5")
    assert _requires_family_extras("openrouter/anthropic/claude-opus-4.8")
    assert _requires_family_extras("anthropic/claude-sonnet-4.6")
    assert _requires_family_extras("anthropic/claude-fable-5")
    assert not _requires_family_extras("anthropic/claude-haiku-4.5")
    assert not _requires_family_extras("anthropic/claude-opus-4.5")
    assert not _requires_family_extras("anthropic/claude-3-haiku")
    assert not _requires_family_extras("deepseek/deepseek-v4-pro")


def test_pinning_anthropic_adds_both_extras():
    out = _normalize_family_extras(_BLOCK_PLAIN, "anthropic/claude-sonnet-5")
    lp = _params(out)
    assert lp["additional_drop_params"] == ["temperature", "top_p", "top_k"]
    assert lp["cache_control_injection_points"] == [
        {"location": "message", "role": "system"},
        {"location": "message", "index": -1},
    ]
    assert "some human comment that must survive" in out


def test_pinning_off_anthropic_removes_both_extras():
    out = _normalize_family_extras(_BLOCK_WITH_EXTRAS, "qwen/qwen3.8-max")
    lp = _params(out)
    assert "additional_drop_params" not in lp
    assert "cache_control_injection_points" not in lp
    assert "some human comment that must survive" in out
    assert "[managed]" not in out  # the managed comment leaves with its extras


def test_normalizer_is_idempotent_in_both_directions():
    once = _normalize_family_extras(_BLOCK_PLAIN, "anthropic/claude-sonnet-5")
    twice = _normalize_family_extras(once, "anthropic/claude-sonnet-5")
    assert once == twice
    off_once = _normalize_family_extras(_BLOCK_WITH_EXTRAS, "qwen/qwen3.8-max")
    off_twice = _normalize_family_extras(off_once, "qwen/qwen3.8-max")
    assert off_once == off_twice


def test_non_anthropic_to_non_anthropic_is_untouched():
    assert _normalize_family_extras(_BLOCK_PLAIN, "deepseek/deepseek-v4-pro") == _BLOCK_PLAIN


def test_the_live_config_obeys_the_family_rule():
    """Guards HAND edits too: any agent-* pin in the real config must carry
    the extras iff it is Anthropic. Fails the suite the moment the file
    drifts, instead of a role silently living on its fallback.

    Reads through ROUTER_CONFIG_PATH rather than the literal path: the
    live config is the operator's file and is gitignored, so on a fresh
    clone -- CI, a contributor's laptop -- this checks the example, which is
    what that clone would install.
    """
    from agent.tools.model_rates import ROUTER_CONFIG_PATH

    d = _yaml.safe_load(ROUTER_CONFIG_PATH.read_text())
    for entry in d["model_list"]:
        name = entry.get("model_name") or ""
        if not name.startswith("agent-"):
            continue
        lp = entry.get("params") or {}
        wants = _requires_family_extras(str(lp.get("model", "")))
        has = "additional_drop_params" in lp and "cache_control_injection_points" in lp
        assert has == wants, f"{name}: family extras {'missing' if wants else 'left behind'}"


# ---------------------------------------------------------------------------
# provider pinning (dashboard feature, 2026-08-28)
# ---------------------------------------------------------------------------

import pytest as _pytest
from agent import model_config as _mc

_PROVIDER_CONFIG = """model_list:
  - model_name: agent-coder
    params:
      # comment that must survive
      model: openrouter/deepseek/deepseek-v4-pro
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body: {"provider": {"require_parameters": true}}
    model_info:
      input_cost_per_token: 0.00000087
      output_cost_per_token: 0.00000174
  - model_name: agent-vision
    params:
      model: openrouter/qwen/qwen3-vl-235b-a22b-instruct
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body: {"provider": {"require_parameters": true}}
    model_info:
      input_cost_per_token: 0.0000002
      output_cost_per_token: 0.0000008

router_settings:
  drop_params: true
"""


@_pytest.fixture
def provider_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_PROVIDER_CONFIG)
    monkeypatch.setattr(_mc, "ROUTER_CONFIG_PATH", cfg)
    _mc._PINS_CACHE.update(key=None, pins=None)
    yield cfg
    _mc._PINS_CACHE.update(key=None, pins=None)


def test_pin_a_provider_writes_only_and_disables_pool_fallback(provider_config):
    _mc.set_provider_pins({"agent-coder": "DeepSeek"})
    import yaml as _y
    lp = [e for e in _y.safe_load(provider_config.read_text())["model_list"]
          if e["model_name"] == "agent-coder"][0]["params"]
    pref = lp["extra_body"]["provider"]
    assert pref["only"] == ["DeepSeek"]
    assert pref["allow_fallbacks"] is False
    assert pref["require_parameters"] is True
    assert "# comment that must survive" in provider_config.read_text()


def test_clearing_returns_to_the_canonical_auto_form(provider_config):
    _mc.set_provider_pins({"agent-coder": "DeepSeek"})
    _mc.set_provider_pins({"agent-coder": None})
    assert '"provider": {"require_parameters": true}}' in provider_config.read_text()
    assert "only" not in provider_config.read_text()


def test_get_current_pins_surfaces_the_provider(provider_config):
    _mc.set_provider_pins({"agent-coder": "DeepSeek"})
    pins = _mc.get_current_pins()
    assert pins["agent-coder"]["provider"] == "DeepSeek"
    assert pins["agent-vision"]["provider"] is None


def test_unmanaged_role_is_refused(provider_config):
    with _pytest.raises(_mc.UnknownRoleError):
        _mc.set_provider_pins({"smart-router": "OpenAI"})


def test_implausible_provider_name_is_refused(provider_config):
    with _pytest.raises(_mc.ProviderPinError):
        _mc.set_provider_pins({"agent-coder": 'x"], "allow_fallbacks": [true'})
    assert "allow_fallbacks" not in provider_config.read_text().split("agent-coder")[1].split("model_info")[0].replace(
        '{"provider": {"require_parameters": true}}', "")


def test_a_model_repin_resets_the_provider_pin(provider_config):
    """A provider chosen for model A is meaningless for model B -- OpenRouter
    would refuse to route a model the pinned provider does not serve."""
    _mc.set_provider_pins({"agent-coder": "DeepSeek"})
    catalog = [{"id": "qwen/qwen3.8-max", "input_cost_per_token": 2e-06, "output_cost_per_token": 6e-06}]
    _mc.set_pins({"agent-coder": "qwen/qwen3.8-max"}, catalog)
    pins = _mc.get_current_pins()
    assert pins["agent-coder"]["model"] == "qwen/qwen3.8-max"
    assert pins["agent-coder"]["provider"] is None


# ---------------------------------------------------------------------------
# Restarting the router: pm2's exit code is not the question
# ---------------------------------------------------------------------------

def _fake_pm2(returncode: int, out: str = "restarted"):
    class _R:
        pass

    r = _R()
    r.returncode, r.stdout, r.stderr = returncode, out, ""
    return lambda *a, **k: r


def test_a_router_that_answers_counts_as_restarted_even_if_pm2_complained(monkeypatch):
    """Reported live: the operator's only evidence that a restart had worked
    was a Telegram alert, because the dialog reported a failure. pm2 can print
    a warning and exit non-zero while the app restarts perfectly, so the thing
    answering its liveness route is what decides."""
    monkeypatch.setattr(_mc.subprocess, "run", _fake_pm2(1, "some pm2 warning"))
    monkeypatch.setattr(_mc, "_wait_for_router", lambda url, limit_s=0: (True, 3.0))

    res = _mc.restart_llm_router("http://127.0.0.1:4000/v1")
    assert res["ok"] is True
    assert res["healthy"] is True
    assert res["restarted"] is False, "pm2's own verdict is still reported"
    assert res["waited_s"] == 3.0


def test_a_router_that_never_comes_back_is_not_a_success(monkeypatch):
    """The other direction: pm2 exits zero and the router dies on a bad config."""
    monkeypatch.setattr(_mc.subprocess, "run", _fake_pm2(0))
    monkeypatch.setattr(_mc, "_wait_for_router", lambda url, limit_s=0: (False, 25.0))

    res = _mc.restart_llm_router("http://127.0.0.1:4000/v1")
    assert res["ok"] is False
    assert res["restarted"] is True, "which half failed has to be answerable"
    assert res["healthy"] is False


def test_without_a_base_url_it_falls_back_to_pm2s_verdict(monkeypatch):
    monkeypatch.setattr(_mc.subprocess, "run", _fake_pm2(0))
    monkeypatch.setattr(_mc, "_router_liveness_url", lambda base: None)

    res = _mc.restart_llm_router(None)
    assert res["ok"] is True and res["healthy"] is None


def test_the_wait_returns_as_soon_as_the_router_answers(monkeypatch):
    """Polling, not a fixed sleep: a router back in two seconds must not make
    the operator watch a spinner for twenty."""
    calls = {"n": 0}

    class _Resp:
        status_code = 200

    def get(url, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("connection refused")
        return _Resp()

    monkeypatch.setattr(_mc.httpx, "get", get)
    monkeypatch.setattr(_mc.time, "sleep", lambda s: None)

    healthy, waited = _mc._wait_for_router("http://127.0.0.1:4000/health/liveliness")
    assert healthy is True
    assert calls["n"] == 3
    assert waited < _mc._ROUTER_BOOT_WAIT_S


def test_the_wait_gives_up_rather_than_hanging(monkeypatch):
    monkeypatch.setattr(_mc.httpx, "get", lambda url, timeout=None: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(_mc.time, "sleep", lambda s: None)
    monkeypatch.setattr(_mc.time, "monotonic", iter([0, 0, 30, 30]).__next__)

    healthy, _ = _mc._wait_for_router("http://127.0.0.1:4000/health/liveliness", limit_s=25)
    assert healthy is False
