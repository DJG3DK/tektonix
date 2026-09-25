import { describe, expect, it } from "vitest";
import type { SwebenchHost, SwebenchRunSummary } from "./types";
import { headlineRun, hostPeaks, modelList, scoreLabel, scoreText, swebenchScorecard } from "./swebenchFormat";

const run = (over: Partial<SwebenchRunSummary> = {}): SwebenchRunSummary => ({
  name: "r", kind: "sample", state: "done", notes: "", started_at: "2026-09-24T14:00:00Z", finished_at: null,
  duration_s: 3600, total: 50, done: 50, graded: true, resolved: 36, graded_count: 50, resolved_rate: 72,
  total_cost_usd: 20, stopped_early: null, parallel: 4, budget_usd: 3,
  models: { "agent-coder -> deepseek/deepseek-v4.1-flash": 40, "agent-planner -> z-ai/glm-5.3-flash": 3 }, ...over,
});

describe("SWE-bench wording", () => {
  it("never states a sample as a percentage on SWE-bench", () => {
    const text = swebenchScorecard(run(), ["django__django-10097"]);
    expect(text).toContain("resolved 36 of a 50-task sample of SWE-bench Verified");
    expect(text).not.toMatch(/\d+(\.\d)?%/);
    expect(text).toContain("django__django-10097");
    expect(scoreLabel(run())).toBe("resolved in a 50-task sample (72.0%) — not the published number");
    expect(scoreLabel(run({ kind: "selected", total: 4, resolved: 3, graded_count: 4 })))
      .toBe("resolved in a 4-task selection (75.0%) — not the published number");
  });

  it("gives a full run its percentage", () => {
    const full = run({ kind: "full", total: 500, resolved: 360, graded_count: 500 });
    expect(swebenchScorecard(full, [])).toContain("resolves 360 of the 500 SWE-bench Verified tasks (72.0%)");
    expect(scoreLabel(full)).toBe("resolved · 72.0%");
  });

  it("the headline is the newest full run, else the newest run that is a score", () => {
    const diag = run({ name: "d", kind: "diagnostic" });
    const sample = run({ name: "s" });
    const full = run({ name: "f", kind: "full" });
    expect(headlineRun([diag, sample, full])?.name).toBe("f");
    expect(headlineRun([diag, sample])?.name).toBe("s");
    expect(headlineRun([diag])).toBeUndefined();
  });

  it("a running run scores only what is graded", () => {
    const r = run({ graded: false, resolved: 7, graded_count: 10, state: "running" });
    expect(scoreText(r)).toBe("7/10");
    expect(scoreLabel(r)).toBe("resolved among the tasks graded so far");
    expect(scoreText(run({ graded: false, resolved: 0, graded_count: 0 }))).toBe("—");
  });

  it("names each role's model once", () => {
    expect(modelList(run().models)).toEqual(["coder: deepseek/deepseek-v4.1-flash", "planner: z-ai/glm-5.3-flash"]);
  });

  it("host peaks: the runner's peaks win, the lowest memory, OOM total and error total come from the series", () => {
    const host: SwebenchHost = {
      interval_s: 60, samples: 3,
      peaks: { mem_pct: 93, router_inflight: 11 },
      series: [
        { t: 0, mem_pct: 40, mem_avail_gb: 60, oom_kills: 0, router_errors: 0, router_inflight: 4, router_p90_s: 8 },
        { t: 60, mem_pct: 80, mem_avail_gb: 12.25, oom_kills: 1, router_errors: 2, router_inflight: 10, router_p90_s: 41 },
        { t: 120, mem_pct: 70, mem_avail_gb: 30, oom_kills: 3, router_errors: 1, router_inflight: 6 },
      ],
    };
    const by = Object.fromEntries(hostPeaks(host).map((p) => [p.key, p.value]));
    expect(by.mem_pct).toBe("93%");
    expect(by.mem_avail_gb).toBe("12.3 GB");
    expect(by.oom_kills).toBe("3");
    expect(by.router_errors).toBe("3");
    expect(by.router_inflight).toBe("11");
    expect(by.router_p90_s).toBe("41.0 s");
    expect(by.cpu_pct).toBe("—");
  });
});
