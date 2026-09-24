import { describe, expect, it } from "vitest";
import type { SwebenchRunSummary } from "./types";
import { headlineRun, modelList, scoreLabel, scoreText, swebenchScorecard } from "./swebenchFormat";

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
    expect(scoreLabel(run())).toContain("not the published number");
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
});
