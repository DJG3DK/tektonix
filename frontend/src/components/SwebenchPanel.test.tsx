import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SwebenchHost, SwebenchOverview, SwebenchRunSummary } from "../types";
import { SwebenchPanel } from "./SwebenchPanel";

const getSwebench = vi.fn();
const getSwebenchRun = vi.fn();
const getSwebenchTask = vi.fn();
const getSwebenchRunLog = vi.fn();
const startSwebenchRun = vi.fn();
const stopSwebenchRun = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getSwebench: () => getSwebench(),
    getSwebenchRun: (n: string) => getSwebenchRun(n),
    getSwebenchTask: (r: string, i: string) => getSwebenchTask(r, i),
    getSwebenchRunLog: (n: string, l?: number) => getSwebenchRunLog(n, l),
    startSwebenchRun: (o: unknown) => startSwebenchRun(o),
    stopSwebenchRun: (n: string) => stopSwebenchRun(n),
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

/** Six minutes of a run: memory climbing to 91%, two OOM kills, one error. */
const host = (): SwebenchHost => ({
  interval_s: 60,
  samples: 6,
  peaks: { mem_pct: 91, cpu_pct: 72.4, load1: 19.5, disk_pct: 63, containers: 7, oom_kills: 2, router_inflight: 9,
    router_p90_s: 48.2, router_errors: 1 },
  series: [
    { t: 1_000, mem_pct: 40, mem_avail_gb: 70.2, cpu_pct: 20, load1: 4, disk_pct: 60, containers: 3, oom_kills: 0,
      router_inflight: 3, router_p90_s: 12.5, router_errors: 0 },
    { t: 1_060, mem_pct: 65, mem_avail_gb: 40.9, cpu_pct: 72.4, load1: 19.5, disk_pct: 61, containers: 6, oom_kills: 0,
      router_inflight: 9, router_p90_s: 48.2, router_errors: 1 },
    { t: 1_120, mem_pct: 91, mem_avail_gb: 10.4, cpu_pct: 60, load1: 15, disk_pct: 63, containers: 7, oom_kills: 2,
      router_inflight: 5, router_p90_s: 30, router_errors: 0 },
  ],
});

const runDetail = (over: Record<string, unknown> = {}) => ({
  summary: summary(),
  host: null,
  ...over,
});

