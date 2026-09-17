import { useState } from "react";
import { removeProject, type RemoveProjectResult } from "../api";
import { StepList } from "./StepList";
import "./RemoveProject.css";

/* Taking a project off the agent.
 *
 * The confirmation asks the operator to type the project's name. That is not
 * ceremony: this control sits in a list of projects, one row apart from the
 * next one, and the cost of hitting the wrong row is an archive-or-delete of
 * everything the agent learned about something that was working fine.
 *
 * The panel leads with what is NOT affected, because that is the question
 * anyone hovering over a red button in a list of their repositories is
 * actually asking. The live repo, its branches and its remote are untouched;
 * the agent forgets the project, that is all.
 */
export function RemoveProject({ name, live, onRemoved }: {
  name: string;
  live: string;
  onRemoved: () => void | Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [memory, setMemory] = useState<"archive" | "delete">("archive");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RemoveProjectResult | null>(null);

  function reset() {
    setOpen(false);
    setTyped("");
    setError(null);
    setMemory("archive");
  }

  async function go() {
    setBusy(true);
    setError(null);
    try {
      const res = await removeProject(name, memory);
      setResult(res);
      setOpen(false);
      setTyped("");
      await onRemoved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not remove the project");
    } finally {
      setBusy(false);
    }
  }

  if (result) {
    return (
      <div className="rmproj-done">
        <p className="rmproj-done-head">
          <strong>{result.name}</strong> is no longer configured.
          {result.archive
            ? <> Its memory is archived as <code>{result.archive}</code>.</>
            : <> Its memory was deleted.</>}
        </p>
        <StepList steps={result.steps} />
        <p className="rmproj-note">
          <code>{result.live_untouched}</code> was not touched — the repository, its branches
          and its remote are exactly as they were.
        </p>
      </div>
    );
  }

  if (!open) {
    return (
      <div className="rmproj">
        <button type="button" className="rmproj-open" onClick={() => setOpen(true)}>
          Remove from Tektonix
        </button>
        <span className="rmproj-hint">The repository itself is not affected.</span>
      </div>
    );
  }

  return (
    <div className="rmproj-confirm">
      <p className="rmproj-lead">
        Tektonix will stop watching <strong>{name}</strong>: its workspace, its deploy key and its
        configuration go away. <code>{live}</code> is <strong>not</strong> touched — no files, no
        branches, no remote.
      </p>

      <fieldset className="rmproj-choice">
        <legend>What the agent learned about it</legend>
        <label className={memory === "archive" ? "is-chosen" : ""}>
          <input
            type="radio"
            name={`memory-${name}`}
            checked={memory === "archive"}
            onChange={() => setMemory("archive")}
          />
          <span>
            <strong>Archive it</strong>
            <em>
              Its memory, generated skills, planning sessions and task history are saved to a
              file. Adding {name} again later offers to restore them, so it picks up where it
              left off.
            </em>
          </span>
        </label>
        <label className={memory === "delete" ? "is-chosen" : ""}>
          <input
            type="radio"
            name={`memory-${name}`}
            checked={memory === "delete"}
            onChange={() => setMemory("delete")}
          />
          <span>
            <strong>Delete it</strong>
            <em>Removed outright. There is nothing to restore from afterwards.</em>
          </span>
        </label>
      </fieldset>

      <label className="rmproj-type">
        <span>Type <code>{name}</code> to confirm</span>
        <input
          className="wiz-input"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          aria-label={`Type ${name} to confirm removal`}
        />
      </label>

      <div className="rmproj-actions">
        <button
          type="button"
          className="rmproj-go"
          disabled={typed !== name || busy}
          onClick={() => void go()}
        >
          {busy ? "Removing…" : memory === "archive" ? "Remove and archive" : "Remove and delete"}
        </button>
        <button type="button" className="wiz-btn" disabled={busy} onClick={reset}>
          Cancel
        </button>
      </div>
      {error && <p className="wiz-error" role="alert">{error}</p>}
    </div>
  );
}
