import type { CurrentUser, PlanningSessionMeta, TaskMeta } from "../types";

// Builders rather than fixed objects: a test states only the fields it cares
// about, so adding a required field to a type breaks one place here instead
// of every test file.

let seq = 0;
const nextId = () => `id-${++seq}`;

export function task(over: Partial<TaskMeta> = {}): TaskMeta {
  return {
    task_id: nextId(),
    goal: "do the thing",
    repo: "webapp",
    budget_usd: 5,
    status: "done",
    created_at: 1_700_000_000,
    category: "feature",
    ...over,
  };
}

export function session(over: Partial<PlanningSessionMeta> = {}): PlanningSessionMeta {
  return {
    session_id: nextId(),
    repo: "webapp",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    title: "a plan",
    plan_markdown: "# plan",
    cost_usd: 0,
    ...over,
  } as PlanningSessionMeta;
}

export function user(over: Partial<CurrentUser> = {}): CurrentUser {
  return {
    id: 1,
    email: "operator@example.test",
    role: "admin",
    allowed_repos: null,
    totp_enabled: true,
    must_change_password: false,
    require_totp_setup: false,
    ...over,
  } as CurrentUser;
}

/** A Response stand-in for stubbing global fetch. */
export function response(body: unknown, init: { status?: number } = {}): Response {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}
