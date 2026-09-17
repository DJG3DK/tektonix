import type { ComponentProps } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CreateProjectResult, NewProjectDecisionResult } from "../api";
import type { PlanningSessionMeta } from "../types";
import { PlanningView } from "./PlanningView";

// The planner's "New project…" door. It shares the Repo dropdown with the
// ordinary start-a-session flow, so most of what matters here is that the
// two never bleed into each other: the sentinel must never reach
// createPlanningSession, and a failed create must leave the form standing.

const createProject = vi.fn();
const createPlanningSession = vi.fn();
const decideNewProject = vi.fn();
const getPlanningSession = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    createProject: (...a: unknown[]) => createProject(...a),
    createPlanningSession: (...a: unknown[]) => createPlanningSession(...a),
    decideNewProject: (...a: unknown[]) => decideNewProject(...a),
    getPlanningSession: (...a: unknown[]) => getPlanningSession(...a),
  };
});

const okResult = (name: string): CreateProjectResult => ({
  ok: true,
  name,
  live: `/srv/projects/${name}`,
  steps: [
    { step: "init", ok: true, detail: "git init + initial commit" },
    { step: "config", ok: true, detail: "projects.json updated" },
  ],
  github: null,
});

function renderPanel(over: Partial<ComponentProps<typeof PlanningView>> = {}) {
  const props = {
    repos: ["3d-bot"],
    isAdmin: true,
    githubReady: true,
    onProjectCreated: vi.fn(async () => {}),
    session: null,
    onBuildNow: vi.fn(),
    onSessionCreated: vi.fn(),
    onSessionUpdated: vi.fn(),
    ...over,
  };
  render(<PlanningView {...props} />);
  return props;
}

const repoSelect = () => screen.getByLabelText(/^repo$/i);
const startButton = () => screen.getByRole("button", { name: /start planning|create & start|creating project/i });

async function openDoor() {
  await userEvent.selectOptions(repoSelect(), "__new__");
  return screen.getByLabelText(/project name/i);
}

beforeEach(() => {
  createProject.mockReset();
  createPlanningSession.mockReset();
  decideNewProject.mockReset();
  getPlanningSession.mockReset();
});

