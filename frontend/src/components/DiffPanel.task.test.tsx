import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiffPanel } from "./DiffPanel";

/* The panel is where Approve & merge lives, so what it shows must be the
 * task it is for, and the decision must name the commit it showed
 * (2026-09-29 audit, U3). */

const getTaskDiff = vi.fn();
const submitMergeDecision = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getTaskDiff: (...a: unknown[]) => getTaskDiff(...a),
    submitMergeDecision: (...a: unknown[]) => submitMergeDecision(...a),
  };
});

function diffFor(task: string, head: string) {
  return {
    base: "base000", head, branch: `agent/${task}`, total_additions: 1, total_deletions: 0,
    files: [{ path: `src/${task}.js`, additions: 1, deletions: 0, patch: "+x", binary: false, untracked: false }],
  };
}

const props = (over = {}) => ({
  taskId: "A", open: true, onClose: vi.fn(), live: false, awaitingMerge: true, onDecided: vi.fn(), ...over,
});

beforeEach(() => {
  getTaskDiff.mockReset();
  submitMergeDecision.mockReset().mockResolvedValue(undefined);
});

describe("DiffPanel per task", () => {
  it("a slow diff for the task just left does not land on the new task's panel", async () => {
    let settleA: (v: unknown) => void = () => {};
    getTaskDiff.mockImplementation(async (id: string) => {
      if (id === "A") return new Promise((r) => (settleA = r));
      return diffFor("B", "shaB");
    });
    const { rerender } = render(<DiffPanel {...props({ taskId: "A" })} />);
    rerender(<DiffPanel {...props({ taskId: "B" })} />);
    await screen.findByText("src/B.js");

    await act(async () => { settleA(diffFor("A", "shaA")); await Promise.resolve(); });
    expect(screen.getByText("src/B.js")).toBeInTheDocument();
    expect(screen.queryByText("src/A.js")).toBeNull();
  });

  it("switching task clears the previous task's diff and notes at once", async () => {
    getTaskDiff.mockImplementation(async (id: string) => diffFor(id, `sha${id}`));
    const { rerender } = render(<DiffPanel {...props({ taskId: "A" })} />);
    await screen.findByText("src/A.js");
    await userEvent.click(screen.getByRole("button", { name: /send back/i }));
    await userEvent.type(screen.getByPlaceholderText(/what should the agent change/i), "notes for A");

    // Hold B's diff so the moment right after the switch is observable.
    getTaskDiff.mockImplementation(() => new Promise(() => {}));
    rerender(<DiffPanel {...props({ taskId: "B" })} />);
    expect(screen.queryByText("src/A.js")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("notes for A")).toBeNull();
  });

  it("a decision names the commit the panel showed", async () => {
    getTaskDiff.mockResolvedValue(diffFor("A", "shaA"));
    vi.stubGlobal("confirm", () => true);
    render(<DiffPanel {...props()} />);
    await screen.findByText("src/A.js");
    await userEvent.click(screen.getByRole("button", { name: /approve & merge/i }));
    expect(submitMergeDecision).toHaveBeenCalledWith("A", "approve", undefined, "shaA");
    vi.unstubAllGlobals();
  });
});
