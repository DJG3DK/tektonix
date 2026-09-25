"""What the box and the router were doing while a benchmark ran.

2026-09-25: the first 50-task sample ran six tasks at once and the question
"can we run ten" had to be answered from three places -- sysstat's ten-minute
samples, the kernel log's OOM kills and the router's ledger. The runner now
takes its own sample once a minute (`Sampler`), appends it to
`<run_dir>/host.jsonl`, and keeps the peaks for summary.json, so the
analytics page shows the run and the box it ran on in one view.

Every figure is host-wide: two shards of one run sample the same box, and
the page merges them.
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path

from agent import paths

INTERVAL_S = 60
LEDGER = paths.REPO_ROOT / "services" / "model-router" / "logs" / "routing.jsonl"
# The ledger's tail is enough: a minute of calls is a few hundred lines.
_LEDGER_TAIL_BYTES = 4_000_000
# The router seats a benchmark task uses; the reviewer and the rest are noise here.
_AGENT_ALIASES = ("agent-coder", "agent-test-writer", "agent-verifier", "agent-planner",
                  "agent-investigator", "agent-coder-fallback", "agent-coder-frontend")

FIELDS = ("mem_pct", "mem_avail_gb", "cpu_pct", "load1", "disk_pct", "containers", "oom_kills",
          "router_calls", "router_inflight", "router_p50_s", "router_p90_s", "router_errors")


def _meminfo() -> tuple[float, float]:
    """(% used, GB available) the way `free` counts them."""
    total = avail = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = float(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail = float(line.split()[1])
    except OSError:
        pass
    if not total or avail is None:
        return 0.0, 0.0
    return round(100.0 * (total - avail) / total, 1), round(avail / 1024 / 1024, 1)


def _cpu_ticks() -> tuple[int, int]:
    """(busy, total) jiffies since boot, for a delta between two samples."""
    try:
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        nums = [int(p) for p in parts]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return sum(nums) - idle, sum(nums)
    except (OSError, ValueError, IndexError):
        return 0, 0


def _load1() -> float:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _disk_pct(path: str = "/var/lib/docker") -> float:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        u = shutil.disk_usage("/")
    return round(100.0 * u.used / (u.used + u.free), 1)


def _containers() -> int:
    try:
        r = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, timeout=10)
        return len([ln for ln in r.stdout.splitlines() if ln.strip()])
    except (OSError, subprocess.SubprocessError):
        return 0


def _oom_kills_since(epoch: float) -> int:
    """Kernel OOM kills since the run began. On this box every one so far
    was inside a task's container (2 GB, no swap), which is the design; the
    count says how often a test run outgrew it."""
    try:
        r = subprocess.run(["journalctl", "-k", "-q", "--no-pager", "-o", "cat", "--since", f"@{int(epoch)}"],
                           capture_output=True, text=True, timeout=15)
        return sum(1 for ln in r.stdout.splitlines() if "oom-kill" in ln or "Out of memory" in ln)
    except (OSError, subprocess.SubprocessError):
        return 0


def router_window(since: float, until: float, ledger: Path = LEDGER) -> dict:
    """The router's last interval: calls finished in it, the most in flight
    at once, median and p90 duration, and errors -- from the ledger's tail."""
    rows = []
    try:
        size = ledger.stat().st_size
        with ledger.open("rb") as fh:
            fh.seek(max(0, size - _LEDGER_TAIL_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")
        for line in chunk.splitlines()[1:] if size > _LEDGER_TAIL_BYTES else chunk.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = d.get("ts")
            if isinstance(ts, (int, float)) and since < ts <= until and d.get("alias") in _AGENT_ALIASES:
                rows.append(d)
    except OSError:
        pass
    durations = sorted(float(d.get("duration_s") or 0) for d in rows)
    events = []
    for d in rows:
        end = float(d["ts"])
        events.append((end - float(d.get("duration_s") or 0), 1))
        events.append((end, -1))
    events.sort()
    live = peak = 0
    for _, delta in events:
        live += delta
        peak = max(peak, live)
    return {
        "router_calls": len(rows),
        "router_inflight": peak,
        "router_p50_s": round(statistics.median(durations), 1) if durations else 0.0,
        "router_p90_s": round(durations[int(len(durations) * 0.9)], 1) if durations else 0.0,
        "router_errors": sum(1 for d in rows if d.get("error") or d.get("ok") is False),
    }


class Sampler:
    """One sample a minute to `<run_dir>/host.jsonl`, and the peaks so far."""

    def __init__(self, run_dir: Path, started: float, interval_s: int = INTERVAL_S):
        self.path = Path(run_dir) / "host.jsonl"
        self.started = started
        self.interval_s = interval_s
        self.peaks: dict[str, float] = {}
        self.samples = 0
        self.last: dict | None = None
        self._cpu = _cpu_ticks()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="host-sampler", daemon=True)

    def start(self) -> Sampler:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.interval_s + 5)

    def take(self, now: float | None = None) -> dict:
        now = now or time.time()
        busy, total = _cpu_ticks()
        pb, pt = self._cpu
        self._cpu = (busy, total)
        cpu = round(100.0 * (busy - pb) / (total - pt), 1) if total > pt else 0.0
        mem_pct, mem_avail = _meminfo()
        sample = {
            "t": int(now), "mem_pct": mem_pct, "mem_avail_gb": mem_avail, "cpu_pct": cpu,
            "load1": _load1(), "disk_pct": _disk_pct(), "containers": _containers(),
            "oom_kills": _oom_kills_since(self.started),
            **router_window(now - self.interval_s, now),
        }
        for k in FIELDS:
            v = sample.get(k)
            if isinstance(v, (int, float)):
                self.peaks[k] = max(self.peaks.get(k, 0), v)
        self.samples += 1
        self.last = sample
        try:
            with self.path.open("a") as fh:
                fh.write(json.dumps(sample) + "\n")
        except OSError:
            pass
        return sample

    def record(self) -> dict:
        """What summary.json carries: the peaks, not the series."""
        return {"interval_s": self.interval_s, "samples": self.samples, "peaks": self.peaks, "last": self.last}

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.take()
            except Exception:  # noqa: BLE001 -- a sample must never end a run
                pass


def read_samples(run_dir: Path, limit: int = 300) -> list[dict]:
    """The series for the page, thinned to at most `limit` points."""
    path = Path(run_dir) / "host.jsonl"
    try:
        rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]


def merge_series(series: list[list[dict]], limit: int = 300) -> list[dict]:
    """Shards of one run sample the same box: one series, one point per
    minute, the larger reading where two shards sampled the same minute."""
    by_minute: dict[int, dict] = {}
    for rows in series:
        for r in rows:
            key = int(r.get("t", 0)) // 60
            cur = by_minute.get(key)
            if cur is None:
                by_minute[key] = dict(r)
            else:
                for k in FIELDS:
                    if isinstance(r.get(k), (int, float)):
                        cur[k] = max(cur.get(k) or 0, r[k])
    rows = [by_minute[k] for k in sorted(by_minute)]
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]
