"""Building the upstream request, and reading usage back out of it.

The streaming path is the subtle one: `usage` arrives in the LAST chunk, after
the body has already gone to the client, so the ledger line depends on scraping
it on the way past rather than on the response object.
"""

from __future__ import annotations

from router.upstream import Usage, build_body


def test_the_alias_is_replaced_by_the_real_model():
    b = build_body({"model": "agent-coder", "messages": []}, "deepseek/flash", {})
    assert b["model"] == "deepseek/flash"


def test_usage_include_is_always_requested():
    """This is what makes OpenRouter report usage.cost -- the real billed
    figure. Without it the ledger would be back to multiplying tokens by a
    rate table that disagrees with the invoice."""
    assert build_body({"model": "a"}, "m", {})["usage"] == {"include": True}


def test_an_existing_usage_block_is_preserved():
    b = build_body({"model": "a", "usage": {"something": 1}}, "m", {})
    assert b["usage"] == {"something": 1, "include": True}


def test_deployment_extra_body_is_applied():
    b = build_body({"model": "a"}, "m", {"provider": {"require_parameters": True}})
    assert b["provider"] == {"require_parameters": True, "sort": "throughput"}


def test_a_per_call_argument_beats_the_config_default():
    """config.yaml is a default. A caller that passes something explicitly
    means it, or the config silently overrides the code."""
    b = build_body({"model": "a", "reasoning_effort": "high"}, "m", {"reasoning_effort": "low"})
    assert b["reasoning_effort"] == "high"


def test_speed_is_the_default_until_the_router_names_hosts_itself():
    """Cost is the operator's choice of model, not of host. The router's own
    ranking (router/fastest.py) replaces this once it has one."""
    assert build_body({"model": "a"}, "m", {})["provider"] == {"sort": "throughput"}
    b = build_body({"model": "a"}, "m", {"provider": {"ignore": ["X"]}})
    assert b["provider"] == {"sort": "throughput", "ignore": ["X"]}
    assert build_body({"model": "a"}, "m", {"provider": {"sort": "latency"}})["provider"] == {"sort": "latency"}
    assert build_body({"model": "a"}, "m", {"provider": {"order": ["x"]}})["provider"] == {"order": ["x"]}


def test_routing_metadata_never_reaches_the_provider():
    b = build_body({"model": "a", "metadata": {"agent_task_id": "T1"}}, "m", {})
    assert "metadata" not in b


def test_streaming_asks_for_usage_in_the_stream():
    b = build_body({"model": "a", "stream": True}, "m", {})
    assert b["stream_options"] == {"include_usage": True}


def test_existing_stream_options_are_kept():
    b = build_body({"model": "a", "stream": True, "stream_options": {"x": 1}}, "m", {})
    assert b["stream_options"] == {"x": 1, "include_usage": True}


# ---------------------------------------------------------------------------
# usage parsing
# ---------------------------------------------------------------------------

def test_usage_reads_cost_tokens_and_provider():
    u = Usage.from_payload({
        "model": "deepseek/flash", "provider": "DeepInfra",
        "usage": {"prompt_tokens": 35, "completion_tokens": 16, "cost": 1.66e-05,
                  "prompt_tokens_details": {"cached_tokens": 12}}})
    assert (u.prompt_tokens, u.completion_tokens, u.cost) == (35, 16, 1.66e-05)
    assert u.cached_tokens == 12
    assert u.provider == "DeepInfra" and u.model == "deepseek/flash"


def test_a_payload_with_no_usage_is_all_none():
    u = Usage.from_payload({"model": "m"})
    assert u.prompt_tokens is None and u.cost is None


def test_merge_only_overwrites_with_real_values():
    """Streaming: early chunks carry the model, the last carries usage. A
    later None must not erase what an earlier chunk established."""
    u = Usage(model="deepseek/flash", provider="DeepInfra")
    u.merge(Usage.from_payload({"usage": {"prompt_tokens": 10, "cost": 0.5}}))
    assert u.model == "deepseek/flash" and u.provider == "DeepInfra"
    assert u.prompt_tokens == 10 and u.cost == 0.5


def test_the_final_chunk_wins_on_usage():
    u = Usage(prompt_tokens=1, completion_tokens=1)
    u.merge(Usage.from_payload({"usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.02}}))
    assert (u.prompt_tokens, u.completion_tokens, u.cost) == (100, 50, 0.02)


# ---------------------------------------------------------------------------
# which failures are worth trying again
# ---------------------------------------------------------------------------

import pytest

from router.upstream import is_transient


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408])
def test_provider_trouble_is_retried(status):
    """A 429 is the provider asking us to wait. config.yaml records two of them
    a minute apart taking a whole demo down, because the only answer available
    was to give up on the pinned model."""
    assert is_transient(status, None) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_our_own_mistakes_are_not_retried(status):
    """A malformed request, a bad key or an unknown model fails identically on
    the second attempt -- retrying only adds latency to a certain failure."""
    assert is_transient(status, None) is False


def test_a_request_that_never_completed_is_retried():
    """No status means a timeout, a dropped connection or a DNS blip."""
    assert is_transient(None, "ReadTimeout: ...") is True


def test_no_status_and_no_error_is_not_retried():
    assert is_transient(None, None) is False
