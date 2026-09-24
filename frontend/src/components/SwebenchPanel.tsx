import { useCallback, useEffect, useState } from "react";
import { getSwebench, getSwebenchRun, getSwebenchTask } from "../api";
import type { SwebenchOverview, SwebenchRun, SwebenchTaskDetail, SwebenchTaskRow } from "../types";
import { minutes, usd } from "../evalsFormat";
import { headlineRun, KIND_LABEL, modelList, scoreLabel, scoreText, swebenchScorecard } from "../swebenchFormat";
import "./EvalsPanel.css";
import "./SwebenchPanel.css";

/**
 * SWE-bench Verified: real GitHub issues from twelve Python projects, run
 * through the real agent in each task's official image and graded by the
 * official harness (evals/SWEBENCH.md). Where the golden suite above answers
 * "did my change make it better?", this is the number the rest of the world
 * compares agents by.
 *
 * Read-only. A run is started from a shell (scripts/run_swebench.py): it
 * pulls gigabytes of images and runs for hours. This reads what it leaves,
 * which it rewrites after every task, so a run shows here as it goes.
 */

const POLL_RUNNING_MS = 15_000;
const POLL_IDLE_MS = 120_000;

export function SwebenchPanel() {
  const [data, setData] = useState<SwebenchOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [run, setRun] = useState<SwebenchRun | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await getSwebench());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not load SWE-bench runs");
    }
  }, []);

  const runs = data?.runs ?? [];
  const anyRunning = runs.some((r) => r.state === "running");
  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), anyRunning ? POLL_RUNNING_MS : POLL_IDLE_MS);
    return () => clearInterval(t);
  }, [load, anyRunning]);

  const headline = headlineRun(runs);
  const shown = runs.find((r) => r.name === selected) ?? headline ?? runs[0];
  const referenceFails = data?.gold_check.reference_fails ?? [];

  // The run on screen, task by task; refreshed with the list while it runs.
  const shownKey = shown ? `${shown.name}:${shown.done}:${shown.graded_count}:${shown.state}` : "";
  useEffect(() => {
    if (!shown) return;
    let live = true;
    getSwebenchRun(shown.name).then((r) => live && setRun(r)).catch(() => {});
    return () => {
      live = false;
    };
  }, [shownKey]);   // eslint-disable-line react-hooks/exhaustive-deps

  async function copy() {
    if (!headline) return;
    try {
      await navigator.clipboard.writeText(swebenchScorecard(headline, referenceFails));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* no clipboard: nothing to do */
    }
  }

  if (error) {
    return (
      <div className="analytics-section">
        <h2>SWE-bench Verified</h2>
        <p className="bench-warning" role="note">Couldn&apos;t load SWE-bench runs: {error}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="analytics-section">
        <h2>SWE-bench Verified</h2>
        <p className="analytics-section-sub">Loading…</p>
      </div>
    );
  }

  const tasks = run && run.summary.name === shown?.name ? run.tasks : [];

  return (
    <div className="analytics-section evals swebench">
      <div className="evals-head">
        <div>
          <h2>SWE-bench Verified</h2>
          <p className="analytics-section-sub">
            The public benchmark agents are compared on: {data.dataset_size} real GitHub issues from twelve Python
            projects. Each task runs in its official image with no internet access and is graded by the official
            harness. Only a run of all {data.dataset_size} tasks is the published number. Runs are started from the
            server (<code>scripts/run_swebench.py</code>).
          </p>
        </div>
      </div>

      {headline ? (
        <div className="evals-scorecard">
          <div className="evals-score">
            <span className="evals-score-big">{scoreText(headline)}</span>
            <span className="evals-score-label">{scoreLabel(headline)}</span>
          </div>
          <div className="evals-score-meta">
            <span>
              {headline.kind === "full" ? "Latest full run" : `Latest run (${KIND_LABEL[headline.kind]})`}{" "}
              {headline.started_at?.slice(0, 10)}
              {headline.state === "running" && <> · <strong>running</strong>, {headline.done} of {headline.total} done</>}
              {headline.state === "stopped" && <> · stopped</>}
            </span>
            <span>
              {usd(headline.total_cost_usd)}
              {headline.duration_s != null && headline.state !== "running" && <> · {minutes(headline.duration_s)}</>}
            </span>
            {modelList(headline.models).length > 0 && (
              <span className="swebench-models">{modelList(headline.models).join(" · ")}</span>
            )}
            {headline.notes && <span className="evals-score-notes">“{headline.notes}”</span>}
          </div>
          {headline.graded && (
            <button type="button" className="evals-btn-secondary evals-copy" onClick={() => void copy()}>
              {copied ? "Copied" : "Copy scorecard"}
            </button>
          )}
        </div>
      ) : (
        <p className="analytics-section-sub">No runs yet.</p>
      )}

      {headline?.state === "running" && (
        <div className="evals-progress" role="status">
          <div className="evals-progress-top">
            <span>
              <strong>{headline.done}</strong> of {headline.total} tasks done · {headline.graded_count} graded,{" "}
              {headline.resolved ?? 0} resolved so far · {usd(headline.total_cost_usd)} spent
            </span>
          </div>
          <div className="evals-bar" aria-hidden="true">
            <div className="evals-bar-fill" style={{ width: `${(100 * headline.done) / Math.max(1, headline.total)}%` }} />
          </div>
        </div>
      )}

      {data.gold_check.checked > 0 && (
        <p className="analytics-section-sub swebench-gold">
          Reference fixes checked on {data.gold_check.checked} task{data.gold_check.checked === 1 ? "" : "s"}:{" "}
          {referenceFails.length === 0 ? "all pass in the official images." : (
            <>
              the official fix itself fails on <strong>{referenceFails.join(", ")}</strong> in the official
              images, so no agent can resolve {referenceFails.length === 1 ? "it" : "them"}.
            </>
          )}
        </p>
      )}

      {runs.length > 0 && (
        <>
          <h3 className="evals-subhead">Runs</h3>
          <div className="evals-history" role="list">
            {runs.slice(0, 12).map((r) => (
              <button
                key={r.name}
                type="button"
                role="listitem"
                className={`evals-history-row ${shown?.name === r.name ? "is-current" : ""}`}
                onClick={() => setSelected(r.name)}
              >
                <span className="evals-history-date">{r.started_at?.replace("T", " ").slice(0, 16)}</span>
                <span className="evals-history-score swebench-history-score">{scoreText(r)}</span>
                <span className="evals-bar evals-bar--mini" aria-hidden="true">
                  <span
                    className="evals-bar-fill"
                    style={{ width: `${r.graded_count ? (100 * (r.resolved ?? 0)) / (r.graded ? r.total : r.graded_count) : 0}%` }}
                  />
                </span>
                <span className="evals-history-cost">{usd(r.total_cost_usd)}</span>
                <span className="evals-history-notes">
                  <em className={`evals-partial swebench-kind is-${r.kind}`}>{KIND_LABEL[r.kind]}</em>
                  {r.state !== "done" && <em className="evals-partial">{r.state}</em>} {r.notes || r.name}
                </span>
              </button>
            ))}
          </div>
        </>
      )}

      {shown && (
        <>
          <h3 className="evals-subhead">
            Tasks — {shown.name} ({shown.done} of {shown.total} run)
          </h3>
          <div className="evals-tasks">
            {tasks.map((t) => <TaskRow key={t.id} runName={shown.name} task={t} />)}
            {!run && <p className="analytics-section-sub">Loading tasks…</p>}
          </div>
        </>
      )}
    </div>
  );
}

