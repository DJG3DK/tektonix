import { useEffect, useRef, useState } from "react";
import { useBudgetInput } from "../useDefaultTaskBudget";
import { RouteBadge, RouteSelect, type RouteChoice } from "./RouteSelect";
import { JumpToBottom } from "./JumpToBottom";
import type { AttachmentEntry, CreateProjectResult } from "../api";
import { archivePlanningSession, createPlanningSession, createProject, decideNewProject, uploadFiles } from "../api";
import type { NewProjectProposal, PlanningLogEntry, PlanningSessionMeta } from "../types";
import { usePlanningStream } from "../usePlanningStream";
import { AutoGrowTextarea } from "./AutoGrowTextarea";
import { cleanText, ModelBadge, parseToolCalls, relativeTime, renderWithColorSwatches, TOOL_ICONS } from "./ChatMessage";
import { StepList } from "./StepList";
import "./ChatMessage.css";
import "./TaskView.css";
import "./NewTaskPanel.css";
import "./MessageInput.css";
import { StopButton } from "./StopButton";
import "./PlanningView.css";

/** The Repo dropdown's "New project…" door. A sentinel rather than a second
 *  control because the question an admin is answering is the same one --
 *  "which project am I planning?" -- and the answer "one that does not exist
 *  yet" belongs in that list. It is never handed to createPlanningSession:
 *  the form below turns it into a real project first. */
const NEW_PROJECT = "__new__";

// Mirrors the validator behind POST /api/projects/create so the rule shows up
// as the operator types rather than as a 400 from the server: 1-64 chars of
// [A-Za-z0-9._-], not starting with "." (a dot-directory would be invisible
// in the allowed root and clash with .git-style names).
const PROJECT_NAME_RE = /^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$/;
const PROJECT_NAME_RULE = "1–64 characters: letters, digits, '.', '_' or '-', and not starting with '.'";
function isValidProjectName(name: string): boolean {
  return PROJECT_NAME_RE.test(name);
}

export interface NewProjectForm {
  name: string;
  description: string;
  github: boolean;
}

interface Props {
  repos: string[];
  /** Only admins get the "New project…" door; the endpoint is admin-only and
   *  a restricted account should not see a control that will 403 them. */
  isAdmin: boolean;
  /** Whether any GitHub token is configured (App asks once after sign-in).
   *  Without one, offering "create a private GitHub repo" would just fail
   *  late, so the checkbox is not shown at all. */
  githubReady: boolean;
  /** Awaited between creating the project and opening its planning session,
   *  so App's repo list already contains the new name by the time the
   *  session view renders it. */
  onProjectCreated: (name: string) => Promise<void>;
  // null -- no session picked (or "+ New" in the sidebar) -- show the
  // start-a-session prompt instead of a conversation. Session list/
  // selection lives in App.tsx (mirroring how `selected: TaskMeta` works),
  // not here -- the sidebar needs the full list to render its own
  // Planning section regardless of which one (if any) is currently open.
  session: PlanningSessionMeta | null;
  // Same shape as App.tsx's handleCreate for a normal new task -- "Build
  // Now" hands the saved plan document off to the real build system exactly
  // the way a manually-typed goal would, no dedicated backend endpoint.
  onBuildNow: (goal: string, repo: string, budgetUsd: number, route: RouteChoice) => void;
  /** Why the last Build Now failed, if it did -- a dead button is not an answer. */
  buildError?: string | null;
  onClearBuildError?: () => void;
  onSessionCreated: (session: PlanningSessionMeta) => void;
  /** The session's meta changed server-side under the same id -- today,
   *  a confirmed create_project proposal moved it onto the new repo. App
   *  replaces its copy (sidebar row and selection) so the header, the
   *  composer's uploads and Build Now all follow the repo. */
  onSessionUpdated: (session: PlanningSessionMeta) => void;
}

