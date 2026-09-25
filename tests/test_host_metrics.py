"""The runner's own record of the box and the router during a benchmark
(agent/evals/host_metrics.py), and how the page reads it back."""
import json
import time

from agent.evals import host_metrics as hm


def test_the_router_window_counts_calls_in_flight_and_latency(tmp_path):
    ledger = tmp_path / "routing.jsonl"
    now = 1_000_000.0
    rows = [
        {"ts": now - 50, "alias": "agent-coder", "duration_s": 20.0},      # in flight 930-950
        {"ts": now - 45, "alias": "agent-verifier", "duration_s": 10.0},   # 945-955
        {"ts": now - 44, "alias": "agent-test-writer", "duration_s": 12.0},  # 944-956: overlaps the one above
        {"ts": now - 10, "alias": "agent-reviewer", "duration_s": 99.0},   # not a task seat
        {"ts": now - 100, "alias": "agent-coder", "duration_s": 1.0},      # before the window
        {"ts": now - 5, "alias": "agent-coder", "duration_s": 2.0, "error": "boom"},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    w = hm.router_window(now - 60, now, ledger=ledger)
    assert w["router_calls"] == 4
    assert w["router_inflight"] == 3   # 930-950, 945-955 and 944-956 overlap
    assert w["router_errors"] == 1
    assert w["router_p50_s"] == 11.0 and w["router_p90_s"] == 20.0


def test_a_sample_is_appended_and_the_peaks_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(hm, "_containers", lambda: 3)
    monkeypatch.setattr(hm, "_oom_kills_since", lambda epoch: 2)
    monkeypatch.setattr(hm, "router_window", lambda since, until, ledger=None: {
        "router_calls": 5, "router_inflight": 4, "router_p50_s": 2.0, "router_p90_s": 9.0, "router_errors": 0})
    s = hm.Sampler(tmp_path, started=time.time(), interval_s=60)
    a = s.take(now=1_000_000)
    monkeypatch.setattr(hm, "_containers", lambda: 1)
    s.take(now=1_000_060)
    lines = (tmp_path / "host.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0]) == a
    rec = s.record()
    assert rec["samples"] == 2 and rec["peaks"]["containers"] == 3 and rec["peaks"]["router_inflight"] == 4
    assert 0 <= a["mem_pct"] <= 100 and a["disk_pct"] > 0
    assert rec["last"]["containers"] == 1


def test_shards_of_one_run_merge_to_one_series_per_minute():
    a = [{"t": 60, "mem_pct": 10.0, "containers": 3}, {"t": 120, "mem_pct": 12.0, "containers": 2}]
    b = [{"t": 65, "mem_pct": 11.0, "containers": 1}, {"t": 180, "mem_pct": 9.0, "containers": 4}]
    merged = hm.merge_series([a, b])
    assert [r["t"] // 60 for r in merged] == [1, 2, 3]
    assert merged[0]["mem_pct"] == 11.0 and merged[0]["containers"] == 3


def test_a_long_series_is_thinned_for_the_page(tmp_path):
    (tmp_path / "host.jsonl").write_text("\n".join(json.dumps({"t": i * 60, "mem_pct": 1}) for i in range(1000)))
    rows = hm.read_samples(tmp_path, limit=100)
    assert len(rows) == 100 and rows[0]["t"] == 0 and rows[-1]["t"] == 990 * 60
