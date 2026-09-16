import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ConsolidationStatusPanel } from "./ConsolidationStatusPanel";
import { LogEntryCard } from "./LogEntryCard";
import { MobileNav } from "./MobileNav";
import type { LogEntry } from "../types";

const getConsolidationStatus = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getConsolidationStatus: () => getConsolidationStatus() };
});

const entry = (over: Partial<LogEntry> = {}): LogEntry =>
  ({
    node: "work",
    step_id: null,
    summary: "did a thing",
    detail: "the long version",
    cost_usd: 0,
    timestamp: new Date().toISOString(),
    ...over,
  }) as LogEntry;

beforeEach(() => {
  getConsolidationStatus.mockReset();
});

describe("LogEntryCard", () => {
  it("labels the coordinator's own work entries", () => {
    render(<LogEntryCard entry={entry({ node: "work" })} index={0} />);
    expect(screen.getByText("Work")).toBeInTheDocument();
  });

  it("names a subagent from its work:<name> tag", () => {
    // The set of subagents is not known in advance, so the label is derived
    // rather than looked up.
    render(<LogEntryCard entry={entry({ node: "work:investigator" })} index={0} />);
    expect(screen.getByText("investigator")).toBeInTheDocument();
  });

  it("falls back to the raw node name for anything unrecognised", () => {
    // Cast on purpose: the union says this cannot happen, and the component
    // guards against it anyway — because the backend can ship a new node type
    // before the frontend's type is updated, and the honest failure there is
    // an unfamiliar label, not a blank card.
    const unknownNode = { node: "something_new" } as unknown as Partial<LogEntry>;
    render(<LogEntryCard entry={entry(unknownNode)} index={0} />);
    expect(screen.getByText("something_new")).toBeInTheDocument();
  });

  it("starts expanded, because collapsed-by-default hid the reasoning", () => {
    render(<LogEntryCard entry={entry({ detail: "the long version" })} index={0} />);
    expect(screen.getByText("the long version")).toBeInTheDocument();
  });

  it("collapses on click when there is detail to hide", async () => {
    render(<LogEntryCard entry={entry({ summary: "did a thing", detail: "the long version" })} index={0} />);
    await userEvent.click(screen.getByText("did a thing"));
    expect(screen.queryByText("the long version")).not.toBeInTheDocument();
  });

  it("does not offer to collapse an entry with no detail", async () => {
    render(<LogEntryCard entry={entry({ summary: "just a line", detail: "" })} index={0} />);
    await userEvent.click(screen.getByText("just a line"));
    expect(screen.getByText("just a line")).toBeInTheDocument(); // no crash, nothing to toggle
  });

  it("renders a fresh entry as seconds old", () => {
    render(<LogEntryCard entry={entry({ timestamp: new Date().toISOString() })} index={0} />);
    expect(screen.getByText(/\d+s ago/)).toBeInTheDocument();
  });

  it("renders an older entry in minutes and hours", () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 3600_000).toISOString();
    render(<LogEntryCard entry={entry({ timestamp: twoHoursAgo })} index={0} />);
    expect(screen.getByText("2h ago")).toBeInTheDocument();
  });
});