beforeEach(() => {
  getSwebench.mockReset().mockResolvedValue(overview());
  getSwebenchRun.mockReset().mockResolvedValue(runDetail({
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
  }));
  getSwebenchRunLog.mockReset().mockResolvedValue({ lines: ["task 12 done", "pulling image for task 13"] });
  startSwebenchRun.mockReset().mockResolvedValue({ ok: true, name: "tektonix-sample50-2", shards: [] });
  stopSwebenchRun.mockReset().mockResolvedValue({ ok: true, stopped: ["tektonix-sample50"] });
  vi.spyOn(window, "confirm").mockReturnValue(true);
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

  it("shows the host during the run: the peaks and four sparklines", async () => {
    getSwebenchRun.mockResolvedValue(runDetail({ tasks: [], host: host() }));
    render(<SwebenchPanel />);
    const block = await screen.findByRole("region", { name: "Host during the run" });
    expect(block).toHaveTextContent("Memory peak91%");
    expect(block).toHaveTextContent("Memory available, lowest10.4 GB");
    expect(block).toHaveTextContent("CPU peak72%");
    expect(block).toHaveTextContent("Load average peak (1 min)19.5");
    expect(block).toHaveTextContent("Docker disk peak63%");
    expect(block).toHaveTextContent("Containers running, peak7");
    expect(block).toHaveTextContent("OOM kills in task containers2");
    expect(block).toHaveTextContent("Model calls in flight, peak9");
    expect(block).toHaveTextContent("p90 model-call latency, worst minute48.2 s");
    expect(block).toHaveTextContent("Model-call errors1");
    for (const f of ["mem_pct", "router_inflight", "router_p90_s", "containers"]) {
      expect(screen.getByTestId(`sparkline-${f}`).querySelector("path.swebench-spark-line")).not.toBeNull();
    }
    expect(screen.getByTestId("sparkline-mem_pct")).toHaveAttribute("aria-label", "Memory used over the run: now 91%, peak 91%");
    expect(screen.getByTestId("sparkline-router_inflight")).toHaveAttribute("aria-label", "Model calls in flight over the run: now 5, peak 9");
    expect(block).toHaveTextContent("Sampled every 60 s by the runner; every figure is host-wide (shards of one run share the box).");
  });

  it("a run from before host sampling shows no host block", async () => {
    render(<SwebenchPanel />);
    await screen.findByText("django__django-11265");
    expect(screen.queryByRole("region", { name: "Host during the run" })).not.toBeInTheDocument();
    expect(screen.queryByText("Host during the run")).not.toBeInTheDocument();
  });

  it("a task not run yet asks for nothing", async () => {
    render(<SwebenchPanel />);
    await userEvent.click(await screen.findByText("sympy__sympy-1"));
    expect(screen.getByText("Not run yet.")).toBeInTheDocument();
    await waitFor(() => expect(getSwebenchTask).not.toHaveBeenCalled());
  });

  it("offers the Start controls with their defaults", async () => {
    render(<SwebenchPanel />);
    const box = await screen.findByRole("group", { name: "Start a run" });
    expect(within(box).getByLabelText("Sample")).toHaveValue("50");
    expect(within(box).getByRole("option", { name: "All 500" })).toHaveValue("500");
    expect(within(box).getByLabelText("Seed")).toHaveValue(1);
    expect(within(box).getByLabelText("Tasks at once")).toHaveValue(10);
    expect(within(box).getByLabelText("Per-task budget $")).toHaveValue(3);
  });

  it("Start is disabled, with the reason, while a real run is in progress", async () => {
    render(<SwebenchPanel />);
    const start = await screen.findByRole("button", { name: "Start" });
    expect(start).toBeDisabled();
    expect(screen.getByText(/A run is already in progress \(tektonix-sample50\)/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("a diagnostic run does not hold the box", async () => {
    getSwebench.mockResolvedValue({
      ...overview(),
      runs: [summary({ name: "diag-live", kind: "diagnostic", state: "running", graded_count: 0, resolved: null })],
    });
    render(<SwebenchPanel />);
    expect(await screen.findByRole("button", { name: "Start" })).toBeEnabled();
  });

  it("Start confirms what will happen, then sends the sample, seed, parallelism and budget", async () => {
    getSwebench.mockResolvedValue({
      ...overview(),
      runs: [summary({ state: "done", graded: true, resolved: 20, graded_count: 50, resolved_rate: 40 })],
    });
    render(<SwebenchPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    const dialog = screen.getByRole("dialog", { name: "Start a SWE-bench run" });
    expect(dialog).toHaveTextContent("50 tasks ran about $18 and 4 to 6 hours at ten at once; 500 is roughly ten times that");
    expect(dialog).toHaveTextContent("roughly $18 and 4 to 6 hours");
    expect(startSwebenchRun).not.toHaveBeenCalled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Start run" }));
    expect(startSwebenchRun).toHaveBeenCalledWith({ sample: 50, seed: 1, parallel: 10, budget_usd: 3 });
    // The confirm closes and the runs list is reloaded.
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(getSwebench.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("the confirm's rough line and the request follow the fields", async () => {
    getSwebench.mockResolvedValue({ ...overview(), runs: [] });
    render(<SwebenchPanel />);
    const box = await screen.findByRole("group", { name: "Start a run" });
    await userEvent.selectOptions(within(box).getByLabelText("Sample"), "500");
    await userEvent.clear(within(box).getByLabelText("Seed"));
    await userEvent.type(within(box).getByLabelText("Seed"), "7");
    await userEvent.clear(within(box).getByLabelText("Tasks at once"));
    await userEvent.type(within(box).getByLabelText("Tasks at once"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("roughly $180 and 80 to 120 hours");
    await userEvent.type(within(dialog).getByPlaceholderText(/What changed/), "new planner prompt");
    await userEvent.click(within(dialog).getByRole("button", { name: "Start run" }));
    expect(startSwebenchRun).toHaveBeenCalledWith({ sample: 500, seed: 7, parallel: 5, budget_usd: 3, notes: "new planner prompt" });
  });

  it("out-of-range fields are clamped to the contract's limits", async () => {
    getSwebench.mockResolvedValue({ ...overview(), runs: [] });
    render(<SwebenchPanel />);
    const box = await screen.findByRole("group", { name: "Start a run" });
    await userEvent.clear(within(box).getByLabelText("Tasks at once"));
    await userEvent.type(within(box).getByLabelText("Tasks at once"), "40");
    await userEvent.clear(within(box).getByLabelText("Per-task budget $"));
    await userEvent.type(within(box).getByLabelText("Per-task budget $"), "25");
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("16 at once, up to $10.00 per task");
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(startSwebenchRun).toHaveBeenCalledWith({ sample: 50, seed: 1, parallel: 16, budget_usd: 10 });
  });

  it("Cancel closes the confirm without starting anything", async () => {
    getSwebench.mockResolvedValue({ ...overview(), runs: [] });
    render(<SwebenchPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(startSwebenchRun).not.toHaveBeenCalled();
  });

  it("a refused start (a run already in progress) shows the server's reason", async () => {
    getSwebench.mockResolvedValue({ ...overview(), runs: [] });
    startSwebenchRun.mockRejectedValue(new Error("a run is already in progress: tektonix-sample50"));
    render(<SwebenchPanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("a run is already in progress: tektonix-sample50");
  });

  it("Stop on the running headline asks first, then stops the shown run and reloads", async () => {
    render(<SwebenchPanel />);
    const progress = await screen.findByRole("status");
    await userEvent.click(within(progress).getByRole("button", { name: "Stop" }));
    expect(window.confirm).toHaveBeenCalledWith(
      "Stop this run? Tasks already finished keep their results; nothing is graded until you grade it later.",
    );
    expect(stopSwebenchRun).toHaveBeenCalledWith("tektonix-sample50");
    await waitFor(() => expect(getSwebench.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("a declined Stop does nothing", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<SwebenchPanel />);
    const progress = await screen.findByRole("status");
    await userEvent.click(within(progress).getByRole("button", { name: "Stop" }));
    expect(stopSwebenchRun).not.toHaveBeenCalled();
  });

  it("every running run in the list has its own Stop; finished ones do not", async () => {
    render(<SwebenchPanel />);
    await screen.findByRole("status");
    const list = screen.getByRole("list");
    expect(within(list).getAllByRole("button", { name: /^Stop / })).toHaveLength(1);
    await userEvent.click(within(list).getByRole("button", { name: "Stop tektonix-sample50" }));
    expect(stopSwebenchRun).toHaveBeenCalledWith("tektonix-sample50");
  });

  it("the runner log is fetched when opened, not before", async () => {
    render(<SwebenchPanel />);
    await screen.findByText("django__django-11265");
    expect(getSwebenchRunLog).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText(/Runner log/));
    await waitFor(() => expect(getSwebenchRunLog).toHaveBeenCalledWith("tektonix-sample50", 80));
    expect(await screen.findByText(/pulling image for task 13/)).toBeInTheDocument();
  });
});
