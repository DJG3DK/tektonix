import { useCallback, useEffect, useState } from "react";
import {
  getSwebench, getSwebenchRun, getSwebenchRunLog, getSwebenchTask, startSwebenchRun, stopSwebenchRun,
} from "../api";
import type { SwebenchOverview, SwebenchReview, SwebenchRun, SwebenchTaskDetail, SwebenchTaskRow } from "../types";
import { minutes, usd } from "../evalsFormat";
import { headlineRun, KIND_LABEL, modelList, scoreLabel, scoreText, swebenchScorecard } from "../swebenchFormat";
import { SwebenchHostBlock } from "./SwebenchHost";
import "./EvalsPanel.css";
import "./SwebenchPanel.css";

/**
 * SWE-bench Verified: real GitHub issues from twelve Python projects, run
 * through the real agent in each task's official image and graded by the
 * official harness (evals/SWEBENCH.md). Where the golden suite above answers
 * "did my change make it better?", this is the number the rest of the world
 * compares agents by.
 *
 * A run is a detached runner on the server (agent/routers/swebench.py): it
 * pulls gigabytes of images and runs for hours. This panel starts and stops
 * one and reads what it leaves, which it rewrites after every task, so a run
 * shows here as it goes. Grading is a separate step, done later.
 */

const POLL_RUNNING_MS = 15_000;
const POLL_IDLE_MS = 120_000;
const LOG_POLL_MS = 15_000;
const LOG_LINES = 80;

/** Sample sizes the Start control offers; 500 is the full run (the contract's
 *  meaning of `sample: 500`), the only one whose score is publishable. */
const SAMPLE_SIZES = [50, 100, 250, 500] as const;
const FULL = 500;

/** What a run costs and takes, from the one measured point: 50 tasks ran
 *  about $18 and 4 to 6 hours at ten at once. Cost scales with the tasks;
 *  time with the tasks and inversely with how many run at once. */