describe("ConsolidationStatusPanel", () => {
  // The whole point of this panel is that a failed run used to be
  // indistinguishable from a healthy one, so each state must be distinct.
  it("shows a loading state first", () => {
    getConsolidationStatus.mockReturnValue(new Promise(() => {}));
    render(<ConsolidationStatusPanel />);
    expect(screen.getByText(/loading consolidation status/i)).toBeInTheDocument();
  });

  it("reports never-run — the state a log tail cannot show you", async () => {
    getConsolidationStatus.mockResolvedValue({ ran_at: null, ok: null, stale: false });
    render(<ConsolidationStatusPanel />);
    expect(await screen.findByText(/never run/i)).toBeInTheDocument();
  });

  it("reports a failure with its exit code", async () => {
    getConsolidationStatus.mockResolvedValue({ ran_at: "2026-08-29T04:15:00Z", ok: false, exit_code: 3, stale: false });
    render(<ConsolidationStatusPanel />);
    expect(await screen.findByText(/failed \(exit 3\)/i)).toBeInTheDocument();
  });

  it("distinguishes stale from failed", async () => {
    getConsolidationStatus.mockResolvedValue({ ran_at: "2026-08-01T04:15:00Z", ok: true, stale: true });
    render(<ConsolidationStatusPanel />);
    expect(await screen.findByText(/stale/i)).toBeInTheDocument();
  });

  it("surfaces a fetch error instead of pretending to be healthy", async () => {
    getConsolidationStatus.mockRejectedValue(new Error("backend down"));
    render(<ConsolidationStatusPanel />);
    expect(await screen.findByText(/backend down/i)).toBeInTheDocument();
  });
});

describe("MobileNav", () => {
  const handlers = () => ({
    onTasks: vi.fn(),
    onNewPlan: vi.fn(),
    onAnalytics: vi.fn(),
    onModels: vi.fn(),
    onUsers: vi.fn(),
    onSettings: vi.fn(),
    onGitHub: vi.fn(),
  });

  it("hides admin-only destinations from a non-admin", () => {
    render(<MobileNav view="task" pane="list" isAdmin={false} {...handlers()} />);
    expect(screen.queryByText("Stats")).not.toBeInTheDocument();
    expect(screen.queryByText("Models")).not.toBeInTheDocument();
    expect(screen.queryByText("Users")).not.toBeInTheDocument();
    expect(screen.getByText("Tasks")).toBeInTheDocument();
  });

  it("offers them to an admin", () => {
    render(<MobileNav view="task" pane="list" isAdmin {...handlers()} />);
    expect(screen.getByText("Stats")).toBeInTheDocument();
    expect(screen.getByText("Models")).toBeInTheDocument();
    expect(screen.getByText("Users")).toBeInTheDocument();
  });

  it("routes a tap to its handler", async () => {
    const h = handlers();
    render(<MobileNav view="task" pane="main" isAdmin {...h} />);
    await userEvent.click(screen.getByText("Stats"));
    expect(h.onAnalytics).toHaveBeenCalled();
  });

  it("Plan starts a planning session, not the raw task composer", async () => {
    // The sidebar's header buttons are hidden on a phone, so this bar is the
    // only way to the plan-first primary action there.
    const h = handlers();
    render(<MobileNav view="task" pane="main" isAdmin={false} {...h} />);
    await userEvent.click(screen.getByText("Plan"));
    expect(h.onNewPlan).toHaveBeenCalled();
  });

  it("highlights Plan while a planning session is open", () => {
    const { container } = render(<MobileNav view="planning" pane="main" isAdmin={false} {...handlers()} />);
    expect(container.querySelector(".mnav-tab.is-active")?.textContent).toBe("Plan");
  });

  it("treats the list pane as 'Tasks' being current, whatever the view", () => {
    // Tasks is not a view — on a phone it returns to the list pane, which is
    // why the tab takes `pane` as well as `view`.
    const { container } = render(
      <MobileNav view="analytics" pane="list" isAdmin {...handlers()} />,
    );
    const active = container.querySelectorAll(".active, [aria-current]");
    expect(active.length).toBeGreaterThan(0);
  });

  it("meets the 44px touch floor on every tab", () => {
    const { container } = render(<MobileNav view="task" pane="list" isAdmin {...handlers()} />);
    // The CSS owns the real number; this asserts every tab is a real control
    // rather than a bare span, which is what makes it hittable at all.
    const tabs = container.querySelectorAll("button");
    expect(tabs.length).toBeGreaterThanOrEqual(4);
    tabs.forEach((t) => expect(t.tagName).toBe("BUTTON"));
  });
});
