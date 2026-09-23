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

  it("a supervisor heal reads as the system acting, not the agent speaking", () => {
    const entry = {
      node: "supervisor",
      summary: "supervisor: auto-heal #1: review_timeout",
      detail: "",
      timestamp: new Date().toISOString(),
    } as unknown as LogEntry;
    const { container } = render(<ChatMessage entry={entry} />);
    expect(container.querySelector(".chat-system")).not.toBeNull();
    expect(screen.getByText("supervisor: auto-heal #1: review_timeout")).toBeTruthy();
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
    expect(btn).toHaveAttribute("aria-expanded", "true");
    expect(document.querySelector(".chat-tool-result-body")?.textContent).toContain("README.md");
  });
});

describe("every speaker wears the same mark", () => {
  function agentEntry(node: LogEntry["node"]) {
    return { node, step_id: null, summary: "did a thing", detail: "detail",
             cost_usd: 0, timestamp: "2026-09-22T22:00:00Z" };
  }

  it("gives a subagent the mark, not its initials", () => {
    // It used to render a tinted disc with "TE" for test-writer. The name is
    // already on the line beside it, so the disc answered a question the
    // label had already answered -- and made one transcript look like two
    // different products talking.
    const { container } = render(
      <ChatMessage entry={agentEntry("work:test-writer")} prevEntry={undefined} />);
    const avatar = container.querySelector(".chat-avatar")!;
    expect(avatar.querySelector("svg")).not.toBeNull();
    expect(avatar.textContent).toBe("");
  });

  it("gives the coordinator the same one", () => {
    const { container } = render(<ChatMessage entry={agentEntry("work")} prevEntry={undefined} />);
    expect(container.querySelector(".chat-avatar svg")).not.toBeNull();
  });

  it("has no tinted-disc variant left", () => {
    const { container } = render(
      <ChatMessage entry={agentEntry("work:investigator")} prevEntry={undefined} />);
    expect(container.querySelector(".chat-avatar--sub")).toBeNull();
    expect(container.querySelector(".chat-avatar--mark")).not.toBeNull();
  });

  it("still shows only one avatar for a run of messages from one speaker", () => {
    // The avatar is suppressed on a continuation row; making every mark
    // identical must not turn a conversation into a column of logos.
    const prev = agentEntry("work:test-writer");
    const { container } = render(<ChatMessage entry={agentEntry("work:test-writer")} prevEntry={prev} />);
    expect(container.querySelector(".chat-avatar")).toBeNull();
  });
});