function PlanningEntry({ entry }: { entry: PlanningLogEntry }) {
  const [open, setOpen] = useState(false);

  if (entry.kind === "user") {
    return (
      <div className="chat-row chat-row--user">
        <div className="chat-bubble chat-bubble--user">
          <div className="chat-text">{renderWithColorSwatches(entry.detail)}</div>
        </div>
        <span className="chat-time">{relativeTime(entry.timestamp)}</span>
      </div>
    );
  }

  if (entry.kind === "tool-result") {
    const body = entry.detail || entry.summary.slice("tool result:".length).trim();
    return (
      <div className="chat-tool-result">
        <div className="chat-tool-result-head" onClick={() => setOpen((o) => !o)}>
          <span className={`chat-chevron ${open ? "open" : ""}`}>▸</span>
          <span>output</span>
          <span className="chat-time">{relativeTime(entry.timestamp)}</span>
          <code className="chat-tool-result-preview">{body.replace(/\s+/g, " ").slice(0, 90)}</code>
        </div>
        {open && <pre className="chat-tool-result-body">{body}</pre>}
      </div>
    );
  }

  // "agent" -- either a tool call or prose, same distinction ChatMessage.tsx
  // makes from the summary prefix.
  if (entry.summary.startsWith("calling: ")) {
    const calls = parseToolCalls(entry.summary);
    return (
      <div className="chat-tools">
        <ModelBadge model={entry.model} />
        <span className="chat-time">{relativeTime(entry.timestamp)}</span>
        {calls.map((c, i) => (
          <span key={i} className="chat-tool-chip" title={c.args}>
            <span className="chat-tool-icon">{TOOL_ICONS[c.name] ?? "⚙"}</span>
            <span className="chat-tool-name">{c.name}</span>
            <span className="chat-tool-args">{c.args.slice(0, 64)}</span>
          </span>
        ))}
      </div>
    );
  }

  const text = cleanText(entry.detail || entry.summary);
  return (
    <div className="chat-row chat-row--agent">
      <div className="chat-avatar">✦</div>
      <div className="chat-agent-col">
        <div className="chat-agent-name">
          Agent
          <ModelBadge model={entry.model} />
          <span className="chat-time">{relativeTime(entry.timestamp)}</span>
        </div>
        <div className="chat-bubble chat-bubble--agent">
          <div className="chat-text">{renderWithColorSwatches(text)}</div>
        </div>
      </div>
    </div>
  );
}

