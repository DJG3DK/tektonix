import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SwebenchOverview, SwebenchRunSummary } from "../types";
import { SwebenchPanel } from "./SwebenchPanel";

const getSwebench = vi.fn();
const getSwebenchRun = vi.fn();
const getSwebenchTask = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getSwebench: () => getSwebench(),
    getSwebenchRun: (n: string) => getSwebenchRun(n),
    getSwebenchTask: (r: string, i: string) => getSwebenchTask(r, i),
  };
});

const summary = (over: Partial<SwebenchRunSummary> = {}): SwebenchRunSummary => ({
  name: "tektonix-sample50", kind: "sample", state: "running", notes: "seeded sample",
  started_at: "2026-09-24T14:00:00Z", finished_at: null, duration_s: null, total: 50, done: 12, graded: false,
  resolved: 7, graded_count: 10, resolved_rate: null, total_cost_usd: 3.2, stopped_early: null, parallel: 4,
  budget_usd: 3, models: {}, ...over,
});

const overview = (): SwebenchOverview => ({
  runs: [summary(), summary({ name: "diag-x", kind: "diagnostic", state: "done", notes: "experiment" })],
  dataset_size: 500,
  gold_check: { checked: 7, reference_fails: ["django__django-10097"] },
});

beforeEach(() => {
  getSwebench.mockReset().mockResolvedValue(overview());
  getSwebenchRun.mockReset().mockResolvedValue({
    summary: summary(),
    tasks: [
      { id: "django__django-11265", repo: "django/django", outcome: "shipped", reason: null, resolved: false,
        cost_usd: 0.33, duration_s: 3600, patch_bytes: 591, review_verdict: "READY", models: {}, started: true,
        reference_fails: false, has_trajectory: true, harness_note: "no_tests_collected",
        tests: { patch_applied: true, fail_to_pass_failed: ["test_with_exclude"], fail_to_pass_passed: 0,
          pass_to_pass_failed: [], pass_to_pass_passed: 30 } },
      { id: "sympy__sympy-1", repo: "sympy/sympy", outcome: "not_run", reason: null, cost_usd: null,
        duration_s: null, patch_bytes: null, review_verdict: null, models: {}, started: false,
        reference_fails: false, has_trajectory: false, tests: null },
    ],
  });
  getSwebenchTask.mockReset().mockResolvedValue({
    id: "django__django-11265", patch: "--- a/django/db/models/sql/query.py",
    review: { verdict: "READY", summary: "The change handles the nested case too.", findings: [], agentMessage: null },
    conversation: [{ generation: 0, namespace: "coordinator", messages: [
      { role: "human", name: null, text: "Resolve the following GitHub issue", tool_calls: [] },
      { role: "ai", name: null, text: "", tool_calls: [{ name: "read_file", args: "{}" }] },
    ] }],
  });
});

describe("SwebenchPanel", () => {
  it("shows a running sample as graded-so-far, never as a SWE-bench percentage", async () => {
    render(<SwebenchPanel />);
    expect((await screen.findAllByText("7/10")).length).toBeGreaterThan(0);
    expect(screen.getByText("resolved among the tasks graded so far")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("12 of 50 tasks done");
    expect(screen.queryByText("Copy scorecard")).not.toBeInTheDocument();
    expect(screen.getByText(/the official fix itself fails on/)).toHaveTextContent("django__django-10097");
  });

  it("opens a task: the failing graded test, the patch, and the agent's conversation", async () => {
    render(<SwebenchPanel />);
    await userEvent.click(await screen.findByText("django__django-11265"));
    expect(await screen.findByText("✗ test_with_exclude")).toBeInTheDocument();
    expect(await screen.findByText("--- a/django/db/models/sql/query.py")).toBeInTheDocument();
    await userEvent.click(screen.getByText(/Show the agent's conversation \(2 messages\)/));
    expect(screen.getByText("Resolve the following GitHub issue")).toBeInTheDocument();
    expect(screen.getByText("→ read_file")).toBeInTheDocument();
    expect(getSwebenchTask).toHaveBeenCalledWith("tektonix-sample50", "django__django-11265");
  });

  it("shows what the reviewer said, and the harness's own note as the harness's", async () => {
    render(<SwebenchPanel />);
    expect(await screen.findByText("harness: no_tests_collected")).toHaveAttribute("title", expect.stringContaining("harness"));
    await userEvent.click(screen.getByText("django__django-11265"));
    expect(await screen.findByText("Reviewer:")).toBeInTheDocument();
    expect(screen.getByText("The change handles the nested case too.")).toBeInTheDocument();
  });

  it("a task not run yet asks for nothing", async () => {
    render(<SwebenchPanel />);
    await userEvent.click(await screen.findByText("sympy__sympy-1"));
    expect(screen.getByText("Not run yet.")).toBeInTheDocument();
    await waitFor(() => expect(getSwebenchTask).not.toHaveBeenCalled());
  });
});
