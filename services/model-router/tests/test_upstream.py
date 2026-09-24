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


def test_speed_is_the_default_until_the_router_names_hosts_itself():
    """Cost is the operator's choice of model, not of host. The router's own
    ranking (router/fastest.py) replaces this once it has one."""
    assert build_body({"model": "a"}, "m", {})["provider"] == {"sort": "throughput"}
    b = build_body({"model": "a"}, "m", {"provider": {"ignore": ["X"]}})
    assert b["provider"] == {"sort": "throughput", "ignore": ["X"]}
    assert build_body({"model": "a"}, "m", {"provider": {"sort": "latency"}})["provider"] == {"sort": "latency"}
    assert build_body({"model": "a"}, "m", {"provider": {"order": ["x"]}})["provider"] == {"order": ["x"]}
