import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { BenchmarkPanel, deltaTone, formatDelta, formatValue } from "./BenchmarkPanel";
import type { BenchmarkWindow, Benchmarks } from "../types";

function win(over: Partial<BenchmarkWindow> = {}): BenchmarkWindow {
  return {
    tasks: 20, shipped: 16, escalated: 4,
    outcomes: { shipped: 16, escalated: 4 },
    ship_rate: 80, escalation_rate: 20, first_pass_rate: 60,
    reviewed: 18, first_pass: 11,
    iterations_median: 2, iterations_p90: 5,
    cost_median: 1.2, cost_p90: 4, cost_per_shipped_median: 1.1,
    total_cost: 24,
    memory_prompts: 40, sections_offered: 120, sections_read: 20,
    section_reads_per_prompt: 0.5,
    history_queries: 10, history_used: 4, history_follow_rate: 40,
    ...over,
  };
}

function data(over: Partial<Benchmarks> = {}): Benchmarks {
  return {
    window_days: 14, current: win(), previous: win(),
    delta: {}, sample_warning: null, ...over,
  };
}

describe("deltaTone", () => {
  it("treats a falling escalation rate as an improvement", () => {
    expect(deltaTone("escalation_rate", -5)).toBe("good");
    expect(deltaTone("escalation_rate", 5)).toBe("bad");
  });

  it("treats a falling first-pass rate as a regression", () => {
    // The whole reason the direction table exists: both metrics moved down,
    // and colouring them the same would be wrong for one of them.
    expect(deltaTone("first_pass_rate", -5)).toBe("bad");
    expect(deltaTone("first_pass_rate", 5)).toBe("good");
  });

  it("wants fix cycles and cost to go down", () => {
    expect(deltaTone("iterations_median", -1)).toBe("good");
    expect(deltaTone("cost_per_shipped_median", 0.5)).toBe("bad");
  });

  it("colours a diagnostic metric neither way", () => {
    expect(deltaTone("section_reads_per_prompt", 2)).toBe("flat");
    expect(deltaTone("section_reads_per_prompt", -2)).toBe("flat");
  });

  it("says nothing when there was no prior window", () => {
    expect(deltaTone("first_pass_rate", undefined)).toBe("none");
  });

  it("does not claim an improvement when nothing moved", () => {
    expect(deltaTone("first_pass_rate", 0)).toBe("flat");
  });
});

describe("formatting", () => {
  it("renders an absent measurement as a dash, never as zero", () => {
    expect(formatValue(null, "pct")).toBe("—");
    expect(formatValue(undefined, "usd")).toBe("—");
    expect(formatValue(0, "pct")).toBe("0%");
  });

  it("labels a percentage-point move as points, not a percentage", () => {
    // 60% -> 45% is 15 POINTS, not 15%. The panel has to say which.
    expect(formatDelta(-15, "pct")).toBe("−15 pts");
    expect(formatDelta(15, "pct")).toBe("+15 pts");
  });

  it("formats money and counts", () => {
    expect(formatDelta(-0.4, "usd")).toBe("−$0.40");
    expect(formatDelta(0, "num")).toBe("±0.0");
    expect(formatValue(1.25, "usd")).toBe("$1.25");
  });
});

describe("<BenchmarkPanel />", () => {
  it("shows the six headline metrics", () => {
    render(<BenchmarkPanel data={data()} />);
    for (const label of [
      "First-pass reviews", "Fix cycles (median)", "Escalations",
      "Cost per shipped task", "History searches used", "Memory reads / prompt",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getByText("11 of 18 reviewed", { exact: false })).toBeInTheDocument();
  });

  it("warns when the sample is too thin to read the deltas", () => {
    render(<BenchmarkPanel data={data({
      sample_warning: "too few tasks for a meaningful comparison",
      current: win({ tasks: 3 }),
    })} />);
    expect(screen.getByRole("note")).toHaveTextContent("Small sample");
  });

  it("carries no warning when both windows are full", () => {
    render(<BenchmarkPanel data={data()} />);
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("says 'no prior window' rather than showing a zero delta", () => {
    render(<BenchmarkPanel data={data({ delta: {} })} />);
    expect(screen.getAllByText("no prior window").length).toBe(6);
  });

  it("colours each delta by whether the metric got better", () => {
    const { container } = render(<BenchmarkPanel data={data({
      delta: { first_pass_rate: -10, escalation_rate: -10 },
    })} />);
    const good = container.querySelectorAll(".bench-delta-good");
    const bad = container.querySelectorAll(".bench-delta-bad");
    expect(good.length).toBe(1);   // escalations fell
    expect(bad.length).toBe(1);    // first-pass fell too, and that is worse
    expect(good[0].textContent).toBe("−10 pts");
  });

  it("renders a loading state rather than an empty page", () => {
    render(<BenchmarkPanel data={null} />);
    expect(screen.getByText("Benchmarks")).toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("renders windows with no data at all without crashing", () => {
    const empty = win({
      tasks: 0, shipped: 0, escalated: 0, outcomes: {}, ship_rate: null,
      escalation_rate: null, first_pass_rate: null, reviewed: 0, first_pass: 0,
      iterations_median: null, iterations_p90: null, cost_median: null,
      cost_p90: null, cost_per_shipped_median: null, total_cost: 0,
      memory_prompts: 0, sections_offered: 0, sections_read: 0,
      section_reads_per_prompt: null, history_queries: 0, history_used: 0,
      history_follow_rate: null,
    });
    const { container } = render(
      <BenchmarkPanel data={data({ current: empty, previous: empty })} />);
    expect(container.querySelectorAll(".analytics-card").length).toBe(6);
    expect(screen.getAllByText("—").length).toBe(6);
  });
});

describe("<BenchmarkPanel /> when the fetch fails", () => {
  it.each(["route-missing", "getBenchmarks failed: 404"])(
    "names the cause for %s instead of spinning forever", (err) => {
      // What a frontend deployed ahead of its backend looks like: the page
      // knows about a route the running process does not have.
      render(<BenchmarkPanel data={null} error={err} />);
      expect(screen.getByRole("note")).toHaveTextContent("after the next restart");
      expect(screen.queryByText("Loading…")).toBeNull();
    });

  it("shows any other failure rather than hiding it", () => {
    render(<BenchmarkPanel data={null} error="NetworkError" />);
    expect(screen.getByRole("note")).toHaveTextContent("Couldn't load benchmarks: NetworkError");
  });

  it("prefers the error over stale data", () => {
    render(<BenchmarkPanel data={data()} error="getBenchmarks failed: 500" />);
    expect(screen.queryByText("First-pass reviews")).toBeNull();
  });
});
