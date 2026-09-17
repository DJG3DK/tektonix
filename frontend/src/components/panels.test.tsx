import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalCard } from "./ApprovalCard";
import { AutoGrowTextarea } from "./AutoGrowTextarea";
import { NewTaskPanel } from "./NewTaskPanel";
import type { PendingApproval } from "../types";

const approveTask = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, approveTask: (...a: unknown[]) => approveTask(...a) };
});

const approval = (over: Partial<PendingApproval> = {}): PendingApproval => ({
  action_requests: [{ name: "bash", args: { command: "rm -rf build" }, description: "run a command" }],
  review_configs: [{ action_name: "bash", allowed_decisions: ["approve", "reject"] }],
  ...over,
});

const question = (): PendingApproval => ({
  action_requests: [{ name: "ask_user", args: { question: "Which database should I target?" } }],
  review_configs: [{ action_name: "ask_user", allowed_decisions: ["respond"] }],
});

beforeEach(() => {
  approveTask.mockReset();
});

describe("ApprovalCard — sensitive actions", () => {
  it("prefers the human-written description when there is one", () => {
    render(<ApprovalCard taskId="t1" pendingApproval={approval()} onDecided={vi.fn()} />);
    expect(screen.getByText("run a command")).toBeInTheDocument();
    expect(screen.getByText("bash")).toBeInTheDocument();
  });

  it("falls back to the raw args so the operator can always see the command", () => {
    // Without this branch an approval with no description would ask the
    // operator to authorise something it never showed them.
    const noDescription = approval({
      action_requests: [{ name: "bash", args: { command: "rm -rf build" } }],
    });
    render(<ApprovalCard taskId="t1" pendingApproval={noDescription} onDecided={vi.fn()} />);
    expect(screen.getByText(/rm -rf build/)).toBeInTheDocument();
  });

  it("approves with no message", async () => {
    approveTask.mockResolvedValue(undefined);
    const onDecided = vi.fn();
    render(<ApprovalCard taskId="t1" pendingApproval={approval()} onDecided={onDecided} />);

    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(approveTask).toHaveBeenCalledWith("t1", "approve", undefined);
    expect(onDecided).toHaveBeenCalled();
  });

  it("keeps the card up when the decision fails to record", async () => {
    // Reporting success on a failed approval would leave the operator
    // believing they had unblocked a task that is still waiting.
    approveTask.mockRejectedValue(new Error("backend down"));
    const onDecided = vi.fn();
    render(<ApprovalCard taskId="t1" pendingApproval={approval()} onDecided={onDecided} />);

    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    expect(await screen.findByText(/backend down/i)).toBeInTheDocument();
    expect(onDecided).not.toHaveBeenCalled();
  });
});

describe("ApprovalCard — ask_user is a question, not an approval", () => {
  it("renders the question and an answer box rather than approve/reject", () => {
    render(<ApprovalCard taskId="t1" pendingApproval={question()} onDecided={vi.fn()} />);
    expect(screen.getByText(/which database should i target/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/your answer to the agent/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^approve$/i })).not.toBeInTheDocument();
  });

  it("sends the typed answer as the respond decision", async () => {
    approveTask.mockResolvedValue(undefined);
    render(<ApprovalCard taskId="t1" pendingApproval={question()} onDecided={vi.fn()} />);

    await userEvent.type(screen.getByLabelText(/your answer to the agent/i), "the staging one");
    const send = screen.getAllByRole("button").find((b) => !b.hasAttribute("disabled"))!;
    await userEvent.click(send);
    expect(approveTask).toHaveBeenCalledWith("t1", "respond", "the staging one");
  });

  it("will not send an empty answer", () => {
    render(<ApprovalCard taskId="t1" pendingApproval={question()} onDecided={vi.fn()} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons.every((b) => b.hasAttribute("disabled"))).toBe(true);
  });

  it("treats a mixed batch as an approval, not a question", () => {
    // isQuestion requires EVERY request to be ask_user; one sensitive action
    // in the batch means the whole thing needs approving.
    const mixed = approval({
      action_requests: [
        { name: "ask_user", args: { question: "which?" } },
        { name: "bash", args: { command: "sudo reboot" } },
      ],
    });
    render(<ApprovalCard taskId="t1" pendingApproval={mixed} onDecided={vi.fn()} />);
    expect(screen.queryByLabelText(/your answer to the agent/i)).not.toBeInTheDocument();
  });
});