function roughCost(sample: number): number {
  return Math.round((18 * sample) / 50);
}
function roughHours(sample: number, parallel: number): [number, number] {
  const scale = (sample / 50) * (10 / Math.max(1, parallel));
  return [Math.max(1, Math.round(4 * scale)), Math.max(1, Math.round(6 * scale))];
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

const STOP_PROMPT = "Stop this run? Tasks already finished keep their results; nothing is graded until you grade it later.";

export function SwebenchPanel() {
  const [data, setData] = useState<SwebenchOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [run, setRun] = useState<SwebenchRun | null>(null);
  const [copied, setCopied] = useState(false);
  // The Start control (mirrors EvalsPanel): a confirm step, then a busy state.
  const [confirming, setConfirming] = useState(false);
  const [sample, setSample] = useState<number>(50);
  // The number fields hold what was typed; the numbers used are clamped
  // below. Clamping on every keystroke snapped a cleared field to its
  // minimum, so typing "5" after clearing "10" gave "15".
  const [seedRaw, setSeedRaw] = useState("1");
  const [parallelRaw, setParallelRaw] = useState("10");
  const [budgetRaw, setBudgetRaw] = useState("3");
  const seed = Math.max(0, Math.floor(Number(seedRaw) || 0));
  const parallel = clamp(Math.floor(Number(parallelRaw) || 10), 1, 16);
  const budget = clamp(Number(budgetRaw) || 3, 0.5, 10);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState<null | "start" | "stop">(null);
  const [actionError, setActionError] = useState<string | null>(null);

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
  // One real run at a time: a diagnostic experiment does not hold the box.
  const blocking = runs.find((r) => r.state === "running" && r.kind !== "diagnostic");
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

  async function start() {
    setBusy("start");
    setActionError(null);
    try {
      const trimmed = notes.trim();
      const res = await startSwebenchRun({
        sample, seed, parallel, budget_usd: budget, ...(trimmed ? { notes: trimmed } : {}),
      });
      setConfirming(false);
      setNotes("");
      setSelected(res.name);
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "could not start the run");
    } finally {
      setBusy(null);
    }
  }

  async function stop(name: string) {
    if (!confirm(STOP_PROMPT)) return;
    setBusy("stop");
    setActionError(null);
    try {
      await stopSwebenchRun(name);
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "could not stop the run");
    } finally {
      setBusy(null);
    }
  }

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

  const shownRun = run && run.summary.name === shown?.name ? run : null;
  const tasks = shownRun ? shownRun.tasks : [];

  return (
    <div className="analytics-section evals swebench">
      <div className="evals-head">
        <div>
          <h2>SWE-bench Verified</h2>
          <p className="analytics-section-sub">
            The public benchmark agents are compared on: {data.dataset_size} real GitHub issues from twelve Python
            projects. Each task runs in its official image with no internet access and is graded by the official
            harness. Only a run of all {data.dataset_size} tasks is the published number.
          </p>
        </div>
      </div>

      <div className="swebench-start" role="group" aria-label="Start a run">
        <span className="swebench-start-title">Start a run</span>
        <label className="swebench-field">
          Sample
          <select value={sample} onChange={(e) => setSample(Number(e.target.value))} disabled={confirming || busy !== null}>
            {SAMPLE_SIZES.map((n) => (
              <option key={n} value={n}>{n === FULL ? `All ${data.dataset_size}` : n}</option>
            ))}
          </select>
        </label>
        <label className="swebench-field">
          Seed
          <input type="number" min={0} step={1} value={seedRaw} disabled={confirming || busy !== null}
            onChange={(e) => setSeedRaw(e.target.value)} />
        </label>
        <label className="swebench-field">
          Tasks at once
          <input type="number" min={1} max={16} step={1} value={parallelRaw} disabled={confirming || busy !== null}
            onChange={(e) => setParallelRaw(e.target.value)} />
        </label>
        <label className="swebench-field">
          Per-task budget $
          <input type="number" min={0.5} max={10} step={0.5} value={budgetRaw} disabled={confirming || busy !== null}
            onChange={(e) => setBudgetRaw(e.target.value)} />
        </label>
        <button
          type="button"
          className="evals-run-btn"
          onClick={() => setConfirming(true)}
          disabled={Boolean(blocking) || confirming || busy !== null}
        >
          Start
        </button>
        {blocking && (
          <span className="swebench-start-blocked">
            A run is already in progress ({blocking.name}); stop it or wait for it to finish before starting another.
          </span>
        )}
      </div>

      {confirming && !blocking && (
        <div className="evals-confirm" role="dialog" aria-label="Start a SWE-bench run">
          <p>
            Runs {sample === FULL ? <>all <strong>{data.dataset_size}</strong> tasks</> : <><strong>{sample}</strong> tasks (seed {seed})</>}{" "}
            through the real agent, each in its official image, <strong>{parallel}</strong> at once, up to{" "}
            <strong>{usd(budget)}</strong> per task. It keeps running if the server restarts. Nothing is graded until
            you grade it afterwards.
          </p>
          <p>
            50 tasks ran about $18 and 4 to 6 hours at ten at once; 500 is roughly ten times that. This run:
            roughly <strong>${roughCost(sample)}</strong> and{" "}
            <strong>{roughHours(sample, parallel)[0]} to {roughHours(sample, parallel)[1]} hours</strong>.
          </p>
          <input
            className="evals-notes"
            placeholder="What changed since the last run? (optional — saved with the result)"
            value={notes}
            maxLength={300}
            onChange={(e) => setNotes(e.target.value)}
          />
          <div className="evals-confirm-row">
            <button type="button" className="evals-btn-secondary" onClick={() => setConfirming(false)} disabled={busy !== null}>
              Cancel
            </button>
            <button type="button" className="evals-run-btn" onClick={() => void start()} disabled={busy !== null}>
              {busy === "start" ? "Starting…" : "Start run"}
            </button>
          </div>
        </div>
      )}

      {actionError && <p className="bench-warning" role="alert">{actionError}</p>}

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
            <button type="button" className="evals-btn-secondary" onClick={() => void stop(headline.name)} disabled={busy !== null}>
              {busy === "stop" ? "Stopping…" : "Stop"}
            </button>
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
              <div key={r.name} className="swebench-run-line">
              <button
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
                  {r.state !== "done" && <em className="evals-partial">{r.state}</em>}
                  {r.shards && <em className="evals-partial">{r.shards.length} processes</em>} {r.notes || r.name}
                </span>
              </button>
              {r.state === "running" && (
                <button
                  type="button"
                  className="evals-btn-secondary swebench-run-stop"
                  onClick={() => void stop(r.name)}
                  disabled={busy !== null}
                  aria-label={`Stop ${r.name}`}
                >
                  Stop
                </button>
              )}
              </div>
            ))}
          </div>
        </>
      )}

      {shownRun?.host && <SwebenchHostBlock host={shownRun.host} />}

      {shown && (
        <>
          <h3 className="evals-subhead">
            Tasks — {shown.name} ({shown.done} of {shown.total} run)
          </h3>
          <RunnerLog key={shown.name} name={shown.name} running={shown.state === "running"} />
          <div className="evals-tasks">
            {tasks.map((t) => <TaskRow key={t.id} runName={shown.name} task={t} />)}
            {!run && <p className="analytics-section-sub">Loading tasks…</p>}
          </div>
        </>
      )}
    </div>
  );
}

