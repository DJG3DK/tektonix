import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";
import { session, task, user } from "../test/fixtures";

// Mocked so the sidebar's tests do not make a real request for the GitHub
// badge. Its own behaviour -- which states count, what a failure shows -- is
// covered in src/useGitHubInboxCount.test.tsx; here it is just a number.
const inboxCount = vi.fn(() => null as number | null);
vi.mock("../useGitHubInboxCount", () => ({ useGitHubInboxCount: () => inboxCount() }));

function renderSidebar(over: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const props = {
    tasks: [],
    selectedTaskId: null,
    planningSessions: [],
    selectedPlanningSessionId: null,
    view: "task" as const,
    user: user(),
    onSelect: vi.fn(),
    onNewTask: vi.fn(),
    onAnalytics: vi.fn(),
    onModels: vi.fn(),
    onUsers: vi.fn(),
    onSettings: vi.fn(),
    onSelectPlanning: vi.fn(),
    onNewPlanning: vi.fn(),
    onDeleted: vi.fn(),
    onPlanningDeleted: vi.fn(),
    onLogout: vi.fn(),
    ...over,
  };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return { ...render(<Sidebar {...(props as any)} />), props };
}

/** The clickable header for a category, e.g. "Bug fix". Matched on its label
 *  span rather than textContent, which also carries the chevron and count. */
function categoryHead(label: string): HTMLElement {
  const head = [...document.querySelectorAll<HTMLElement>(".sidebar-category-head")].find(
    (el) => el.querySelector(".sidebar-category-label")?.textContent === label,
  );
  if (!head) throw new Error(`no category header for "${label}"`);
  return head;
}

/** The task search box only renders past 5 tasks, so tests that use it need
 *  a list that clears the threshold. */
function manyTasks(over: Parameters<typeof task>[0] = {}) {
  return Array.from({ length: 6 }, (_, i) => task({ goal: `filler ${i}`, category: "other", ...over }));
}

const isExpanded = (label: string) =>
  categoryHead(label).querySelector(".sidebar-chevron")?.classList.contains("open") ?? false;

describe("Sidebar — category expansion", () => {
  // Regression: expansion used to be `manuallyExpanded.has(cat) || searching
  // || selectedCategory === cat`. That can only ADD expansion, so the last
  // clause could never be switched off and clicking to collapse did nothing.
  it("starts collapsed when nothing is selected", () => {
    renderSidebar({ tasks: [task({ category: "bug-fix" })] });
    expect(isExpanded("Bug fix")).toBe(false);
  });

  it("opens and closes on click", async () => {
    renderSidebar({ tasks: [task({ category: "bug-fix" })] });
    await userEvent.click(categoryHead("Bug fix"));
    expect(isExpanded("Bug fix")).toBe(true);
    await userEvent.click(categoryHead("Bug fix"));
    expect(isExpanded("Bug fix")).toBe(false);
  });

  it("auto-opens the selected task's category as a default", () => {
    const t = task({ category: "feature", status: "done" });
    renderSidebar({ tasks: [t], selectedTaskId: t.task_id, view: "task" });
    expect(isExpanded("Feature")).toBe(true);
  });

  it("lets the user collapse the selected task's category — the reported bug", async () => {
    const t = task({ category: "feature", status: "done" });
    renderSidebar({ tasks: [t], selectedTaskId: t.task_id, view: "task" });
    expect(isExpanded("Feature")).toBe(true);

    await userEvent.click(categoryHead("Feature"));
    expect(isExpanded("Feature")).toBe(false); // previously stayed open forever
  });

  it("does not open any category on behalf of a RUNNING task", () => {
    // A running task is filtered out of the category lists and shown in the
    // Running group instead, so opening "its" category revealed a list it is
    // not a member of.
    const running = task({ category: "bug-fix", status: "running" });
    const other = task({ category: "bug-fix", status: "done" });
    renderSidebar({ tasks: [running, other], selectedTaskId: running.task_id, view: "task" });
    expect(isExpanded("Bug fix")).toBe(false);
  });

  it("keeps a running task visible in its own group regardless", () => {
    const running = task({ goal: "verify the entry gate", status: "running" });
    renderSidebar({ tasks: [running] });
    expect(screen.getByText("verify the entry gate")).toBeInTheDocument();
  });

  it("shows a running task in the Running group only, never also in a category", () => {
    const running = task({ goal: "only once please", category: "feature", status: "running" });
    renderSidebar({ tasks: [running] });
    // Listed exactly once, and its category does not even appear: running
    // tasks are filtered out of byCategory, leaving "Feature" empty, and an
    // empty category collapses away rather than rendering a zero row.
    expect(screen.getAllByText("only once please")).toHaveLength(1);
    expect(() => categoryHead("Feature")).toThrow();
  });

  it("still lists a running task inside a category once it finishes", async () => {
    const done = task({ goal: "now it is done", category: "feature", status: "done" });
    renderSidebar({ tasks: [done] });
    await userEvent.click(categoryHead("Feature"));
    expect(screen.getByText("now it is done")).toBeInTheDocument();
  });
});