describe("PlanningView — the New project door", () => {
  it("offers the door to admins only", () => {
    // The endpoint is admin-only; a restricted account gets no control that
    // would just 403 them.
    renderPanel({ isAdmin: false });
    expect(screen.queryByRole("option", { name: /new project/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/project name/i)).not.toBeInTheDocument();
  });

  it("renders the option for an admin and keeps the form hidden until it is picked", () => {
    renderPanel();
    expect(screen.getByRole("option", { name: /new project/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/project name/i)).not.toBeInTheDocument();
    // Start is live for the ordinary flow: the first real repo is selected.
    expect(startButton()).toBeEnabled();
  });

  it("reveals the name input and holds Start until the name is valid", async () => {
    renderPanel();
    const name = await openDoor();
    expect(startButton()).toBeDisabled();

    // A leading dot is the server's rule too; better to say so here than as
    // a 400 after a wait.
    await userEvent.type(name, ".bad");
    expect(screen.getByText(/1–64 characters/)).toBeInTheDocument();
    expect(startButton()).toBeDisabled();

    // A leading "_" or "-" is refused by provisioning.PROJECT_NAME_RE too.
    // This validator once allowed both, so the form said the name was fine
    // and the server answered 400 on submit -- the one failure mode a
    // client-side validator exists to prevent.
    for (const rejected of ["_app", "-app"]) {
      await userEvent.clear(name);
      await userEvent.type(name, rejected);
      expect(screen.getByText(/1–64 characters/)).toBeInTheDocument();
      expect(startButton()).toBeDisabled();
    }

    await userEvent.clear(name);
    await userEvent.type(name, "my-app");
    expect(screen.queryByText(/1–64 characters/)).not.toBeInTheDocument();
    expect(startButton()).toBeEnabled();
  });

  it("offers the GitHub checkbox only when a token is configured", async () => {
    renderPanel({ githubReady: false });
    await openDoor();
    expect(screen.queryByRole("checkbox", { name: /github/i })).not.toBeInTheDocument();
  });

  it("checks the GitHub box by default when a token is configured", async () => {
    renderPanel({ githubReady: true });
    await openDoor();
    expect(screen.getByRole("checkbox", { name: /private github repo/i })).toBeChecked();
  });

  it("creates the project, refreshes repos, then opens a session on the new name, in that order", async () => {
    createProject.mockResolvedValue(okResult("my-app"));
    createPlanningSession.mockResolvedValue({ session_id: "s1", repo: "my-app" });
    const { onProjectCreated, onSessionCreated } = renderPanel();

    const name = await openDoor();
    await userEvent.type(name, "my-app");
    await userEvent.type(screen.getByLabelText(/description/i), "a thing");
    await userEvent.click(startButton());

    await waitFor(() => expect(onSessionCreated).toHaveBeenCalled());
    expect(createProject).toHaveBeenCalledWith({ name: "my-app", description: "a thing", github: true });
    expect(onProjectCreated).toHaveBeenCalledWith("my-app");
    expect(createPlanningSession).toHaveBeenCalledWith("my-app", "auto");
    expect(onSessionCreated).toHaveBeenCalledWith(expect.objectContaining({ session_id: "s1", repo: "my-app" }));

    // The sidebar must know the repo before the session view names it, so
    // the refresh sits strictly between create and the session call.
    const order = [
      createProject.mock.invocationCallOrder[0],
      (onProjectCreated as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0],
      createPlanningSession.mock.invocationCallOrder[0],
      (onSessionCreated as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0],
    ];
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("sends github: false when the box is unticked", async () => {
    createProject.mockResolvedValue(okResult("my-app"));
    createPlanningSession.mockResolvedValue({ session_id: "s1", repo: "my-app" });
    const { onSessionCreated } = renderPanel();
    await userEvent.type(await openDoor(), "my-app");
    await userEvent.click(screen.getByRole("checkbox", { name: /private github repo/i }));
    await userEvent.click(startButton());
    await waitFor(() => expect(onSessionCreated).toHaveBeenCalled());
    expect(createProject).toHaveBeenCalledWith(expect.objectContaining({ github: false }));
  });

  it("shows a rejected create in the error chip and never opens a session", async () => {
    // Used to be try/finally with no catch: an unhandled rejection and a
    // button that quietly went back to "Start".
    createProject.mockRejectedValue(new Error("a project named my-app already exists"));
    const { onProjectCreated, onSessionCreated } = renderPanel();
    await userEvent.type(await openDoor(), "my-app");
    await userEvent.click(startButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(/already exists/);
    expect(onProjectCreated).not.toHaveBeenCalled();
    expect(createPlanningSession).not.toHaveBeenCalled();
    expect(onSessionCreated).not.toHaveBeenCalled();
    // The form is still there with what was typed, so the operator can fix
    // the cause and retry.
    expect(screen.getByLabelText(/project name/i)).toHaveValue("my-app");
    expect(startButton()).toBeEnabled();
  });

  it("renders the step list on an ok:false result and keeps the form", async () => {
    createProject.mockResolvedValue({
      ok: false,
      name: "my-app",
      live: "/srv/projects/my-app",
      steps: [
        { step: "init", ok: true, detail: "git init + initial commit" },
        { step: "github", ok: false, detail: "GitHub said 422: name already exists on this account" },
      ],
      github: null,
      message: "Project creation failed at github.",
    } satisfies CreateProjectResult);
    const { onProjectCreated, onSessionCreated } = renderPanel();
    await userEvent.type(await openDoor(), "my-app");
    await userEvent.click(startButton());

    expect(await screen.findByText(/name already exists on this account/)).toBeInTheDocument();
    expect(screen.getByText(/creation failed at github/i)).toBeInTheDocument();
    expect(onProjectCreated).not.toHaveBeenCalled();
    expect(createPlanningSession).not.toHaveBeenCalled();
    expect(onSessionCreated).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/project name/i)).toHaveValue("my-app");
  });

  it("shows the working label while the create is in flight", async () => {
    let settle: (v: CreateProjectResult) => void = () => {};
    createProject.mockReturnValue(new Promise<CreateProjectResult>((r) => (settle = r)));
    createPlanningSession.mockResolvedValue({ session_id: "s1", repo: "my-app" });
    const { onSessionCreated } = renderPanel();
    await userEvent.type(await openDoor(), "my-app");
    await userEvent.click(startButton());
    expect(screen.getByRole("button", { name: /creating project/i })).toBeDisabled();
    settle(okResult("my-app"));
    await waitFor(() => expect(onSessionCreated).toHaveBeenCalled());
  });
});

describe("PlanningView — the ordinary start flow next to the door", () => {
  it("never hands the sentinel to createPlanningSession", async () => {
    createPlanningSession.mockResolvedValue({ session_id: "s2", repo: "3d-bot" });
    const { onSessionCreated } = renderPanel();
    await userEvent.click(startButton());
    await waitFor(() => expect(onSessionCreated).toHaveBeenCalled());
    expect(createPlanningSession).toHaveBeenCalledWith("3d-bot", "auto");
    expect(createProject).not.toHaveBeenCalled();
  });

  it("lands an admin with no projects straight on the form", () => {
    // With an empty repo list the browser would show "New project…" as the
    // selected entry anyway; matching the form to that avoids a dropdown
    // that says one thing and a panel that shows another.
    renderPanel({ repos: [] });
    expect(screen.getByLabelText(/project name/i)).toBeInTheDocument();
    expect(startButton()).toBeDisabled();
  });

  it("surfaces a failed session start rather than swallowing it", async () => {
    createPlanningSession.mockRejectedValue(new Error("createPlanningSession failed: 503"));
    const { onSessionCreated } = renderPanel({ isAdmin: false });
    await userEvent.click(startButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/503/);
    expect(onSessionCreated).not.toHaveBeenCalled();
  });
});

// The mid-conversation door. The agent's create_project tool only records a
// proposal on the session; this card is where an admin turns it into a real
// project, through the same server path as the start form's door, after
// which the session is re-homed under the new repo.

const PROPOSAL = { name: "my-app", description: "a store front", github: true };

const openSession = (over: Partial<PlanningSessionMeta> = {}): PlanningSessionMeta => ({
  session_id: "s1",
  repo: "3d-bot",
  created_at: 1,
  updated_at: 1,
  title: "a new thing",
  plan_markdown: null,
  cost_usd: 0,
  new_project: PROPOSAL,
  ...over,
});

const movedSession = (): PlanningSessionMeta => openSession({ repo: "my-app", new_project: null });

function renderSession(over: Partial<ComponentProps<typeof PlanningView>> = {}) {
  // The hook hydrates from the server on mount; hand it a meta without the
  // proposal so the card provably renders from the session prop.
  getPlanningSession.mockResolvedValue({
    meta: openSession({ new_project: null }), log: [], running: false,
  });
  return renderPanel({ session: openSession(), ...over });
}

const confirmButton = () => screen.getByRole("button", { name: /^confirm$|creating project/i });
const dismissButton = () => screen.getByRole("button", { name: /^dismiss$/i });

describe("PlanningView — the confirm card for a proposed project", () => {
  it("renders the proposal from session.new_project for an admin", () => {
    renderSession();
    const card = screen.getByRole("region", { name: /proposed new project/i });
    expect(card).toHaveTextContent(/the agent proposes a new project/i);
    expect(card).toHaveTextContent("my-app");
    expect(card).toHaveTextContent("a store front");
    expect(screen.getByRole("checkbox", { name: /private github repo/i })).toBeChecked();
    expect(confirmButton()).toBeEnabled();
    expect(dismissButton()).toBeEnabled();
  });

  it("shows no card without a proposal", () => {
    getPlanningSession.mockResolvedValue({ meta: openSession({ new_project: null }), log: [], running: false });
    renderPanel({ session: openSession({ new_project: null }) });
    expect(screen.queryByRole("region", { name: /proposed new project/i })).not.toBeInTheDocument();
  });

  it("tells a non-admin an admin must confirm, with no buttons", () => {
    renderSession({ isAdmin: false });
    expect(screen.getByRole("note")).toHaveTextContent(/an admin must confirm/i);
    expect(screen.queryByRole("button", { name: /^confirm$/i })).not.toBeInTheDocument();
  });

  it("hides the GitHub box when no token is configured", () => {
    renderSession({ githubReady: false });
    expect(screen.queryByRole("checkbox", { name: /github/i })).not.toBeInTheDocument();
  });

  it("confirms: decideNewProject, then onProjectCreated, then onSessionUpdated with the moved session", async () => {
    const result: NewProjectDecisionResult = { project: okResult("my-app"), session: movedSession() };
    decideNewProject.mockResolvedValue(result);
    const { onProjectCreated, onSessionUpdated } = renderSession();

    await userEvent.click(confirmButton());

    await waitFor(() => expect(onSessionUpdated).toHaveBeenCalled());
    expect(decideNewProject).toHaveBeenCalledWith("s1", { decision: "confirm", github: true });
    expect(onProjectCreated).toHaveBeenCalledWith("my-app");
    expect(onSessionUpdated).toHaveBeenCalledWith(expect.objectContaining({ session_id: "s1", repo: "my-app", new_project: null }));

    // App must know the repo before the session view names it.
    const order = [
      decideNewProject.mock.invocationCallOrder[0],
      (onProjectCreated as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0],
      (onSessionUpdated as ReturnType<typeof vi.fn>).mock.invocationCallOrder[0],
    ];
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("sends the box's state, not the proposal's, when the operator unticks it", async () => {
    decideNewProject.mockResolvedValue({ project: okResult("my-app"), session: movedSession() });
    const { onSessionUpdated } = renderSession();
    await userEvent.click(screen.getByRole("checkbox", { name: /private github repo/i }));
    await userEvent.click(confirmButton());
    await waitFor(() => expect(onSessionUpdated).toHaveBeenCalled());
    expect(decideNewProject).toHaveBeenCalledWith("s1", { decision: "confirm", github: false });
  });

  it("shows the working label while the create is in flight", async () => {
    let settle: (v: NewProjectDecisionResult) => void = () => {};
    decideNewProject.mockReturnValue(new Promise<NewProjectDecisionResult>((r) => (settle = r)));
    const { onSessionUpdated } = renderSession();
    await userEvent.click(confirmButton());
    expect(screen.getByRole("button", { name: /creating project/i })).toBeDisabled();
    expect(dismissButton()).toBeDisabled();
    settle({ project: okResult("my-app"), session: movedSession() });
    await waitFor(() => expect(onSessionUpdated).toHaveBeenCalled());
  });

  it("dismisses: decideNewProject with 'dismiss', then the cleared session goes up", async () => {
    decideNewProject.mockResolvedValue({ project: null, session: openSession({ new_project: null }) });
    const { onProjectCreated, onSessionUpdated } = renderSession();
    await userEvent.click(dismissButton());
    await waitFor(() => expect(onSessionUpdated).toHaveBeenCalled());
    expect(decideNewProject).toHaveBeenCalledWith("s1", { decision: "dismiss" });
    expect(onProjectCreated).not.toHaveBeenCalled();
    expect(onSessionUpdated).toHaveBeenCalledWith(expect.objectContaining({ repo: "3d-bot", new_project: null }));
  });

  it("renders the step list on an ok:false create and keeps the card", async () => {
    decideNewProject.mockResolvedValue({
      project: {
        ok: false,
        name: "my-app",
        live: "/srv/projects/my-app",
        steps: [
          { step: "repository", ok: true, detail: "one commit on main" },
          { step: "github", ok: false, detail: "GitHub said 422: name already exists on this account" },
        ],
        github: null,
        message: "Project creation failed at github.",
      } satisfies CreateProjectResult,
      session: openSession(),
    });
    const { onProjectCreated, onSessionUpdated } = renderSession();
    await userEvent.click(confirmButton());

    expect(await screen.findByText(/name already exists on this account/)).toBeInTheDocument();
    expect(screen.getByText(/creation failed at github/i)).toBeInTheDocument();
    expect(onProjectCreated).not.toHaveBeenCalled();
    expect(onSessionUpdated).not.toHaveBeenCalled();
    // still here, still answerable
    expect(screen.getByRole("region", { name: /proposed new project/i })).toBeInTheDocument();
    expect(confirmButton()).toBeEnabled();
  });

  it("surfaces a rejected confirm in the error chip and keeps the card", async () => {
    decideNewProject.mockRejectedValue(new Error("planning session is processing a message"));
    const { onSessionUpdated } = renderSession();
    await userEvent.click(confirmButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/processing a message/);
    expect(onSessionUpdated).not.toHaveBeenCalled();
    expect(confirmButton()).toBeEnabled();
  });
});
