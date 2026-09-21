import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ChatMessage } from "./ChatMessage";
import type { LogEntry } from "../types";

describe("ChatMessage", () => {
  it("renders nothing, and does not throw, for an entry with no text", () => {
    // 2026-09-09: a heartbeat wrapped as a log entry reached the page with no
    // summary; classify() called startsWith on undefined and the error
    // boundary replaced the whole planning view.
    const ping = { type: "ping" } as unknown as LogEntry;
    const { container } = render(<ChatMessage entry={ping} />);
    expect(container.textContent).toBe("");
  });

  it("still renders an ordinary agent line", () => {
    const entry = { node: "planner", summary: "Reading the map", detail: "Reading the map", timestamp: new Date().toISOString() } as unknown as LogEntry;
    const { container } = render(<ChatMessage entry={entry} />);
    expect(container.textContent).toContain("Reading the map");
  });

  it("a gate verdict with detail is a button, and Enter opens it", async () => {
    const user = userEvent.setup();
    const entry = {
      node: "verify_and_ship",
      summary: "READY",
      detail: "checks passed on the branch",
      timestamp: new Date().toISOString(),
    } as unknown as LogEntry;
    render(<ChatMessage entry={entry} />);
    const btn = screen.getByRole("button", { name: "Show gate detail" });
    expect(btn).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("checks passed on the branch")).toBeNull();
    await user.tab();
    expect(btn).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByText("checks passed on the branch")).toBeTruthy();
    expect(btn).toHaveAttribute("aria-expanded", "true");
    expect(btn).toHaveAccessibleName("Hide gate detail");
  });

  it("a gate verdict with no detail is not a button", () => {
    const entry = {
      node: "verify_and_ship",
      summary: "READY",
      timestamp: new Date().toISOString(),
    } as unknown as LogEntry;
    render(<ChatMessage entry={entry} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("READY")).toBeTruthy();
  });

  it("tool output opens on Space", async () => {
    const user = userEvent.setup();
    const entry = {
      node: "work:coder",
      summary: "tool result: ls",
      detail: "exit_code=0\nREADME.md",
      timestamp: new Date().toISOString(),
    } as unknown as LogEntry;
    render(<ChatMessage entry={entry} />);
    const btn = screen.getByRole("button", { name: "Show tool output" });
    btn.focus();
    await user.keyboard(" ");
    expect(screen.getByText(/README.md/)).toBeTruthy();
    expect(btn).toHaveAttribute("aria-expanded", "true");
  });
});
