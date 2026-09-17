import { useEffect, useState } from "react";
import { getAuditLog, type AuditEntry } from "../api";

/**
 * Who moved a control, and when.
 *
 * Not a feed of everything that happened — tasks and model calls are recorded
 * elsewhere and in far more detail. This is the short list of decisions that
 * change what the system is allowed to do: who onboarded or created a project, who
 * approved a particular gated command, who turned auto mode on and for which
 * projects, who let a GitHub source create work on its own, who minted a key
 * that can push.
 *
 * It exists because the answer to "who did that?" used to be a Telegram
 * message in a chat that may have been cleared, if notifications were even on.
 */
export function AuditLogCard() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    getAuditLog(100)
      .then(setEntries)
      .catch((e) => setError(e instanceof Error ? e.message : "could not load the audit log"));
  }, []);

  const shown = expanded ? entries ?? [] : (entries ?? []).slice(0, 10);

  return (
    <section className="settings-card">
      {/* No <h2>: the settings page's own section header already says "Audit
          log" directly above this, and printing it twice in a row reads as a
          layout mistake (it was one). */}
      <p className="settings-body">
        Every change to a control that decides what the agent may do without asking. Kept in the
        same database as your tasks, so it survives restarts and rides along in the backup.
        Telegram alerts are a notification channel, not a record.
      </p>
      {error && <div className="settings-error">{error}</div>}
      {entries === null && !error && <p className="settings-body settings-body--dim">Loading…</p>}
      {entries !== null && entries.length === 0 && (
        <p className="settings-body settings-body--dim">
          Nothing recorded yet. Onboarding a project, approving a gated command, or changing auto
          mode will appear here.
        </p>
      )}
      {shown.length > 0 && (
        <table className="audit-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Who</th>
              <th>What</th>
              <th>Where</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((e) => (
              <tr key={`${e.ts}-${e.actor}-${e.action}`}>
                <td title={new Date(e.ts * 1000).toISOString()}>{ago(e.ts)}</td>
                <td>{e.actor}</td>
                <td>
                  {e.label}
                  {e.detail && <span className="audit-detail"> — {e.detail}</span>}
                </td>
                <td>{e.target ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {(entries?.length ?? 0) > 10 && (
        <button className="settings-btn" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Show the last 10" : `Show all ${entries?.length}`}
        </button>
      )}
    </section>
  );
}

function ago(ts: number): string {
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  if (seconds < 90) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
