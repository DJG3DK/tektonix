import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiffPanel } from "./DiffPanel";

const getTaskDiff = vi.fn();
const getTaskFile = vi.fn();
const saveOperatorEdits = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getTaskDiff: (...a: unknown[]) => getTaskDiff(...a),
    getTaskFile: (...a: unknown[]) => getTaskFile(...a),
    saveOperatorEdits: (...a: unknown[]) => saveOperatorEdits(...a),
  };
});

// Monaco cannot run in jsdom; a textarea stands in with the same contract.
vi.mock("./CodeEditor", () => ({
  default: ({ modified, onChange }: { modified: string; onChange: (v: string) => void }) => (
    <textarea aria-label="editor" defaultValue={modified} onChange={(e) => onChange(e.target.value)} />
  ),
}));

const DIFF = {
  base: "base000", head: "sha111", branch: "agent/t1", total_additions: 1, total_deletions: 1,
  files: [{ path: "src/app.js", additions: 1, deletions: 1, patch: "-a\n+b", binary: false, untracked: false }],
};

beforeEach(() => {
  getTaskDiff.mockReset().mockResolvedValue(DIFF);
  getTaskFile.mockReset().mockResolvedValue({
    path: "src/app.js", original: "const a = 1;\n", modified: "const a = 2;\n", sha: "sha111", base: "base000",
  });
  saveOperatorEdits.mockReset().mockResolvedValue(undefined);
});

const props = (over = {}) => ({
  taskId: "t1", open: true, onClose: vi.fn(), live: false, awaitingMerge: true, onDecided: vi.fn(), ...over,
});

describe("DiffPanel hand edits", () => {
  it("offers Edit only in the final look", async () => {
    const { unmount } = render(<DiffPanel {...props({ awaitingMerge: false })} />);
    await screen.findByText("src/app.js");
    expect(screen.queryByRole("button", { name: /edit src\/app.js/i })).toBeNull();
    unmount();
    render(<DiffPanel {...props()} />);
    expect(await screen.findByRole("button", { name: /edit src\/app.js/i })).toBeInTheDocument();
  });

  it("saves the edited file against the commit it was opened from", async () => {
    const p = props();
    render(<DiffPanel {...p} />);
    await userEvent.click(await screen.findByRole("button", { name: /edit src\/app.js/i }));
    const editor = await screen.findByLabelText("editor");
    await userEvent.clear(editor);
    await userEvent.type(editor, "const a = 3;");
    await userEvent.click(screen.getByRole("button", { name: /all files/i }));
    expect(screen.getByText("edited")).toBeInTheDocument();

    await userEvent.type(screen.getByPlaceholderText(/what did you fix/i), "wrong constant");
    await userEvent.click(screen.getByRole("button", { name: /save 1 file & re-check/i }));

    expect(saveOperatorEdits).toHaveBeenCalledWith(
      "t1", "sha111", [{ path: "src/app.js", content: "const a = 3;" }], "wrong constant");
    expect(p.onDecided).toHaveBeenCalled();
  });

  it("an edit typed and then undone is not a save", async () => {
    render(<DiffPanel {...props()} />);
    await userEvent.click(await screen.findByRole("button", { name: /edit src\/app.js/i }));
    const editor = await screen.findByLabelText("editor");
    await userEvent.type(editor, "x");
    await userEvent.type(editor, "{backspace}");
    expect(screen.queryByRole("button", { name: /save .* re-check/i })).toBeNull();
    expect(screen.getByRole("button", { name: /approve & merge/i })).toBeInTheDocument();
  });

  it("says why a file cannot be opened", async () => {
    getTaskFile.mockRejectedValue(new Error("src/app.js is binary"));
    render(<DiffPanel {...props()} />);
    await userEvent.click(await screen.findByRole("button", { name: /edit src\/app.js/i }));
    expect(await screen.findByText("src/app.js is binary")).toBeInTheDocument();
  });
});
