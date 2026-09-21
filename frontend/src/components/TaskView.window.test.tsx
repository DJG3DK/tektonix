/**
 * A long task log must not put every entry in the DOM.
 *
 * useTaskStream caps the array at 3,000 entries and ChatMessage is memoized
 * (audit H-16), so appending does not re-render the rows above it. Both are
 * about RE-rendering; neither stops 3,000 rows being MOUNTED, and a
 * multi-hour task makes the tab slow long before it reaches the cap.
 *
 * The window is anchored to the end because that is where a log is read
 * from, and it grows on request rather than from an estimated row height:
 * a one-line status and a rendered diff differ by two orders of magnitude,
 * and a spacer sized from the average of those is a scrollbar that lies.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { TaskView } from "./TaskView";
import type { TaskMeta } from "../types";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getTaskDiff: vi.fn().mockResolvedValue({ files: [], diff: "" }),
  sendMessage: vi.fn(),
}));

const task = (over: Partial<TaskMeta> = {}): TaskMeta => ({
  task_id: "t-0001", repo: "proj", goal: "a goal", status: "running",
  created_at: 0, updated_at: 0, cost_so_far: 0, budget_usd: 5,
  ...over,
} as TaskMeta);

/** `n` real-shaped entries, each distinguishable so a slice can be identified. */
function longLog(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    node: "work" as const,
    step_id: null,
    summary: `entry number ${i}`,
    detail: `entry number ${i}`,
    cost_usd: 0,
    timestamp: new Date(1_700_000_000_000 + i * 1000).toISOString(),
  }));
}

function streamWith(log: unknown[]) {
  // The real StreamState, so this renders the real component rather than a
  // shape only this test believes in.
  return {
    log, plan: [], currentStepIndex: 0, costSoFar: 0, escalated: false,
    escalationReason: null, reviewGateResult: null, pendingApproval: null,
    committedSha: null, status: "running", connected: true, hydrateError: null,
    idleSeconds: 0, orphaned: false,
  } as unknown as Parameters<typeof TaskView>[0]["stream"];
}

function renderLog(n: number) {
  return render(
    <TaskView task={task()} stream={streamWith(longLog(n))} setGeneration={() => {}} />,
  );
}

describe("the task log's DOM", () => {
  it("does not mount three thousand rows for a three-thousand-entry log", () => {
    renderLog(3000);
    const rows = document.querySelectorAll(".chat-row");
    // The goal bubble is a chat-row too, hence the slack.
    expect(rows.length).toBeLessThan(400);
    expect(rows.length).toBeGreaterThan(0);
  });

  it("keeps the END of the log, which is the part being read", () => {
    renderLog(3000);
    expect(screen.getByText(/entry number 2999/)).toBeInTheDocument();
    expect(screen.queryByText(/entry number 0\b/)).not.toBeInTheDocument();
  });

  it("says how much is above, and reveals more when asked", async () => {
    const user = userEvent.setup();
    renderLog(3000);
    const earlier = screen.getByRole("button", { name: /Show earlier/ });
    expect(earlier).toHaveTextContent("2800 more entries");

    const before = document.querySelectorAll(".chat-row").length;
    await user.click(earlier);
    expect(document.querySelectorAll(".chat-row").length).toBeGreaterThan(before);
    expect(screen.getByText(/entry number 2999/)).toBeInTheDocument();
  });

  it("shows no control at all for a log that already fits", () => {
    renderLog(12);
    expect(screen.queryByRole("button", { name: /Show earlier/ })).not.toBeInTheDocument();
    expect(screen.getByText(/entry number 0\b/)).toBeInTheDocument();
    expect(screen.getByText(/entry number 11/)).toBeInTheDocument();
  });
});