function NewSessionPanel({
  repos, isAdmin, githubReady, onStart, onCreateProject, starting, creating, error, onClearError, failed,
}: {
  repos: string[];
  isAdmin: boolean;
  githubReady: boolean;
  onStart: (repo: string, route: RouteChoice) => void;
  onCreateProject: (form: NewProjectForm, route: RouteChoice) => void;
  starting: boolean;
  creating: boolean;
  /** Why the last Start failed (a rejected create or session call). */
  error: string | null;
  onClearError: () => void;
  /** An `ok: false` create result: the form stays up over its step list. */
  failed: CreateProjectResult | null;
}) {
  // audit H-13: derive, don't mirror -- see NewTaskPanel for the full note.
  const [repo, setRepo] = useState("");
  // An admin with no projects yet lands straight on the form: the dropdown
  // would otherwise be empty with "New project…" as its only entry, shown as
  // selected by the browser while `repo` still said "" and no form appeared.
  const newProject = isAdmin && (repo === NEW_PROJECT || (!repo && repos.length === 0));
  // The sentinel is never the fallback repo, whatever `repo` holds -- a
  // planning session for a project called "__new__" is not a thing.
  const effectiveRepo = (repo && repo !== NEW_PROJECT ? repo : repos[0]) || "";
  const [route, setRoute] = useState<RouteChoice>("auto");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // Private repo on by default: the whole point of the door is a project that
  // exists nowhere else yet, and the box only renders when a token can act.
  const [github, setGithub] = useState(true);
  const nameOk = isValidProjectName(name);
  const busy = starting || creating;
  const canStart = newProject ? nameOk : Boolean(effectiveRepo);

  function submit() {
    if (newProject) {
      onCreateProject({ name, description: description.trim(), github: githubReady && github }, route);
    } else {
      onStart(effectiveRepo, route);
    }
  }

  return (
    <div className="planning-start-panel">
      <div className="planning-start-card">
        <h1>Plan a project</h1>
        <p className="planning-start-sub">
          Research, talk through design/UI/UX direction, and land on a concrete plan before anything gets
          built. It remembers what it learns about this project between sessions, and can look at your other
          projects too. When you're ready, hit "Build Now" to hand the plan straight to a real build task.
        </p>
        <label className="field">
          <span>Repo</span>
          <select value={newProject ? NEW_PROJECT : effectiveRepo} onChange={(e) => setRepo(e.target.value)} disabled={busy}>
            {repos.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
            {isAdmin && <option value={NEW_PROJECT}>New project…</option>}
          </select>
        </label>
        {newProject && (
          <div className="planning-new-project">
            <label className="field">
              <span>Project name</span>
              <input
                type="text"
                value={name}
                autoFocus
                spellCheck={false}
                autoComplete="off"
                placeholder="my-new-app"
                aria-invalid={name.length > 0 && !nameOk}
                onChange={(e) => setName(e.target.value)}
                disabled={busy}
              />
              {name.length > 0 && !nameOk && (
                <span className="planning-field-rule" role="note">{PROJECT_NAME_RULE}</span>
              )}
            </label>
            <label className="field">
              <span>Description (optional)</span>
              <input
                type="text"
                value={description}
                placeholder="One line for the README and the GitHub repo"
                onChange={(e) => setDescription(e.target.value)}
                disabled={busy}
              />
            </label>
            {githubReady && (
              <label className="planning-check">
                <input type="checkbox" checked={github} onChange={(e) => setGithub(e.target.checked)} disabled={busy} />
                <span>Create a private GitHub repo</span>
              </label>
            )}
          </div>
        )}
        <RouteSelect value={route} onChange={setRoute} />
        {failed && (
          <div className="planning-create-failed">
            <StepList steps={failed.steps} />
            {failed.message && <p className="planning-create-message">{failed.message}</p>}
          </div>
        )}
        {error && (
          <div className="planning-build-error" role="alert">
            {error}
            <button type="button" onClick={onClearError} aria-label="Dismiss error">×</button>
          </div>
        )}
        <button className="submit-btn" disabled={!canStart || busy} onClick={submit}>
          {creating ? "Creating project…" : starting ? "Starting..." : newProject ? "Create & Start Planning" : "Start Planning Session"}
        </button>
      </div>
    </div>
  );
}

function BuildNowPanel({ onConfirm, sessionRoute }: { onConfirm: (budgetUsd: number, route: RouteChoice) => void; sessionRoute?: string | null }) {
  const [open, setOpen] = useState(false);
  const [budget, setBudget] = useBudgetInput();  // seeded from Settings → Default task budget
  // A frontend planning session hands its plan to the frontend coder by
  // default; the operator can still pick otherwise here.
  const [route, setRoute] = useState<RouteChoice>(sessionRoute === "frontend" ? "frontend" : "auto");
  if (!open) {
    return (
      <button className="planning-build-btn" onClick={() => setOpen(true)}>
        🚀 Build Now
      </button>
    );
  }
  return (
    <div className="planning-build-confirm">
      <label className="field">
        <span>Budget (USD)</span>
        <input type="number" min={0.1} step={0.1} value={budget} onChange={(e) => setBudget(parseFloat(e.target.value) || 0)} />
      </label>
      <RouteSelect value={route} onChange={setRoute} />
      <button className="planning-build-confirm-btn" onClick={() => onConfirm(budget, route)}>
        Confirm &amp; Start Building
      </button>
      <button className="planning-build-cancel-btn" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </div>
  );
}

/* The planner's create_project proposal, awaiting an admin's answer. The
   agent cannot create anything itself; this card is the gate. It sits above
   the composer rather than in the log because it is a pending decision, and
   the conversation can carry on around it either way. */
function NewProjectCard({
  proposal, isAdmin, githubReady, busy, failed, error, onConfirm, onDismiss, onClearError,
}: {
  proposal: NewProjectProposal;
  isAdmin: boolean;
  githubReady: boolean;
  busy: boolean;
  /** An ok:false create: the steps show which one died; the card stays. */
  failed: CreateProjectResult | null;
  error: string | null;
  onConfirm: (github: boolean) => void;
  onDismiss: () => void;
  onClearError: () => void;
}) {
  // Prefilled from what the agent recorded (what the operator told it), and
  // only offered when a token can act -- same rule as the start form.
  const [github, setGithub] = useState(proposal.github);
  if (!isAdmin) {
    return (
      <p className="planning-proposal-note" role="note">
        The agent proposes a new project, <code>{proposal.name}</code> &mdash; an admin must confirm it.
      </p>
    );
  }
  return (
    <div className="planning-proposal" role="region" aria-label="Proposed new project">
      <div className="planning-proposal-head">
        The agent proposes a new project: <code>{proposal.name}</code>
      </div>
      {proposal.description && <p className="planning-proposal-desc">{proposal.description}</p>}
      {githubReady && (
        <label className="planning-check">
          <input type="checkbox" checked={github} onChange={(e) => setGithub(e.target.checked)} disabled={busy} />
          <span>Create a private GitHub repo</span>
        </label>
      )}
      {failed && (
        <div className="planning-create-failed">
          <StepList steps={failed.steps} />
          {failed.message && <p className="planning-create-message">{failed.message}</p>}
        </div>
      )}
      <div className="planning-proposal-actions">
        <button className="planning-build-confirm-btn" disabled={busy} onClick={() => onConfirm(githubReady && github)}>
          {busy ? "Creating project…" : "Confirm"}
        </button>
        <button className="planning-build-cancel-btn" disabled={busy} onClick={onDismiss}>
          Dismiss
        </button>
        {error && (
          <div className="planning-build-error" role="alert">
            {error}
            <button type="button" onClick={onClearError} aria-label="Dismiss error">×</button>
          </div>
        )}
      </div>
    </div>
  );
}

/* Why the last turn ended, read from the persisted session rather than the
   live stream. A stream event only reaches whoever is watching at that
   second; this is what an operator sees on returning to a session that
   stopped, which was previously nothing at all. */
function OutcomeBanner({ session }: { session: PlanningSessionMeta | null }) {
  const outcome = session?.last_outcome;
  if (!outcome || outcome === "completed") return null;
  const tone = outcome === "stopped" ? "info" : outcome === "budget" ? "warn" : "bad";
  const headline: Record<string, string> = {
    stopped: "You stopped this turn.",
    budget: "The per-turn budget ceiling was reached.",
    stalled: "The turn was ended after going silent.",
    error: "The turn failed.",
  };
  return (
    <div className={`planning-outcome planning-outcome--${tone}`}>
      <strong>{headline[outcome] ?? "The turn ended."}</strong>
      {session?.last_outcome_detail ? <span> {session.last_outcome_detail}</span> : null}
      {outcome !== "stopped" && (
        <span className="planning-outcome-hint">
          {" "}Everything it read is still in this session &mdash; ask it to continue rather than starting over.
        </span>
      )}
    </div>
  );
}

export function PlanningView({ repos, isAdmin, githubReady, onProjectCreated, session, onBuildNow, onSessionCreated, onSessionUpdated, buildError, onClearBuildError }: Props) {
  const [starting, setStarting] = useState(false);
  const [creating, setCreating] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [createFailed, setCreateFailed] = useState<CreateProjectResult | null>(null);
  // The confirm card's own state; the proposal itself is not mirrored here
  // (audit H-13) -- it is read from the stream, falling back to the session.
  const [deciding, setDeciding] = useState(false);
  const [proposalFailed, setProposalFailed] = useState<CreateProjectResult | null>(null);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [planOpen, setPlanOpen] = useState(true);
  const [archiving, setArchiving] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const logEndRef = useRef<HTMLDivElement>(null);
  const logContainerRef = useRef<HTMLDivElement>(null);
  const stream = usePlanningStream(session?.session_id ?? null);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [stream.log.length]);

  async function openSession(chosenRepo: string, route: RouteChoice) {
    const { session_id } = await createPlanningSession(chosenRepo, route);
    onSessionCreated({
      session_id,
      repo: chosenRepo,
      route: route === "auto" ? undefined : route,
      created_at: Date.now() / 1000,
      updated_at: Date.now() / 1000,
      title: null,
      plan_markdown: null,
      cost_usd: 0,
    });
  }

  async function handleStart(chosenRepo: string, route: RouteChoice = "auto") {
    setStarting(true);
    setStartError(null);
    try {
      await openSession(chosenRepo, route);
    } catch (err) {
      // Used to be try/finally only, so a failed create was an unhandled
      // rejection and a button that just went back to "Start" -- same
      // dead-button problem audit M-20 fixed for tasks.
      setStartError(err instanceof Error ? err.message : "Could not start the session. Please try again.");
    } finally {
      setStarting(false);
    }
  }

  async function handleCreateProject(form: NewProjectForm, route: RouteChoice) {
    setCreating(true);
    setStartError(null);
    setCreateFailed(null);
    try {
      const result = await createProject({
        name: form.name,
        description: form.description || undefined,
        github: form.github,
      });
      if (!result.ok) {
        // The server reports which step died (git init vs. push vs. config)
        // and leaves the form up so the operator can fix the cause and retry
        // with what they typed still in place.
        setCreateFailed(result);
        return;
      }
      const name = result.name || form.name;
      // App must know the repo before the session view names it, or the
      // sidebar and Build Now would be pointing at a project the dropdowns
      // have never heard of.
      await onProjectCreated(name);
      await openSession(name, route);
    } catch (err) {
      setStartError(err instanceof Error ? err.message : "Could not create the project. Please try again.");
    } finally {
      setCreating(false);
    }
  }

  async function handleSend() {
    if (!text.trim() || stream.running || uploading) return;
    const goal = text.trim();
    let attachments: AttachmentEntry[] | undefined;
    if (files.length) {
      setUploading(true);
      setUploadError(null);
      try {
        attachments = await uploadFiles(session!.repo, files);
      } catch (err) {
        setUploadError(err instanceof Error ? err.message : "upload failed");
        setUploading(false);
        return;
      }
      setUploading(false);
    }
    setText("");
    setFiles([]);
    stream.sendMessage(goal, attachments);
  }

  async function handleConfirmProposal(github: boolean) {
    if (!session) return;
    setDeciding(true);
    setProposalFailed(null);
    setProposalError(null);
    try {
      const result = await decideNewProject(session.session_id, { decision: "confirm", github });
      if (!result.project?.ok) {
        // The server reports which step died and leaves the session (and
        // the proposal) where they were, so the card stays for a retry.
        setProposalFailed(result.project);
        return;
      }
      // Same order as the start form: App must know the repo before the
      // view names it, or the header and Build Now would point at a project
      // the dropdowns have never heard of.
      await onProjectCreated(result.project.name);
      stream.clearNewProject();
      onSessionUpdated(result.session);
    } catch (err) {
      setProposalError(err instanceof Error ? err.message : "Could not create the project. Please try again.");
    } finally {
      setDeciding(false);
    }
  }

  async function handleDismissProposal() {
    if (!session) return;
    setDeciding(true);
    setProposalError(null);
    try {
      const result = await decideNewProject(session.session_id, { decision: "dismiss" });
      stream.clearNewProject();
      onSessionUpdated(result.session);
    } catch (err) {
      setProposalError(err instanceof Error ? err.message : "Could not dismiss the proposal. Please try again.");
    } finally {
      setDeciding(false);
    }
  }

  async function handleNewPlan() {
    if (!session) return;
    setArchiving(true);
    try {
      // Closes out the current plan (still fully reachable, just out of the
      // sidebar's default active list) before starting the next one fresh
      // in the same project -- matches how a finished task settles out of
      // the way once it's done, not deleted.
      await archivePlanningSession(session.session_id);
      const { session_id } = await createPlanningSession(session.repo);
      onSessionCreated({
        session_id,
        repo: session.repo,
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
        title: null,
        plan_markdown: null,
        cost_usd: 0,
        archived: false,
      });
    } finally {
      setArchiving(false);
    }
  }

  if (!session) {
    return (
      <NewSessionPanel
        repos={repos}
        isAdmin={isAdmin}
        githubReady={githubReady}
        onStart={handleStart}
        onCreateProject={handleCreateProject}
        starting={starting}
        creating={creating}
        error={startError}
        onClearError={() => setStartError(null)}
        failed={createFailed}
      />
    );
  }

  const repo = session.repo;
  // The stream is the fresher source (turn_complete lands before the
  // sidebar's next poll updates the session prop); the prop covers a
  // session opened from the list before the hook has hydrated.
  const proposal = stream.newProject ?? session.new_project ?? null;

  return (
    <div className="planning-view">
      <div className="planning-view-header">
        <span className="planning-view-repo">{repo}</span>
        <span className="planning-view-title">Planning session</span>
        <RouteBadge route={session?.route} reason={session?.route_reason} />
        <span className="planning-view-cost" title="Total spend for this session (no cap)">
          ${stream.costUsd.toFixed(3)}
        </span>
        <div className="planning-view-actions">
          {/* Same placement as TaskView's Stop: in the header beside the status,
              not down in the scrolling log. The log scrolls away, and a control
              you have to hunt for during a long turn is a control you do not
              have. Still gated on `running` — there is nothing to stop on an
              idle session, and a permanently greyed button is just clutter. */}
          {stream.running && session?.session_id && (
            <StopButton sessionId={session.session_id} onStopped={() => {}} />
          )}
          {stream.planMarkdown && <span className="planning-plan-badge">plan drafted</span>}
          <button className="planning-plan-toggle" onClick={() => setPlanOpen((o) => !o)} disabled={!stream.planMarkdown}>
            {planOpen ? "Hide plan" : "Show plan"}
          </button>
          {stream.planMarkdown && (
            <BuildNowPanel sessionRoute={session?.route} onConfirm={(budgetUsd, route) => onBuildNow(stream.planMarkdown!, repo, budgetUsd, route)} />
          )}
          {buildError && (
            <div className="planning-build-error" role="alert">
              {buildError}
              {onClearBuildError && <button type="button" onClick={onClearBuildError} aria-label="Dismiss error">×</button>}
            </div>
          )}
          <button className="planning-new-plan-btn" disabled={archiving} onClick={handleNewPlan} title="Archive this plan and start a fresh one for the same project">
            {archiving ? "Archiving..." : "New Plan"}
          </button>
        </div>
      </div>

      <div className="planning-view-body">
        <div className="planning-view-log-wrap">
        <JumpToBottom containerRef={logContainerRef} />
        <div className="planning-view-log" ref={logContainerRef}>
          <div className="chat-thread">
            {stream.hydrateError && <div className="hydrate-error">{stream.hydrateError}</div>}
            {!stream.running && <OutcomeBanner session={session} />}
            {stream.log.length === 0 && !stream.running && (
              <div className="planning-empty-hint">
                Tell it what you want to build, or ask it to research something first -- it can search the
                web, browse real pages (with screenshots for design reference), read this project (or any
                of your other projects, for comparison), and remembers what it learns here for next time.
              </div>
            )}
            {stream.log.map((entry, i) => (
              <PlanningEntry key={i} entry={entry} />
            ))}
            {stream.running && (
              <div className="chat-typing">
                <span /><span /><span />
              </div>
            )}
            {stream.sendError && <div className="planning-send-error">{stream.sendError}</div>}
            <div ref={logEndRef} />
          </div>
        </div>
        </div>

        {planOpen && stream.planMarkdown && (
          <div className="planning-plan-panel">
            <div className="planning-plan-panel-head">Current plan draft</div>
            <pre className="planning-plan-markdown">{stream.planMarkdown}</pre>
          </div>
        )}
      </div>

      {proposal && (
        <NewProjectCard
          key={proposal.name}
          proposal={proposal}
          isAdmin={isAdmin}
          githubReady={githubReady}
          busy={deciding}
          failed={proposalFailed}
          error={proposalError}
          onConfirm={handleConfirmProposal}
          onDismiss={handleDismissProposal}
          onClearError={() => setProposalError(null)}
        />
      )}

      <div className="planning-attach-row">
        <input
          ref={fileInput}
          type="file"
          multiple
          accept=".png,.jpg,.jpeg,.webp,.gif,.pdf,.csv,.tsv,.txt,.json,.md,.xlsx,.xls"
          style={{ display: "none" }}
          onChange={(e) => {
            setFiles((prev) => [...prev, ...Array.from(e.target.files ?? [])]);
            e.target.value = "";
          }}
        />
        <button type="button" className="attach-btn" disabled={stream.running || uploading} onClick={() => fileInput.current?.click()}>
          📎 Attach
        </button>
        {uploadError && <span className="planning-send-error">{uploadError}</span>}
        {files.length > 0 && (
          <div className="attach-chips">
            {files.map((f, i) => (
              <span className="attach-chip" key={i}>
                {f.name}
                <button type="button" onClick={() => setFiles((prev) => prev.filter((_, j) => j !== i))}>×</button>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="message-input">
        <AutoGrowTextarea
          ariaLabel="Message the planning agent"
        minHeight={84}
        maxHeight={260}
          placeholder={stream.running ? "Thinking..." : "Ask a question, share a reference, or describe what you want... (Shift+Enter for a new line)"}
          value={text}
          disabled={stream.running}
          onChange={setText}
          onSubmit={handleSend}
        />
        <button onClick={handleSend} disabled={stream.running || uploading || !text.trim()}>
          {uploading ? "…" : stream.running ? "…" : "➤"}
        </button>
      </div>
    </div>
  );
}
