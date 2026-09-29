"""The review we wait for is this sha, not a neighbour that shares a prefix."""

import pytest

from agent.tools.review_gate import _reviewed_sha_is, wait_for_review


FULL = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "aaaaaaaaaaaabbbbbbbbbbbbbbbbbbbbbbbbbbbb"


async def _nothing(project):
    return None


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
    monkeypatch.setattr("agent.tools.review_gate._read_project", _nothing)
    with pytest.raises(TimeoutError):
        await wait_for_review("demo", FULL, timeout=0.01, poll_interval=0.01)


@pytest.mark.asyncio
async def test_wait_for_review_accepts_the_exact_sha(monkeypatch):
    async def fake_state(project):
        return {"lastReviewedSha": FULL, "verdict": "READY", "project": project}

    monkeypatch.setattr("agent.tools.review_gate._read_state", fake_state)
    state = await wait_for_review("demo", FULL, timeout=1, poll_interval=0.01)
    assert state["verdict"] == "READY"


@pytest.mark.asyncio
async def test_a_review_the_service_is_still_running_is_waited_for_past_the_timeout(monkeypatch):
    """2026-09-29: the reviewer ran a ten-minute suite and the wait gave up
    while it was working."""
    from agent.tools import review_gate as rg
    polls = {"n": 0}

    async def fake_state(project):
        polls["n"] += 1
        # Ready only after the plain timeout would have expired.
        return {"lastReviewedSha": FULL if polls["n"] >= 6 else OTHER, "verdict": "READY"}

    async def fake_project(project):
        return {"inProgress": {"sha": FULL, "step": "running checks"}}

    monkeypatch.setattr(rg, "_read_state", fake_state)
    monkeypatch.setattr(rg, "_read_project", fake_project)
    state = await rg.wait_for_review("demo", FULL, timeout=0.02, poll_interval=0.01)
    assert state["verdict"] == "READY" and polls["n"] >= 6


@pytest.mark.asyncio
async def test_a_review_of_some_other_sha_in_progress_does_not_extend_the_wait(monkeypatch):
    from agent.tools import review_gate as rg

    async def fake_state(project):
        return {"lastReviewedSha": OTHER, "verdict": "READY"}

    async def fake_project(project):
        return {"inProgress": {"sha": OTHER, "step": "running checks"}}

    monkeypatch.setattr(rg, "_read_state", fake_state)
    monkeypatch.setattr(rg, "_read_project", fake_project)
    with pytest.raises(TimeoutError):
        await rg.wait_for_review("demo", FULL, timeout=0.02, poll_interval=0.01)


@pytest.mark.asyncio
async def test_the_extended_wait_has_a_ceiling(monkeypatch):
    from agent.tools import review_gate as rg

    async def fake_state(project):
        return {"lastReviewedSha": OTHER, "verdict": "READY"}

    async def fake_project(project):
        return {"inProgress": {"sha": FULL, "step": "running checks"}}

    monkeypatch.setattr(rg, "_read_state", fake_state)
    monkeypatch.setattr(rg, "_read_project", fake_project)
    with pytest.raises(TimeoutError) as e:
        await rg.wait_for_review("demo", FULL, timeout=0.02, poll_interval=0.01)
    assert "did not review" in str(e.value)


@pytest.mark.asyncio
async def test_a_re_review_waits_for_a_verdict_newer_than_the_request(monkeypatch):
    """The reviewer's old record for this sha is not the answer to a new
    request: without this the gate's re-ask read the harness failure back
    at once (2026-09-29)."""
    from agent.tools import review_gate as rg
    polls = {"n": 0}

    async def fake_state(project):
        polls["n"] += 1
        old = {"lastReviewedSha": FULL, "verdict": "NEEDS_FIXES", "reviewedAt": "2026-09-29T00:00:00Z"}
        new = {"lastReviewedSha": FULL, "verdict": "READY", "reviewedAt": "2026-09-29T12:00:00Z"}
        return new if polls["n"] >= 3 else old

    monkeypatch.setattr(rg, "_read_state", fake_state)
    monkeypatch.setattr(rg, "_read_project", _nothing)
    import datetime
    asked = datetime.datetime(2026, 9, 29, 6, tzinfo=datetime.UTC).timestamp()
    state = await rg.wait_for_review("demo", FULL, timeout=1, poll_interval=0.01, after=asked)
    assert state["verdict"] == "READY" and polls["n"] >= 3


def test_a_record_without_a_time_or_without_a_request_time_counts():
    from agent.tools.review_gate import _reviewed_after
    assert _reviewed_after({"lastReviewedSha": FULL}, 1.0)
    assert _reviewed_after({"reviewedAt": "2026-09-29T00:00:00Z"}, None)
    assert not _reviewed_after({"reviewedAt": "2026-09-29T00:00:00Z"}, 4_000_000_000.0)
