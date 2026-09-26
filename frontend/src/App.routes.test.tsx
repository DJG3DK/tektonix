import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { session, task, user } from "./test/fixtures";

// Deep links (2026-09-23 review, finding 11.1): the URL opens the view it
// names, navigating updates the URL, and back/forward follow it.

const getMe = vi.fn();
const listTasks = vi.fn();
const listPlanningSessions = vi.fn();
const getPlanningSession = vi.fn(async (id: string) => ({
  meta: session({ session_id: id }), log: [], running: false,
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getMe: () => getMe(),
    listTasks: () => listTasks(),
    listPlanningSessions: () => listPlanningSessions(),
    listRepos: async () => ["webapp"],
    getGitHubSettings: async () => ({ env_token: false, settings: { tokens: {} } }),
    getPlanningSession: (id: string) => getPlanningSession(id),
    setAuthFailureHandler: () => {},
    // The Benchmarks page's panels each show their own "couldn't load" note;
    // the deep-link test only needs the page to open.
    getEvals: async () => { throw new Error("no evals in this test"); },
    getSwebench: async () => { throw new Error("no SWE-bench in this test"); },
  };
});

import App from "./App";

const T = task({ task_id: "task-aaaa1111", goal: "fix the widget", status: "done" });
const S = session({ session_id: "sess-bbbb2222", title: "a plan for the widget" });

function at(path: string) {
  window.history.replaceState(null, "", path);
}

beforeEach(() => {
  getMe.mockResolvedValue(user());
  listTasks.mockResolvedValue([T]);
  listPlanningSessions.mockResolvedValue([S]);
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "{}" })));
  at("/");
});

describe("deep links", () => {
  it("a task's URL opens that task", async () => {
    at("/task/task-aaaa1111");
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector(".task-view")).not.toBeNull());
    expect(window.location.pathname).toBe("/task/task-aaaa1111");
  });

  it("a planning session's URL opens that session", async () => {
    at("/planning/sess-bbbb2222");
    render(<App />);
    await waitFor(() => expect(getPlanningSession).toHaveBeenCalledWith("sess-bbbb2222"));
    expect(window.location.pathname).toBe("/planning/sess-bbbb2222");
  });

  it("a view's URL opens that view", async () => {
    at("/settings");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
  });

  it("the Benchmarks page has its own URL", async () => {
    at("/benchmarks");
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Benchmarks" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 2, name: "SWE-bench Verified" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/benchmarks");
  });

  it("a task that is not in the list says so rather than showing nothing", async () => {
    at("/task/gone-0000");
    render(<App />);
    expect(await screen.findByRole("status")).toHaveTextContent(/not in your task list/);
  });

  it("navigating updates the URL, and Back returns to where you were", async () => {
    render(<App />);
    const settings = await screen.findAllByRole("button", { name: /^settings$/i });
    await userEvent.click(settings[0]);
    await waitFor(() => expect(window.location.pathname).toBe("/settings"));

    await act(async () => {
      window.history.back();
      await new Promise((r) => setTimeout(r, 20));
    });
    await waitFor(() => expect(window.location.pathname).toBe("/"));
  });

  it("a synonym is replaced, not pushed, so Back is not trapped", async () => {
    window.history.replaceState(null, "", "/settings");
    window.history.pushState(null, "", "/planning");
    const before = window.history.length;
    render(<App />);
    await waitFor(() => expect(window.location.pathname).toBe("/"));
    expect(window.history.length).toBe(before);           // no new entry
    await act(async () => {
      window.history.back();
      await new Promise((r) => setTimeout(r, 20));
    });
    await waitFor(() => expect(window.location.pathname).toBe("/settings"));
  });

  it("an admin URL opened by a restricted account explains the empty pane", async () => {
    getMe.mockResolvedValue(user({ role: "user", allowed_repos: ["webapp"] }));
    at("/analytics");
    render(<App />);
    expect(await screen.findByRole("heading", { name: /admins only/i })).toBeInTheDocument();
  });
});
