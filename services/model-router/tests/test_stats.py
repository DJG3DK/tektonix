"""The view that would have made the 1802-second call obvious.

A coder call that ran 1802 seconds and returned 280 output tokens looked
exactly like a healthy 1217-second test-writer call that returned 64,904.
Duration could not separate them; nothing exposed throughput. `tokens_per_s`
does, and it is computed from a ledger the router was already writing.
"""

from __future__ import annotations

import json
import time

import pytest

from router import stats


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _row(**over):
    base = {"ts": time.time(), "call_id": "c", "alias": "agent-coder",
            "requested_model": "deepseek/flash", "prompt_tokens": 100,
            "completion_tokens": 50, "cached_tokens": 40, "cost": 0.001,
            "duration_s": 2.0, "provider": "DeepInfra", "attempt": 1}
    base.update(over)
    return base


@pytest.fixture
def ledger_file(tmp_path):
    return tmp_path / "routing.jsonl"


def test_calls_are_grouped_by_alias(ledger_file):
    _write(ledger_file, [_row(), _row(), _row(alias="agent-planner")])
    out = stats.summarise(ledger_file, 3600)
    by = {a["alias"]: a for a in out["aliases"]}
    assert by["agent-coder"]["calls"] == 2 and by["agent-planner"]["calls"] == 1


def test_throughput_separates_stuck_from_busy(ledger_file):
    """The whole point. Both calls are long; only one is working."""
    _write(ledger_file, [
        _row(alias="stuck", duration_s=1802, completion_tokens=280),
        _row(alias="busy", duration_s=1217, completion_tokens=64904),
    ])
    by = {a["alias"]: a for a in stats.summarise(ledger_file, 86400)["aliases"]}
    assert by["stuck"]["tokens_per_s"] < 1
    assert by["busy"]["tokens_per_s"] > 50


def test_latency_percentiles(ledger_file):
    _write(ledger_file, [_row(duration_s=d) for d in (1, 2, 3, 4, 100)])
    a = stats.summarise(ledger_file, 3600)["aliases"][0]
    assert a["p50_s"] == 3.0 and a["max_s"] == 100.0


def test_errors_are_counted_and_excluded_from_latency(ledger_file):
    """A failed call's duration says how long it took to fail, which is not
    what a p95 is answering."""
    _write(ledger_file, [_row(), _row(error=True, duration_s=0.01, completion_tokens=0)])
    a = stats.summarise(ledger_file, 3600)["aliases"][0]
    assert a["calls"] == 2 and a["errors"] == 1 and a["error_rate"] == 0.5
    assert a["p50_s"] == 2.0


def test_spend_includes_failed_attempts(ledger_file):
    """A failed attempt can still have cost money upstream."""
    _write(ledger_file, [_row(cost=0.01), _row(error=True, cost=0.002)])
    assert stats.summarise(ledger_file, 3600)["cost_usd"] == 0.012


def test_retries_are_visible(ledger_file):
    """Before this router a fallback was only visible as a gap."""
    _write(ledger_file, [_row(attempt=1), _row(attempt=2), _row(attempt=3)])
    assert stats.summarise(ledger_file, 3600)["aliases"][0]["retries"] == 2


def test_providers_are_reported(ledger_file):
    """OpenRouter spreads one model across resellers; which one served a slow
    call was previously invisible."""
    _write(ledger_file, [_row(provider="DeepInfra"), _row(provider="Alibaba")])
    assert stats.summarise(ledger_file, 3600)["aliases"][0]["providers"] == ["Alibaba", "DeepInfra"]


def test_the_window_is_respected(ledger_file):
    now = time.time()
    _write(ledger_file, [_row(ts=now - 10), _row(ts=now - 7200)])
    assert stats.summarise(ledger_file, 3600, now=now)["calls"] == 1


def test_cache_hit_rate(ledger_file):
    _write(ledger_file, [_row(prompt_tokens=1000, cached_tokens=900)])
    assert stats.summarise(ledger_file, 3600)["aliases"][0]["cache_hit_rate"] == 0.9


def test_a_missing_ledger_is_empty_not_an_error(tmp_path):
    assert stats.summarise(tmp_path / "nope.jsonl", 3600)["calls"] == 0


def test_a_torn_line_is_skipped(ledger_file):
    """The file is appended to by a live process."""
    ledger_file.write_text(json.dumps(_row()) + "\n{partial")
    assert stats.summarise(ledger_file, 3600)["calls"] == 1