function mark(t: SwebenchTaskRow): { cls: string; sym: string; title: string } {
  if (t.resolved === true) return { cls: "is-pass", sym: "✓", title: "resolved (official harness)" };
  if (t.resolved === false) return { cls: "is-fail", sym: "✗", title: "not resolved (official harness)" };
  if (t.started) return { cls: "is-waiting", sym: "•", title: "run, waiting to be graded" };
  return { cls: "is-pending", sym: "○", title: "not run yet" };
}

function TaskRow({ runName, task: t }: { runName: string; task: SwebenchTaskRow }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<SwebenchTaskDetail | null>(null);
  const [showConversation, setShowConversation] = useState(false);
  const m = mark(t);

  useEffect(() => {
    if (!open || detail || !t.started) return;
    let live = true;
    getSwebenchTask(runName, t.id).then((d) => live && setDetail(d)).catch(() => {});
    return () => {
      live = false;
    };
  }, [open, detail, runName, t.id, t.started]);

  const failed = [...(t.tests?.fail_to_pass_failed ?? []), ...(t.tests?.pass_to_pass_failed ?? [])];
  return (
    <div className={`evals-task ${m.cls}`}>
      <button type="button" className="evals-task-row" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="evals-task-mark" title={m.title}>{m.sym}</span>
        <span className="evals-task-id">{t.id}</span>
        {t.reference_fails && <span className="evals-badge is-fail" title="the official fix fails too">unresolvable</span>}
        {t.outcome && t.outcome !== "not_run" && <span className="evals-task-cat">{t.outcome}</span>}
        {t.started && (
          <span className="evals-task-meta">{usd(t.cost_usd)} · {minutes(t.duration_s)}</span>
        )}
      </button>
      {open && (
        <div className="evals-task-detail">
          {!t.started ? (
            <div className="evals-task-line">Not run yet.</div>
          ) : (
            <>
              <div className="evals-task-line">
                Outcome <strong>{t.outcome}</strong>
                {t.review_verdict && <> · reviewer <strong>{t.review_verdict}</strong></>}
                {t.reason && <> · {t.reason}</>}
              </div>
              {t.tests && (
                <div className="evals-task-line">
                  Tests the fix had to pass: {t.tests.fail_to_pass_passed} of{" "}
                  {t.tests.fail_to_pass_passed + t.tests.fail_to_pass_failed.length} passed · tests that already
                  passed: {t.tests.pass_to_pass_passed} of {t.tests.pass_to_pass_passed + t.tests.pass_to_pass_failed.length}{" "}
                  still pass
                  {t.tests.patch_applied === false && <> · <strong>the patch did not apply</strong></>}
                </div>
              )}
              {failed.map((name) => (
                <div key={name} className="evals-assert is-fail">✗ {name}</div>
              ))}
              {modelList(t.models).length > 0 && (
                <div className="evals-task-line">Models: {modelList(t.models).join(" · ")}</div>
              )}
              {detail == null ? (
                <div className="evals-task-line">Loading…</div>
              ) : (
                <>
                  {detail.patch ? <pre className="evals-diff">{detail.patch}</pre>
                    : <div className="evals-task-line">No patch: the agent changed no source file.</div>}
                  {detail.conversation.length > 0 && (
                    <button
                      type="button"
                      className="evals-btn-secondary swebench-conv-toggle"
                      onClick={() => setShowConversation(!showConversation)}
                    >
                      {showConversation ? "Hide the conversation" : `Show the agent's conversation (${
                        detail.conversation.reduce((n, c) => n + c.messages.length, 0)} messages)`}
                    </button>
                  )}
                  {showConversation && <Conversation detail={detail} />}
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Conversation({ detail }: { detail: SwebenchTaskDetail }) {
  return (
    <div className="swebench-conv">
      {detail.conversation.map((thread) => (
        <div key={`${thread.generation}:${thread.namespace}`} className="swebench-thread">
          <div className="swebench-thread-head">
            {thread.namespace === "coordinator" ? "Coordinator" : "Subagent"}
            {thread.generation > 0 && <> · fresh thread {thread.generation}</>}
          </div>
          {thread.messages.map((msg, i) => (
            <div key={i} className={`swebench-msg is-${msg.role}`}>
              <span className="swebench-msg-role">{msg.role === "tool" ? `tool ${msg.name ?? ""}` : msg.role}</span>
              {msg.text && <pre className="swebench-msg-text">{msg.text}</pre>}
              {msg.tool_calls.map((c, j) => (
                <details key={j} className="swebench-call">
                  <summary>→ {c.name}</summary>
                  <pre>{c.args}</pre>
                </details>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
