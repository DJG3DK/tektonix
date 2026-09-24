"""The deployment table, and the reload an at-startup reader could not do.

The proxy this replaced read config.yaml once at startup. Repinning from the dashboard
therefore meant restarting the router, and a restart kills every model call in
flight across every service sharing it -- the operation docs/architecture.md
tells you never to perform while a task is running. These tests exist to keep
that property from quietly coming back.
"""

from __future__ import annotations

import time

import pytest
import yaml

from router.config import Registry, load

BASE = {
    "model_list": [
        {"model_name": "agent-coder",
         "params": {"model": "openrouter/deepseek/deepseek-v4.1-flash",
                            "extra_body": {"provider": {"require_parameters": True}}},
         "model_info": {"input_cost_per_token": 1.5e-07, "output_cost_per_token": 6e-07}},
        {"model_name": "claude-haiku-4.5",
         "params": {"model": "openrouter/anthropic/claude-haiku-4.5", "timeout": 120}},
    ],
    "router_settings": {"fallbacks": [{"agent-coder": ["claude-haiku-4.5", "nonexistent"]}]},
}


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(BASE))
    return p


def test_aliases_resolve_to_their_model(cfg):
    t = load(cfg)
    assert t.resolve("agent-coder").model == "deepseek/deepseek-v4.1-flash"


def test_the_openrouter_prefix_is_stripped(cfg):
    """The old proxy used the leading segment to choose an SDK. We only ever talk to
    OpenRouter, and it does not want the prefix in the model id."""
    assert not t_model(cfg).startswith("openrouter/")


def t_model(cfg):
    return load(cfg).resolve("agent-coder").model


def test_extra_body_and_costs_survive(cfg):
    d = load(cfg).resolve("agent-coder")
    assert d.extra_body == {"provider": {"require_parameters": True}}
    assert d.input_cost_per_token == 1.5e-07


def test_a_per_deployment_timeout_is_honoured(cfg):
    """Exactly one deployment had a timeout before this router; the rest had none,
    which is how an upstream call ran 1802 seconds for 280 output tokens."""
    assert load(cfg).resolve("claude-haiku-4.5").timeout_s == 120.0
    assert load(cfg).resolve("agent-coder").timeout_s > 0


def test_the_fallback_chain_starts_with_the_alias_itself(cfg):
    assert load(cfg).chain("agent-coder")[0] == "agent-coder"


def test_a_fallback_naming_a_missing_deployment_is_dropped(cfg):
    """The old proxy answered that case at the moment of failure with 'Available
    Model Group Fallbacks=None' -- the worst possible time to discover it."""
    assert load(cfg).chain("agent-coder") == ["agent-coder", "claude-haiku-4.5"]


def test_an_alias_with_no_fallbacks_is_just_itself(cfg):
    assert load(cfg).chain("claude-haiku-4.5") == ["claude-haiku-4.5"]


# ---------------------------------------------------------------------------
# hot reload
# ---------------------------------------------------------------------------

def test_a_repin_takes_effect_without_a_restart(cfg):
    reg = Registry(cfg)
    assert reg.table.resolve("agent-coder").model == "deepseek/deepseek-v4.1-flash"

    changed = {**BASE}
    changed["model_list"] = [dict(m) for m in BASE["model_list"]]
    changed["model_list"][0] = {**changed["model_list"][0],
                                "params": {"model": "openrouter/z-ai/glm-5.3"}}
    time.sleep(0.01)
    cfg.write_text(yaml.safe_dump(changed))

    assert reg.table.resolve("agent-coder").model == "z-ai/glm-5.3"


def test_an_unchanged_file_is_not_reparsed(cfg):
    reg = Registry(cfg)
    first = reg.table
    assert reg.table is first, "same object: a stat, not a parse, on the hot path"


def test_a_broken_config_keeps_the_running_table(cfg):
    """The moment the operator saves a bad file is exactly the moment to keep
    serving the table that is known to work."""
    reg = Registry(cfg)
    good = reg.table.resolve("agent-coder").model
    time.sleep(0.01)
    cfg.write_text("model_list: [this is not: valid: yaml")

    assert reg.table.resolve("agent-coder").model == good


