import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiffPanel } from "./DiffPanel";

const getTaskDiff = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getTaskDiff: (...a: unknown[]) => getTaskDiff(...a),
    decideMerge: vi.fn(async () => {}),
  };
});

beforeEach(() => {
  getTaskDiff.mockReset();
});

describe("DiffPanel", () => {
  const props = () => ({
    taskId: "t1",
    open: true,
    onClose: vi.fn(),
    live: false,
    awaitingMerge: false,
    onDecided: vi.fn(),
  });

  it("fetches nothing while closed", () => {
    render(<DiffPanel {...props()} open={false} />);
    expect(getTaskDiff).not.toHaveBeenCalled();
  });

  it("reports how many files changed, pluralised", async () => {
    getTaskDiff.mockResolvedValue({ base: "abc1234567def", files: [{ path: "a.ts", additions: 1, deletions: 0, patch: "+x" }] });
    render(<DiffPanel {...props()} />);
    expect(await screen.findByText("1 file")).toBeInTheDocument();
  });

  it("pluralises correctly for more than one", async () => {
    getTaskDiff.mockResolvedValue({
      base: "abc1234567def",
      files: [
        { path: "a.ts", additions: 1, deletions: 0, patch: "+x" },
        { path: "b.ts", additions: 2, deletions: 1, patch: "+y" },
      ],
    });
    render(<DiffPanel {...props()} />);
    expect(await screen.findByText("2 files")).toBeInTheDocument();
  });

  it("says so explicitly when a task produced no diff", async () => {
    // An empty panel would read as "still loading" forever.
    getTaskDiff.mockResolvedValue({ files: [], base: "abc1234567def" });
    render(<DiffPanel {...props()} />);
    expect(await screen.findByText(/no changes against abc1234567/i)).toBeInTheDocument();
  });

  it("surfaces a load failure rather than showing a blank panel", async () => {
    getTaskDiff.mockRejectedValue(new Error("diff unavailable"));
    render(<DiffPanel {...props()} />);
    expect(await screen.findByText(/diff unavailable/i)).toBeInTheDocument();
  });

  it("closes on request", async () => {
    getTaskDiff.mockResolvedValue({ files: [], base: "abc1234567def" });
    const p = props();
    render(<DiffPanel {...p} />);
    await userEvent.click(await screen.findByRole("button", { name: /close/i }));
    expect(p.onClose).toHaveBeenCalled();
  });
});
