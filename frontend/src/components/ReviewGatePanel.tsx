import { useState } from "react";
import type { ReviewGateResult } from "../types";
import "./ReviewGatePanel.css";

/**
 * `minimized` (task is actively running again): collapse to a one-line strip
 * so an old NEEDS_FIXES verdict doesn't sit front-and-center below the live
 * chat after the operator resumes — it stays one click away instead.
 * Auto-collapses on the transition into minimized, re-expands automatically
 * when the task comes back to rest with a verdict worth reading.
 */
export function ReviewGatePanel({ result, minimized = false }: { result: ReviewGateResult; minimized?: boolean }) {
  const ready = result.verdict === "READY";
  const [open, setOpen] = useState(!minimized);
  // Follows the prop on the transition only, so a manual "show" while
  // minimized sticks. Adjusted during render, not in an effect.
  const [wasMinimized, setWasMinimized] = useState(minimized);
  if (wasMinimized !== minimized) {
    setWasMinimized(minimized);
    setOpen(!minimized);
  }

  if (!open) {
    return (
      <button className={`review-panel review-panel--collapsed ${ready ? "ready" : "needs-fixes"}`} onClick={() => setOpen(true)}>
        <span className="review-verdict">{ready ? "✓ READY" : "⚠ NEEDS FIXES"}</span>
        <span className="review-collapsed-summary">{result.summary}</span>
        <span className="review-collapsed-hint">show</span>
      </button>
    );
  }

  return (
    <div className={`review-panel ${ready ? "ready" : "needs-fixes"}`}>
      <div className="review-panel-header">
        <span className="review-verdict">{ready ? "✓ READY" : "⚠ NEEDS FIXES"}</span>
        <span className="review-source">review service</span>
        {minimized && (
          <button className="review-hide-btn" onClick={() => setOpen(false)}>hide</button>
        )}
      </div>
      <p className="review-summary">{result.summary}</p>
      {result.findings.length > 0 && (
        <ul className="review-findings">
          {result.findings.map((f, i) => (
            <li key={i} className={`finding-${f.severity}`}>
              <span className="finding-severity">{f.severity}</span>
              {f.file && <span className="finding-file">{f.file}</span>}
              <span className="finding-issue">{f.issue}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