/** The runner's own log, the last 80 lines: fetched when opened, and every
 *  15 s while the run is still going. Keyed on the run name by the caller, so
 *  a different run starts closed and empty. */
function RunnerLog({ name, running }: { name: string; running: boolean }) {
  const [open, setOpen] = useState(false);
  const [lines, setLines] = useState<string[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let live = true;
    const fetchLog = () =>
      getSwebenchRunLog(name, LOG_LINES)
        .then((r) => { if (live) { setLines(r.lines); setErr(null); } })
        .catch((e) => { if (live) setErr(e instanceof Error ? e.message : "could not load the log"); });
    void fetchLog();
    const t = running ? setInterval(() => void fetchLog(), LOG_POLL_MS) : null;
    return () => {
      live = false;
      if (t) clearInterval(t);
    };
  }, [open, name, running]);

  return (
    <details className="swebench-log" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Runner log{running ? " · refreshes every 15 s" : ""}</summary>
      {err && <p className="bench-warning" role="note">Couldn&apos;t load the log: {err}</p>}
      {!err && lines == null && <p className="analytics-section-sub">Loading…</p>}
      {lines != null && (
        lines.length === 0
          ? <p className="analytics-section-sub">The log is empty.</p>
          : <pre className="swebench-log-text">{lines.join("\n")}</pre>
      )}
    </details>
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
    getSwebenchTask(t.run ?? runName, t.id).then((d) => live && setDetail(d)).catch(() => {});
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
        {t.harness_note && (
          <span className="evals-task-cat swebench-harness-note" title="the official harness's own note on this task">
            harness: {t.harness_note}
          </span>
        )}
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
                  {(detail.review ?? (t.review_verdict ? { verdict: t.review_verdict } : null)) && (
                    <Review review={detail.review ?? { verdict: t.review_verdict }} />
                  )}
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

/** What the reviewer said, not only its verdict: its summary, each finding,
 *  and the message it sent the agent (2026-09-25: only the verdict word
 *  survived a run, so "what did the reviewer say" had no answer). */
function Review({ review }: { review: SwebenchReview }) {
  const findings = review.findings ?? [];
  const hasText = Boolean(review.summary || findings.length || review.agentMessage);
  return (
    <details className="swebench-review">
      <summary>
        Reviewer: <strong>{review.verdict ?? "no verdict"}</strong>
        {review.escalated && <> · escalated</>}
        {!hasText && <> · no text kept</>}
      </summary>
      {review.summary && <p className="swebench-review-summary">{review.summary}</p>}
      {findings.map((f, i) => (
        <div key={i} className={`evals-assert ${f.severity === "blocking" ? "is-fail" : ""}`}>
          [{f.severity}] {f.file ? `${f.file}: ` : ""}{f.issue}
        </div>
      ))}
      {review.agentMessage && <pre className="swebench-msg-text">{review.agentMessage}</pre>}
    </details>
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