describe("NewTaskPanel", () => {
  const props = () => ({
    repos: ["webapp", "storefront"],
    onSubmit: vi.fn(),
    submitting: false,
    error: null,
    onClearError: vi.fn(),
  });

  it("lists every repo the user may target", () => {
    render(<NewTaskPanel {...props()} />);
    expect(screen.getByRole("option", { name: "webapp" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "storefront" })).toBeInTheDocument();
  });

  it("submits the goal, repo and budget together", async () => {
    const p = props();
    render(<NewTaskPanel {...p} />);
    await userEvent.type(screen.getByRole("textbox"), "fix the parser");
    await userEvent.click(screen.getByRole("button", { name: /start|create|submit/i }));

    expect(p.onSubmit).toHaveBeenCalledWith(
      "fix the parser",
      expect.any(String),
      expect.any(Number),
      expect.any(Array),
      "auto",  // model route: the server decides unless the operator picks
    );
  });

  it("refuses an empty goal", async () => {
    const p = props();
    render(<NewTaskPanel {...p} />);
    const submit = screen.getByRole("button", { name: /start|create|submit/i });
    expect(submit).toBeDisabled();
    expect(p.onSubmit).not.toHaveBeenCalled();
  });

  it("surfaces a submission error", () => {
    render(<NewTaskPanel {...props()} error="budget too large" />);
    expect(screen.getByText(/budget too large/i)).toBeInTheDocument();
  });

  it("blocks a second submit while one is in flight", () => {
    render(<NewTaskPanel {...props()} submitting />);
    expect(screen.getByRole("button", { name: /start|create|submit|ing/i })).toBeDisabled();
  });
});

describe("AutoGrowTextarea", () => {
  it("reports typing through onChange", async () => {
    const onChange = vi.fn();
    render(<AutoGrowTextarea value="" onChange={onChange} ariaLabel="msg" />);
    await userEvent.type(screen.getByLabelText("msg"), "hi");
    expect(onChange).toHaveBeenCalled();
  });

  it("submits on Enter but not on Shift+Enter", async () => {
    const onSubmit = vi.fn();
    render(<AutoGrowTextarea value="x" onChange={vi.fn()} onSubmit={onSubmit} ariaLabel="msg" />);
    const box = screen.getByLabelText("msg");

    await userEvent.type(box, "{Shift>}{Enter}{/Shift}");
    expect(onSubmit).not.toHaveBeenCalled(); // newline, not send

    await userEvent.type(box, "{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("does not submit while disabled", async () => {
    const onSubmit = vi.fn();
    render(<AutoGrowTextarea value="x" onChange={vi.fn()} onSubmit={onSubmit} disabled ariaLabel="msg" />);
    await userEvent.type(screen.getByLabelText("msg"), "{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

/* The Analytics page renders one group per pinned role.
 *
 * Reported live on 2026-09-12, within an hour of the metrics rewrite: every
 * role appeared twice. The page keys its curated labels on short role names
 * ("coder"), the new backend returned the raw alias ("agent-coder"), so each
 * role rendered once as an empty curated row and again as an unknown role
 * carrying the real numbers. */
describe("analytics model usage", () => {
  it("renders one group per role when the backend uses short names", async () => {
    const { rolesToRender } = await import("./AnalyticsView");
    const models = [
      { role: "coder" }, { role: "test-writer" }, { role: "planning-chat-hard" },
    ];
    const roles = rolesToRender(models);
    expect(roles.filter((r) => r === "coder")).toHaveLength(1);
    expect(roles).not.toContain("agent-coder");
    // a role outside the core eight still appears, appended
    expect(roles).toContain("planning-chat-hard");
  });

  it("a raw alias would duplicate, which is what the backend must not send", async () => {
    const { rolesToRender } = await import("./AnalyticsView");
    const roles = rolesToRender([{ role: "agent-coder" }]);
    expect(roles).toContain("coder");        // the empty curated row
    expect(roles).toContain("agent-coder");  // and the raw one: two groups
  });
});
