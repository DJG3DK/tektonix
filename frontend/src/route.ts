/* The dashboard's URL, both ways: which view a path means, and which path a
 * view is at.
 *
 * Views used to live only in React state, so a refresh landed on Planning
 * whatever you were watching, a task could not be bookmarked or sent to
 * anyone, and the PWA always relaunched onto an empty composer (2026-09-23
 * review, finding 11.1). No router dependency: six paths and the History API
 * are the whole requirement.
 *
 * Every route has ONE canonical path (routePath). A few other spellings are
 * accepted on the way in -- `/planning` for the Planning landing at `/`, and
 * any unknown path -- and App replaces them with the canonical one rather
 * than pushing, so they never become history entries of their own.
 *
 * Paths are relative to the deploy `base` (vite.config.ts), read through
 * import.meta.env.BASE_URL the same way api.ts builds request paths, so a
 * subpath deploy keeps working. The server's SPA fallback already answers any
 * unknown path with index.html, so no server change is needed.
 */

export type View = "new-task" | "task" | "analytics" | "models" | "planning" | "users" | "settings" | "github";

export interface Route {
  view: View;
  taskId?: string;
  sessionId?: string;
}

const SIMPLE: Record<string, View> = {
  new: "new-task",
  inbox: "github",
  settings: "settings",
  analytics: "analytics",
  models: "models",
  users: "users",
  planning: "planning",
};

const PATH_OF: Partial<Record<View, string>> = {
  "new-task": "new",
  github: "inbox",
  settings: "settings",
  analytics: "analytics",
  models: "models",
  users: "users",
};

function base(): string {
  const b = import.meta.env.BASE_URL || "/";
  return b.endsWith("/") ? b : `${b}/`;
}

/** Which view a path means. Anything unrecognised is Planning -- the
 *  plan-first landing (2026-08-28) -- rather than an error page. */
export function parseRoute(pathname: string): Route {
  const b = base();
  const rel = pathname.startsWith(b) ? pathname.slice(b.length) : pathname.replace(/^\//, "");
  const [head, id] = rel.split("/").filter(Boolean).map(decodeURIComponent);
  if (head === "task" && id) return { view: "task", taskId: id };
  if (head === "planning" && id) return { view: "planning", sessionId: id };
  return { view: (head && SIMPLE[head]) || "planning" };
}

/** Where a view lives. The inverse of parseRoute for every route it returns. */
export function routePath(route: Route): string {
  const b = base();
  if (route.view === "task" && route.taskId) return `${b}task/${encodeURIComponent(route.taskId)}`;
  if (route.view === "planning" && route.sessionId) return `${b}planning/${encodeURIComponent(route.sessionId)}`;
  const p = PATH_OF[route.view];
  return p ? `${b}${p}` : b;
}

/** Two routes that name the same place. */
export function sameRoute(a: Route, b: Route): boolean {
  return a.view === b.view && (a.taskId ?? null) === (b.taskId ?? null)
    && (a.sessionId ?? null) === (b.sessionId ?? null);
}
