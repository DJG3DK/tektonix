import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EvalsOverview, SwebenchOverview } from "../types";
import { BenchmarksView } from "./BenchmarksView";

const getEvals = vi.fn();
const getSwebench = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getEvals: () => getEvals(),
    getSwebench: () => getSwebench(),
    getEvalRun: async () => ({ summary: {}, tasks: [] }),
    getSwebenchRun: async () => ({ summary: {}, tasks: [], host: null }),
  };
});

const evals: EvalsOverview = {
  runs: [],
  status: null,
  suite: { tasks: 2, by_category: { "bug-fix": 2 }, ids: ["a", "b"] },
  estimate: { cost_usd: null, duration_s: null, from_run: null },
};
const swebench: SwebenchOverview = { runs: [], dataset_size: 500, gold_check: { checked: 0, reference_fails: [] } };

beforeEach(() => {
  getEvals.mockReset().mockResolvedValue(evals);
  getSwebench.mockReset().mockResolvedValue(swebench);
});

describe("BenchmarksView", () => {
  it("renders the title, the line of context, and both suites", async () => {
    render(<BenchmarksView />);
    expect(screen.getByRole("heading", { level: 1, name: "Benchmarks" })).toBeInTheDocument();
    expect(screen.getByText(/The golden suite is ours; SWE-bench Verified is the public benchmark/)).toBeInTheDocument();
    // Wait for both panels to have loaded their own data (the suite's size,
    // "No runs yet.") before reading the headings: the loading branch has
    // the same h2, and it is replaced when the data lands.
    expect(await screen.findByText(/2 fixed coding tasks/)).toBeInTheDocument();
    expect(await screen.findByText("No runs yet.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Golden suite" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "SWE-bench Verified" })).toBeInTheDocument();
  });
});