def test_a_broken_config_is_not_reparsed_every_request(cfg):
    """Remembering the bad mtime matters: without it every single request
    re-parses a file that is still broken."""
    reg = Registry(cfg)
    time.sleep(0.01)
    cfg.write_text("{{{")
    _ = reg.table          # the reload attempt is the point
    assert reg._table.mtime == cfg.stat().st_mtime


def test_a_duplicate_alias_keeps_the_first(cfg):
    doubled = {**BASE, "model_list": BASE["model_list"] + [
        {"model_name": "agent-coder", "params": {"model": "openrouter/other/model"}}]}
    time.sleep(0.01)
    cfg.write_text(yaml.safe_dump(doubled))
    assert load(cfg).resolve("agent-coder").model == "deepseek/deepseek-v4.1-flash"


def test_entries_without_a_model_are_skipped(cfg, tmp_path):
    p = tmp_path / "partial.yaml"
    p.write_text(yaml.safe_dump({"model_list": [
        {"model_name": "broken"},
        {"params": {"model": "openrouter/x/y"}},
        {"model_name": "fine", "params": {"model": "openrouter/x/y"}},
    ]}))
    t = load(p)
    assert list(t.deployments) == ["fine"]


# ---------------------------------------------------------------------------
# starting without a config
#
# config.yaml is the operator's file and is gitignored, so it does not exist in
# a fresh checkout, in CI, or in a container before first run. app.py builds
# the Registry at import time, so a load that raised made the module
# unimportable in all three -- CI caught it on 2026-09-15 with a
# FileNotFoundError during collection, and the Docker bundle would have hit it
# next.
#
# The honest behaviour is to come up with nothing to route and say so through
# readiness, not to refuse to exist.
# ---------------------------------------------------------------------------

def test_a_missing_config_yields_an_empty_table(tmp_path):
    t = load(tmp_path / "nope.yaml")
    assert t.deployments == {} and t.fallbacks == {}


def test_a_registry_can_be_built_without_a_config(tmp_path):
    reg = Registry(tmp_path / "nope.yaml")
    assert reg.table.deployments == {}


def test_the_module_imports_with_no_config(monkeypatch, tmp_path):
    """The actual CI failure: `from router import app` executed
    `Registry()` against a path that does not exist."""
    monkeypatch.setenv("MODEL_ROUTER_CONFIG", str(tmp_path / "nope.yaml"))
    import importlib

    from router import config as config_mod
    importlib.reload(config_mod)
    assert config_mod.Registry().table.deployments == {}


def test_a_config_that_appears_later_is_picked_up(tmp_path):
    """A container's first boot: the service starts, the config is written,
    and it must start routing without a restart."""
    p = tmp_path / "late.yaml"
    reg = Registry(p)
    assert reg.table.deployments == {}

    p.write_text(yaml.safe_dump(BASE))
    assert "agent-coder" in reg.table.deployments


def test_an_empty_file_is_an_empty_table_not_a_crash(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    assert load(p).deployments == {}


def test_a_legacy_litellm_params_entry_still_loads(tmp_path):
    """Back-compat, deliberately kept. The key was renamed to `params` on
    2026-09-16; an operator's own config.yaml predates that and must not stop
    loading because they upgraded."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.safe_dump({"model_list": [
        {"model_name": "legacy", "litellm_params": {"model": "openrouter/x/y"}}]}))
    assert Registry(str(cfg)).table.deployments["legacy"].model == "x/y"


def test_a_deployment_s_output_cap_is_sent(tmp_path):
    """The vision bridge's `max_tokens: 1500` was read by the old proxy and
    silently dropped by this loader until 2026-09-24."""
    raw = {"model_list": [{"model_name": "agent-vision", "params": {
        "model": "openrouter/q/vl", "max_tokens": 1500, "extra_body": {"provider": {"require_parameters": True}}}}]}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    assert load(p).resolve("agent-vision").extra_body == {"max_tokens": 1500,
                                                          "provider": {"require_parameters": True}}
