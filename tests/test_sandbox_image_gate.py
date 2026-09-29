"""The gate in front of the Docker-backed sandbox suites (tests/sandbox_image.py).

A stale local image -- one built before docker/agent-sandbox/ last changed --
used to fail those suites with errors that read as product bugs; now it is a
skip that says to rebuild (2026-09-29 audit, T4). Runs everywhere: the rule
is pure, and the probes are exercised only where docker exists.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests import sandbox_image

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def test_an_image_older_than_the_dockerfile_is_stale():
    assert sandbox_image.is_stale(created=NOW - timedelta(days=7), changed=NOW - timedelta(days=1))


def test_an_image_newer_than_the_dockerfile_is_current():
    assert not sandbox_image.is_stale(created=NOW, changed=NOW - timedelta(days=1))
    assert not sandbox_image.is_stale(created=NOW, changed=NOW)


def test_no_image_or_no_history_is_never_called_stale():
    """Missing is its own skip ("not built"); no history means no verdict."""
    assert not sandbox_image.is_stale(created=None, changed=NOW)
    assert not sandbox_image.is_stale(created=NOW, changed=None)


def test_the_reason_names_the_rebuild(monkeypatch):
    monkeypatch.setattr(sandbox_image, "_image_created", lambda: NOW - timedelta(days=7))
    monkeypatch.setattr(sandbox_image, "_dockerfile_changed", lambda: NOW)
    reason = sandbox_image.skip_reason()
    assert reason and "stale" in reason and "rebuild" in reason
    monkeypatch.setattr(sandbox_image, "_image_created", lambda: None)
    assert "not built" in (sandbox_image.skip_reason() or "")
    monkeypatch.setattr(sandbox_image, "_image_created", lambda: NOW)
    assert sandbox_image.skip_reason() is None


def test_docker_s_timestamp_parses():
    """Nine fractional digits, which fromisoformat only takes up to six of."""
    parsed = sandbox_image._parse_created("2026-09-28T14:23:19.398793542Z")
    assert parsed == datetime(2026, 9, 28, 14, 23, 19, 398793, tzinfo=UTC)
    assert sandbox_image._parse_created("2026-09-28T14:23:19Z") == datetime(2026, 9, 28, 14, 23, 19, tzinfo=UTC)
