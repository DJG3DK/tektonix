import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { _resetDefaultTaskBudgetCache, useBudgetInput, useDefaultTaskBudget } from "./useDefaultTaskBudget";

const getTaskDefaults = vi.fn();
const getRuntimeSettings = vi.fn();
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getTaskDefaults: (...a: unknown[]) => getTaskDefaults(...a),
    getRuntimeSettings: (...a: unknown[]) => getRuntimeSettings(...a),
  };
});

function Probe() {
  const v = useDefaultTaskBudget();
  return <span data-testid="v">{v}</span>;
}

function Input() {
  const [budget, setBudget] = useBudgetInput();
  return <input data-testid="b" type="number" value={budget} onChange={(e) => setBudget(parseFloat(e.target.value) || 0)} />;
}

describe("default task budget", () => {
  beforeEach(() => {
    _resetDefaultTaskBudgetCache();
    getTaskDefaults.mockReset();
    getRuntimeSettings.mockReset();
  });

  it("shows the configured default once settings load", async () => {
    getTaskDefaults.mockResolvedValue({ default_task_budget_usd: 10 });
    render(<Probe />);
    await waitFor(() => expect(screen.getByTestId("v").textContent).toBe("10"));
  });

  it("reads the route every account can, not the admin-only runtime settings", async () => {
    getTaskDefaults.mockResolvedValue({ default_task_budget_usd: 10 });
    render(<Probe />);
    await waitFor(() => expect(getTaskDefaults).toHaveBeenCalled());
    expect(getRuntimeSettings).not.toHaveBeenCalled();
  });

  it("falls back to $2 when settings cannot be read", async () => {
    getTaskDefaults.mockRejectedValue(new Error("503"));
    render(<Probe />);
    await waitFor(() => expect(getTaskDefaults).toHaveBeenCalled());
    expect(screen.getByTestId("v").textContent).toBe("2");
  });

  it("a value the user typed is not overwritten when the default arrives", async () => {
    let resolve!: (v: unknown) => void;
    getTaskDefaults.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<Input />);
    const input = screen.getByTestId("b") as HTMLInputElement;
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(input, { target: { value: "7.5" } });
    resolve({ default_task_budget_usd: 10 });
    await waitFor(() => expect(getTaskDefaults).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(input.value).toBe("7.5");
  });
});
