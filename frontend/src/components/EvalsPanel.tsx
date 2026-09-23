import { useCallback, useEffect, useRef, useState } from "react";
import { getEvalRun, getEvals, startEvalRun, stopEvalRun } from "../api";
import type { EvalReport, EvalsOverview } from "../types";
import { compareRuns, day, minutes, pct, scorecardText, usd } from "../evalsFormat";
import "./EvalsPanel.css";

/**
 * The golden suite: fixed coding tasks, run through the real agent, checks and
 * reviewer, scored by assertions rather than by "it shipped" (evals/README.md).
 *
 * Where the Benchmarks panel above answers "is it doing better on my work?",
 * this answers "did that change make it better?" -- the same questions every
 * time, so a moved number is the change and not the workload. It is also the
 * number worth quoting: the scorecard copies as one paragraph.
 *
 * A run is a detached process on the server (agent/routers/evals.py). This
 * panel starts it, polls its progress file, and reads the reports it leaves.
 */

const POLL_RUNNING_MS = 5_000;
const POLL_IDLE_MS = 60_000;

export function EvalsPanel() {
  const [data, setData] = useState<EvalsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState<null | "start" | "stop">(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<EvalReport | null>(null);
  const [openTask, setOpenTask] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const wasRunning = useRef(false);

  const load = useCallback(async () => {
    try {
      const d = await getEvals();
      setData(d);
      setError(null);
      // A run that just finished: its report is now the newest, show it.
      if (wasRunning.current && !d.status?.running) setSelected(null);
      wasRunning.current = Boolean(d.status?.running);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not load evals");
    }
  }, []);

  const running = Boolean(data?.status?.running);
  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), running ? POLL_RUNNING_MS : POLL_IDLE_MS);
    return () => clearInterval(t);
  }, [load, running]);

  const runs = data?.runs ?? [];
  const fullRuns = runs.filter((r) => r.full);
  const latest = fullRuns[0];
  const shown = runs.find((r) => r.name === selected) ?? latest ?? runs[0];
  const shownIndex = shown ? fullRuns.findIndex((r) => r.name === shown.name) : -1;
  const baseline = shownIndex >= 0 ? fullRuns[shownIndex + 1] : undefined;
  const { regressed, fixed } = shown ? compareRuns(shown, baseline) : { regressed: [], fixed: [] };

  // The full report (assertions, diffs) for the run on screen.
  useEffect(() => {
    if (!shown) return;
    let live = true;
    setReport(null);
    getEvalRun(shown.name).then((r) => live && setReport(r)).catch(() => {});
    return () => {
      live = false;
    };
  }, [shown?.name]);   // eslint-disable-line react-hooks/exhaustive-deps

  async function start() {
    setBusy("start");
    setActionError(null);
    try {
      await startEvalRun(notes.trim());
      setConfirming(false);
      setNotes("");
      wasRunning.current = true;
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "could not start the run");
    } finally {
      setBusy(null);
    }
  }

  async function stop() {
    if (!confirm("Stop the eval run? Tasks already finished keep their results; the rest are not run.")) return;
    setBusy("stop");
    setActionError(null);
    try {
      await stopEvalRun();
      await load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "could not stop the run");
    } finally {
      setBusy(null);
    }
  }

  async function copy() {
    if (!latest) return;
    try {
      await navigator.clipboard.writeText(scorecardText(latest));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* no clipboard (an insecure origin, a locked-down browser): nothing to do */
    }
  }

  if (error) {
    return (
      <div className="analytics-section">
        <h2>Golden suite</h2>
        <p className="bench-warning" role="note">Couldn&apos;t load the eval suite: {error}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="analytics-section">
        <h2>Golden suite</h2>
        <p className="analytics-section-sub">Loading…</p>
      </div>
    );
  }

  const status = data.status;
  const cats = Object.keys(data.suite.by_category).length;
  const b = latest?.benchmarks || {};
  const lastFailedToStart = status && !status.running && status.finished_at == null && status.done === 0 && !status.pid;

  return (
    <div className="analytics-section evals">
      <div className="evals-head">
        <div>
          <h2>Golden suite</h2>
          <p className="analytics-section-sub">
            {data.suite.tasks} fixed coding tasks across {cats} categories, run through the real agent, checks and
            independent reviewer in an isolated sandbox — scored by assertions, not by whether it shipped.
          </p>
        </div>
        {!running && !confirming && (
          <button type="button" className="evals-run-btn" onClick={() => setConfirming(true)}>
            Run the golden suite
          </button>
        )}
      </div>

      {confirming && !running && (
        <div className="evals-confirm" role="dialog" aria-label="Run the golden suite">
          <p>
            Runs all {data.suite.tasks} tasks through the real agent. It has its own store, reviewer and practice
            repos — your projects are not touched. It keeps running if the server restarts.
            {data.estimate.cost_usd != null && (
              <>
                {" "}Estimated <strong>{usd(data.estimate.cost_usd)}</strong> and{" "}
                <strong>{minutes(data.estimate.duration_s)}</strong>, from the last run.
              </>
            )}
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

      {running && status && (
        <div className="evals-progress" role="status">
          <div className="evals-progress-top">
            <span>
              Running — <strong>{status.done}</strong> of {status.tasks_total} done, {status.passed} passed,{" "}
              {usd(status.spent_usd)} spent
              {status.started_at && <> · {minutes(Date.now() / 1000 - status.started_at)} in</>}
            </span>
            <button type="button" className="evals-btn-secondary" onClick={() => void stop()} disabled={busy !== null}>
              {busy === "stop" ? "Stopping…" : "Stop"}
            </button>
          </div>
          <div className="evals-bar" aria-hidden="true">
            <div className="evals-bar-fill" style={{ width: `${(100 * status.done) / Math.max(1, status.tasks_total)}%` }} />
          </div>
          {status.results.length > 0 && (
            <div className="evals-live">
              {status.results.map((r) => (
                <span key={r.id} className={`evals-chip ${r.passed ? "is-pass" : "is-fail"}`} title={r.outcome}>
                  {r.passed ? "✓" : "✗"} {r.id}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {actionError && <p className="bench-warning" role="alert">{actionError}</p>}
      {lastFailedToStart && (
        <p className="bench-warning" role="note">
          The last run did not start. The server's <code>logs/evals/last-run.log</code> says why.
        </p>
      )}

      {latest ? (
        <div className="evals-scorecard">
          <div className="evals-score">
            <span className="evals-score-big">
              {latest.tasks_passed}/{latest.tasks_attempted}
            </span>
            <span className="evals-score-label">tasks passed · {pct(latest.pass_rate)}</span>
          </div>
          <div className="evals-score-meta">
            <span>Latest full run {day(latest.started_at)}</span>
            <span>{usd(latest.total_cost_usd)} · {minutes(latest.duration_s)}</span>
            {latest.notes && <span className="evals-score-notes">“{latest.notes}”</span>}
            <div className="evals-cats">
              {Object.entries(latest.by_category)
                .sort(([a], [z]) => a.localeCompare(z))
                .map(([c, v]) => (
                  <span key={c} className={`evals-chip ${v.passed === v.tasks ? "is-pass" : "is-fail"}`}>
                    {c} {v.passed}/{v.tasks}
                  </span>
                ))}
            </div>
          </div>
          <button type="button" className="evals-btn-secondary evals-copy" onClick={() => void copy()}>
            {copied ? "Copied" : "Copy scorecard"}
          </button>
        </div>
      ) : (
        <p className="analytics-section-sub">No full run yet — start one above.</p>
      )}

      {latest && (
        <div className="analytics-cards">
          <div className="analytics-card">
            <span className="analytics-card-label">First-pass reviews</span>
            <span className="analytics-card-value">{pct(b.first_pass_rate as number | undefined)}</span>
            <span className="analytics-card-sub">passed the independent reviewer with no redo</span>
          </div>
          <div className="analytics-card">
            <span className="analytics-card-label">Escalations</span>
            <span className="analytics-card-value">{pct(b.escalation_rate as number | undefined)}</span>
            <span className="analytics-card-sub">{b.escalated ?? 0} asked for a human</span>
          </div>
          <div className="analytics-card">
            <span className="analytics-card-label">Cost per task</span>
            <span className="analytics-card-value">
              {latest.total_cost_usd != null && latest.tasks_attempted
                ? usd(latest.total_cost_usd / latest.tasks_attempted) : "—"}
            </span>
            <span className="analytics-card-sub">median {usd(b.cost_median as number | undefined)}</span>
          </div>
          <div className="analytics-card">
            <span className="analytics-card-label">Time per task</span>
            <span className="analytics-card-value">
              {latest.duration_s && latest.tasks_attempted ? minutes(latest.duration_s / latest.tasks_attempted) : "—"}
            </span>
            <span className="analytics-card-sub">wall-clock, checks and review included</span>
          </div>
        </div>
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
                <span className="evals-history-score">
                  {r.tasks_passed}/{r.tasks_attempted}
                </span>
                <span className="evals-bar evals-bar--mini" aria-hidden="true">
                  <span className="evals-bar-fill" style={{ width: `${r.pass_rate ?? 0}%` }} />
                </span>
                <span className="evals-history-cost">{usd(r.total_cost_usd)}</span>
                <span className="evals-history-notes">
                  {!r.full && <em className="evals-partial">partial</em>} {r.notes}
                </span>
              </button>
            ))}
          </div>
        </>
      )}

      {shown && (
        <>
          <h3 className="evals-subhead">
            Tasks — {shown.started_at?.replace("T", " ").slice(0, 16)}
            {baseline && (
              <span className="evals-compare">
                {" "}vs {day(baseline.started_at)}:{" "}
                {regressed.length === 0 && fixed.length === 0 ? "no change" : (
                  <>
                    {regressed.length > 0 && <span className="is-fail">{regressed.length} regressed</span>}
                    {regressed.length > 0 && fixed.length > 0 && ", "}
                    {fixed.length > 0 && <span className="is-pass">{fixed.length} fixed</span>}
                  </>
                )}
              </span>
            )}
          </h3>
          <div className="evals-tasks">
            {(report?.tasks ?? []).map((t) => {
              const open = openTask === t.id;
              const failedAsserts = t.assertions.filter((a) => !a.ok);
              return (
                <div key={t.id} className={`evals-task ${t.passed ? "is-pass" : "is-fail"}`}>
                  <button
                    type="button"
                    className="evals-task-row"
                    aria-expanded={open}
                    onClick={() => setOpenTask(open ? null : t.id)}
                  >
                    <span className="evals-task-mark">{t.passed ? "✓" : "✗"}</span>
                    <span className="evals-task-id">{t.id}</span>
                    {regressed.includes(t.id) && <span className="evals-badge is-fail">regressed</span>}
                    {fixed.includes(t.id) && <span className="evals-badge is-pass">fixed</span>}
                    <span className="evals-task-cat">{t.category}</span>
                    <span className="evals-task-meta">
                      {usd(t.cost_usd)} · {minutes(t.duration_s)} · {t.iterations} redo{t.iterations === 1 ? "" : "s"}
                    </span>
                  </button>
                  {open && (
                    <div className="evals-task-detail">
                      <div className="evals-task-line">
                        Outcome <strong>{t.outcome}</strong>
                        {t.review_verdict && <> · reviewer <strong>{t.review_verdict}</strong></>}
                        {t.escalation_reason && <> · {t.escalation_reason}</>}
                      </div>
                      {(t.passed ? t.assertions : failedAsserts).map((a, i) => (
                        <div key={i} className={`evals-assert ${a.ok ? "is-pass" : "is-fail"}`}>
                          {a.ok ? "✓" : "✗"} {a.describe}
                          {!a.ok && a.detail && <div className="evals-assert-detail">{a.detail}</div>}
                        </div>
                      ))}
                      {t.changed_paths.length > 0 && (
                        <div className="evals-task-line">Changed: {t.changed_paths.join(", ")}</div>
                      )}
                      {t.diff && <pre className="evals-diff">{t.diff}</pre>}
                    </div>
                  )}
                </div>
              );
            })}
            {!report && <p className="analytics-section-sub">Loading tasks…</p>}
          </div>
        </>
      )}
    </div>
  );
}
