"""How many times one model call may silently become three.

The openai SDK defaults to max_retries=2 and we never set it, so the default
was inherited rather than chosen. A tool-calling call is non-streaming (see
model_client's disable_streaming="tool_calling"), so it is governed by a real
total timeout -- and on hitting it the SDK retries twice before this code sees
an error. One logical model call could occupy three times the timeout, fifteen
minutes at the current 300s, while the agent log stayed silent because the SDK
swallows the first two failures.

Measured 2026-09-14 on task aa457790: a coder call logged duration_s=1802 at
the router -- thirty minutes -- for 280 output tokens, err=False, with no
matching error on the agent side. The router keeps an abandoned upstream call
running after the client gives up, so each retry adds a concurrent upstream
rather than replacing one.

What this does NOT do, deliberately: cap the router's own upstream time. The
test-writer legitimately runs 1217s producing 64,904 output tokens, so any
global router timeout small enough to stop the 1802s case would kill real
generations mid-flight. Duration does not separate stuck from busy --
throughput does -- and that needs a different mechanism than a timeout.
"""

from __future__ import annotations

import pytest

from agent.config import load_config
from agent.deep_agent import llm_for_role


@pytest.fixture(scope="module")
def client():
    return llm_for_role(load_config(), "agent-coder").root_async_client


def test_retries_are_set_explicitly_not_inherited(client):
    assert client.max_retries == 1, (
        "the SDK default of 2 means a timed-out call costs 3x the timeout "
        "before anything is reported"
    )


def test_one_retry_is_kept(client):
    """Zero would turn every transient blip into an escalation; the point is
    to halve the worst case, not to remove the safety net."""
    assert client.max_retries >= 1


def test_the_worst_case_is_bounded_at_twice_the_timeout(client):
    timeout = client.timeout
    budget = (client.max_retries + 1) * float(timeout)
    assert budget <= 2 * float(timeout) + 1


def test_every_role_gets_the_same_ceiling():
    """A per-role model is built by the same factory; a role that skipped it
    would quietly keep the default."""
    cfg = load_config()
    for role in ("agent-coder", "agent-investigator", "agent-test-writer", "agent-reviewer"):
        assert llm_for_role(cfg, role).root_async_client.max_retries == 1, role


def test_an_explicit_timeout_still_wins():
    """Roles that opt into a longer budget (planning at high reasoning effort)
    must keep it -- the retry cap is orthogonal."""
    llm = llm_for_role(load_config(), "agent-planning-chat-hard", timeout=900)
    assert float(llm.root_async_client.timeout) == 900.0
    assert llm.root_async_client.max_retries == 1
