import { describe, expect, it } from "vitest";
import { compareRuns, minutes, scorecardText } from "./evalsFormat";
import type { EvalRunSummary } from "./types";

const run = (over: Partial<EvalRunSummary> = {}): EvalRunSummary => ({
  name: "2026-09-23T12-00-00Z", started_at: "2026-09-23T12:00:00Z", finished_at: "2026-09-23T13:10:00Z",
  duration_s: 4200, notes: "", tasks_total: 3, tasks_attempted: 3, tasks_passed: 2, pass_rate: 66.7,
  total_cost_usd: 0.3, stopped_early: false, full: true,
  by_category: { "bug-fix": { tasks: 2, passed: 2 }, security: { tasks: 1, passed: 0 } },
  benchmarks: { first_pass_rate: 100, escalated: 0 }, failed: ["c"], results: { a: true, b: true, c: false },
  ...over,
});

describe("evals format", () => {
  it("compares only the tasks both runs had", () => {
    const now = run({ results: { a: true, b: false, c: true, new: false } });
    const before = run({ results: { a: true, b: true, c: false } });
    expect(compareRuns(now, before)).toEqual({ regressed: ["b"], fixed: ["c"] });
    expect(compareRuns(now, undefined)).toEqual({ regressed: [], fixed: [] });
  });

  it("minutes reads naturally at both ends", () => {
    expect(minutes(90)).toBe("2 min");
    expect(minutes(4200)).toBe("1 h 10 min");
    expect(minutes(null)).toBe("—");
  });

  it("the scorecard is one quotable paragraph with the numbers that matter", () => {
    const t = scorecardText(run());
    expect(t).toContain("2/3 tasks passed (67%)");
    expect(t).toContain("100% passed independent review first time");
    expect(t).toContain("$0.30 total ($0.10 per task), 1 h 10 min");
    expect(t).toContain("By category: bug-fix 2/2, security 0/1.");
  });
});
