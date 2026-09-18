"""The review we wait for is this sha, not a neighbour that shares a prefix."""

import pytest

from agent.tools.review_gate import _reviewed_sha_is, wait_for_review


FULL = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "aaaaaaaaaaaabbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_a_prefix_match_is_not_the_review_we_asked_for():
    """12-char startswith used to accept OTHER as a review of FULL."""
    assert FULL[:12] == OTHER[:12]
    assert not _reviewed_sha_is(OTHER, FULL)
    assert _reviewed_sha_is(FULL, FULL)


def test_empty_values_never_match():
    assert not _reviewed_sha_is("", FULL)
    assert not _reviewed_sha_is(FULL, "")
    assert not _reviewed_sha_is(None, FULL)


@pytest.mark.asyncio
async def test_wait_for_review_does_not_accept_a_prefix_neighbour(monkeypatch):
    async def fake_state(project):
        return {"lastReviewedSha": OTHER, "verdict": "READY"}

    monkeypatch.setattr("agent.tools.review_gate._read_state", fake_state)
    with pytest.raises(TimeoutError):
        await wait_for_review("demo", FULL, timeout=0.01, poll_interval=0.01)


@pytest.mark.asyncio
async def test_wait_for_review_accepts_the_exact_sha(monkeypatch):
    async def fake_state(project):
        return {"lastReviewedSha": FULL, "verdict": "READY", "project": project}

    monkeypatch.setattr("agent.tools.review_gate._read_state", fake_state)
    state = await wait_for_review("demo", FULL, timeout=1, poll_interval=0.01)
    assert state["verdict"] == "READY"