describe("Sidebar — search", () => {
  it("only offers a search box once the list is long enough to need one", () => {
    renderSidebar({ tasks: [task()] });
    expect(screen.queryByPlaceholderText(/search tasks/i)).not.toBeInTheDocument();
    cleanup();
    renderSidebar({ tasks: manyTasks() });
    expect(screen.getByPlaceholderText(/search tasks/i)).toBeInTheDocument();
  });

  it("expands categories so matches are visible", async () => {
    renderSidebar({ tasks: [...manyTasks(), task({ goal: "fix the parser", category: "bug-fix" })] });
    expect(isExpanded("Bug fix")).toBe(false);

    await userEvent.type(screen.getByPlaceholderText(/search tasks/i), "parser");
    expect(isExpanded("Bug fix")).toBe(true);
    expect(screen.getByText("fix the parser")).toBeInTheDocument();
  });

  it("search overrides a user collapse — matches must not be hidden", async () => {
    renderSidebar({ tasks: [...manyTasks(), task({ goal: "fix the parser", category: "bug-fix" })] });
    await userEvent.click(categoryHead("Bug fix"));
    await userEvent.click(categoryHead("Bug fix")); // explicitly collapsed
    expect(isExpanded("Bug fix")).toBe(false);

    await userEvent.type(screen.getByPlaceholderText(/search tasks/i), "parser");
    expect(isExpanded("Bug fix")).toBe(true);
  });

  it("filters out non-matching tasks", async () => {
    renderSidebar({
      tasks: [
        ...manyTasks(),
        task({ goal: "fix the parser", category: "bug-fix" }),
        task({ goal: "add a chart", category: "feature" }),
      ],
    });
    await userEvent.type(screen.getByPlaceholderText(/search tasks/i), "parser");
    expect(screen.getByText("fix the parser")).toBeInTheDocument();
    expect(screen.queryByText("add a chart")).not.toBeInTheDocument();
  });
});

describe("Sidebar — repo filter", () => {
  it("applies to building and planning together", async () => {
    renderSidebar({
      tasks: [
        ...manyTasks(),
        task({ goal: "bot task", repo: "webapp", category: "feature" }),
        task({ goal: "steals task", repo: "storefront", category: "feature" }),
      ],
      planningSessions: [
        session({ title: "bot plan", repo: "webapp" }),
        session({ title: "steals plan", repo: "storefront" }),
      ],
    });
    await userEvent.click(screen.getByRole("button", { name: "webapp" }));
    await userEvent.type(screen.getByPlaceholderText(/search tasks/i), "task");
    expect(screen.getByText("bot task")).toBeInTheDocument();
    expect(screen.queryByText("steals task")).not.toBeInTheDocument();
  });
});

describe("Sidebar — admin-only navigation", () => {
  it("offers Users and Analytics to an admin", () => {
    renderSidebar({ user: user({ role: "admin" }) });
    expect(screen.getByRole("button", { name: /users/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /analytics/i })).toBeInTheDocument();
  });

  it("hides them from a non-admin", () => {
    renderSidebar({ user: user({ role: "user" }) });
    expect(screen.queryByRole("button", { name: /users/i })).not.toBeInTheDocument();
  });
});

describe("Sidebar — empty state", () => {
  it("renders no category headers when there is nothing to show", () => {
    renderSidebar({ tasks: [] });
    // Empty categories collapse away entirely rather than showing zero rows.
    expect(screen.queryByText("Bug fix")).not.toBeInTheDocument();
  });

  it("keeps a category with items", () => {
    renderSidebar({ tasks: [task({ category: "performance" })] });
    expect(within(categoryHead("Performance")).getByText("Performance")).toBeInTheDocument();
  });
});


describe("GitHub inbox badge", () => {
  function githubButton() {
    return screen.getByRole("button", { name: /GitHub/ });
  }

  it("shows the count on the GitHub button when items are waiting", () => {
    inboxCount.mockReturnValue(3);
    renderSidebar();
    expect(within(githubButton()).getByText("3")).toBeInTheDocument();
  });

  it("shows nothing when the inbox is clear", () => {
    // The button has to look exactly as it did before on a quiet day -- a
    // badge showing "0" is a permanent piece of furniture that says nothing.
    inboxCount.mockReturnValue(0);
    renderSidebar();
    expect(within(githubButton()).queryByText("0")).toBeNull();
  });

  it("shows nothing when the count could not be read", () => {
    inboxCount.mockReturnValue(null);
    renderSidebar();
    expect(githubButton().querySelector(".nav-badge")).toBeNull();
  });

  it("caps a large count rather than stretching the button", () => {
    inboxCount.mockReturnValue(247);
    renderSidebar();
    expect(within(githubButton()).getByText("99+")).toBeInTheDocument();
  });

  it("says what the number means, for a screen reader", () => {
    inboxCount.mockReturnValue(2);
    renderSidebar();
    expect(githubButton().querySelector(".nav-badge")!.getAttribute("aria-label"))
      .toContain("awaiting a decision");
  });
});
