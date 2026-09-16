"""RouterLedger: the router's billed cost per call, read back from
services/model-router/logs/routing.jsonl by x-router-call-id."""

import json

from agent.tools.router_ledger import RouterLedger


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_returns_billed_cost_for_known_call_ids_only(tmp_path):
    log = tmp_path / "routing.jsonl"
    _write(log, [
        {"ts": 1, "call_id": "a", "cost": 0.05},
        {"ts": 2, "call_id": "b", "cost": 0.07},
        {"ts": 3, "requested_model": "x"},  # a pre-upgrade row without a call id
    ])
    assert RouterLedger(log).actual_costs(["a", "b", "c"]) == {"a": 0.05, "b": 0.07}


def test_missing_log_resolves_nothing(tmp_path):
    assert RouterLedger(tmp_path / "nope.jsonl").actual_costs(["a"]) == {}


def test_failure_rows_and_torn_lines_are_ignored(tmp_path):
    log = tmp_path / "routing.jsonl"
    log.write_text(
        json.dumps({"ts": 1, "call_id": "a", "cost": 0.05}) + "\n"
        + json.dumps({"ts": 2, "call_id": "f", "routed_model": None, "error": True}) + "\n"
        + '{"ts": 3, "call_id": "b", "cos'  # still being written
    )
    assert RouterLedger(log).actual_costs(["a", "b", "f"]) == {"a": 0.05}


def test_picks_up_rows_appended_after_the_first_read(tmp_path):
    log = tmp_path / "routing.jsonl"
    _write(log, [{"ts": 1, "call_id": "a", "cost": 0.05}])
    ledger = RouterLedger(log)
    assert ledger.actual_costs(["b"]) == {}
    with open(log, "a") as f:
        f.write(json.dumps({"ts": 2, "call_id": "b", "cost": 0.02}) + "\n")
    assert ledger.actual_costs(["b"]) == {"b": 0.02}


def test_reads_only_the_tail_of_a_large_log(tmp_path):
    log = tmp_path / "routing.jsonl"
    rows = [{"ts": i, "call_id": f"old-{i}", "cost": 0.01, "pad": "x" * 200} for i in range(5000)]
    rows.append({"ts": 9999, "call_id": "recent", "cost": 0.03})
    _write(log, rows)
    assert log.stat().st_size > 512_000
    ledger = RouterLedger(log)
    assert ledger.actual_costs(["recent"]) == {"recent": 0.03}
    assert ledger.actual_costs(["old-0"]) == {}, "the head of the file is deliberately not read"
