"""The ledger's schema is a contract with three readers.

agent/tools/router_ledger.py (BudgetGuard's billed cost), agent/metrics.py
(the whole Analytics page) and agent/tools/model_rates.py all parse this file.
The field names were copied from what the previous writer used, not chosen, so
a cutover needs no reader to change.
"""

from __future__ import annotations

import json

from router import ledger


def _row(tmp_path, **over):
    kw = dict(call_id="c1", alias="agent-coder", model="deepseek/flash",
              prompt_tokens=10, completion_tokens=2, cached_tokens=1,
              cost=0.001, duration_s=1.5, task_id="T1", provider="DeepInfra",
              path=tmp_path / "l.jsonl")
    kw.update(over)
    ledger.record(**kw)
    return json.loads((tmp_path / "l.jsonl").read_text().splitlines()[-1])


def test_every_field_the_old_writer_emitted_is_present(tmp_path):
    row = _row(tmp_path)
    for f in ("ts", "call_id", "requested_model", "routed_model", "alias", "tier",
              "cause", "matched_keyword", "classifier_model", "prompt_tokens",
              "completion_tokens", "cached_tokens", "task_id", "session_id",
              "cost", "duration_s"):
        assert f in row, f


def test_metrics_can_attribute_the_role_and_model(tmp_path):
    """The old writer crossed these two fields, leaving most lines
    unattributable -- see agent/metrics.py's own note."""
    from agent.metrics import _role_and_model
    assert _role_and_model(_row(tmp_path)) == ("agent-coder", "deepseek/flash")


def test_budget_guard_can_read_the_cost_back(tmp_path):
    from agent.tools.router_ledger import RouterLedger
    _row(tmp_path, call_id="abc", cost=0.0042)
    led = RouterLedger(path=tmp_path / "l.jsonl")
    assert led.actual_costs(["abc"]) == {"abc": 0.0042}


def test_a_failure_is_recorded_with_its_reason(tmp_path):
    row = _row(tmp_path, error=True, error_detail="upstream said no", cost=None)
    assert row["error"] is True and "upstream said no" in row["error_detail"]


def test_a_success_carries_no_error_key(tmp_path):
    """metrics counts an error by the presence of the key."""
    assert "error" not in _row(tmp_path)


def test_the_new_fields_are_additive(tmp_path):
    row = _row(tmp_path, attempt=2)
    assert row["provider"] == "DeepInfra" and row["attempt"] == 2


def test_a_long_error_is_capped(tmp_path):
    row = _row(tmp_path, error=True, error_detail="x" * 5000)
    assert len(row["error_detail"]) <= 400


def test_writing_never_raises(tmp_path):
    ledger.record(call_id="c", alias="a", model="m",
                  path=tmp_path / "no" / "such" / "dir" / "l.jsonl")
