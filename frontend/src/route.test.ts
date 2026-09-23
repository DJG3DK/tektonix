import { describe, expect, it } from "vitest";
import { parseRoute, routePath, type Route } from "./route";

describe("route", () => {
  it("lands on Planning with no path, and on anything it does not know", () => {
    expect(parseRoute("/")).toEqual({ view: "planning" });
    expect(parseRoute("/no/such/thing")).toEqual({ view: "planning" });
  });

  it("reads a task and a planning session from the path", () => {
    expect(parseRoute("/task/abc-123")).toEqual({ view: "task", taskId: "abc-123" });
    expect(parseRoute("/planning/s-9")).toEqual({ view: "planning", sessionId: "s-9" });
  });

  it("names every other view", () => {
    expect(parseRoute("/inbox").view).toBe("github");
    expect(parseRoute("/new").view).toBe("new-task");
    expect(parseRoute("/settings").view).toBe("settings");
    expect(parseRoute("/analytics").view).toBe("analytics");
  });

  it("is the inverse of routePath for every route it produces", () => {
    const routes: Route[] = [
      { view: "planning" }, { view: "planning", sessionId: "s 1/x" }, { view: "task", taskId: "t-1" },
      { view: "github" }, { view: "new-task" }, { view: "settings" }, { view: "analytics" },
      { view: "models" }, { view: "users" },
    ];
    for (const r of routes) expect(parseRoute(routePath(r))).toEqual(r);
  });

  it("a task view with no task is just the root, not /task/undefined", () => {
    expect(routePath({ view: "task" })).toBe("/");
  });
});
