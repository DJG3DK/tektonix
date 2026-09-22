import type { TaskStatus } from "../types";
import "./StatusBadge.css";

const CONFIG: Record<TaskStatus | "connecting" | "stalled", { label: string; color: string; pulse?: boolean }> = {
  running: { label: "Running", color: "var(--blue)", pulse: true },
  // Waiting on another task on the same project. Dim and NOT pulsing: the
  // pulse is what says "something is happening", and the entire point of
  // this state is that nothing is. Until 2026-09-22 a queued task said
  // "Running" with a live pulse, which is the lie this replaces.
  queued: { label: "Queued", color: "var(--text-dim)" },
  connecting: { label: "Connecting", color: "var(--text-faint)", pulse: true },
  done: { label: "Done", color: "var(--green)" },
  escalated: { label: "Escalated", color: "var(--red)" },
  error: { label: "Error", color: "var(--red)" },
  // Store says "running" but the backend process that was driving this task
  // restarted — distinct from "escalated" (which the graph itself decided)
  // so it's clear this needs a Resume click, not that anything failed.
  stalled: { label: "Stalled", color: "var(--red)" },
  // Operator-initiated via the Stop button — distinct from "stalled"
  // (infra hiccup) and "escalated" (the graph itself gave up): this one
  // was a deliberate choice, so it gets a neutral color, not red.
  stopped: { label: "Stopped", color: "var(--text-faint)" },
  // Paused on a human-in-the-loop approval request (deep_agent.py's
  // INTERRUPT_ON) — amber, not red: nothing went wrong, the agent is
  // correctly waiting for a decision on a specific risky tool call.
  awaiting_approval: { label: "Needs approval", color: "var(--amber)", pulse: true },
  awaiting_merge: { label: "Review diff", color: "var(--amber)", pulse: true },
};

export function StatusBadge({ status }: { status: TaskStatus | "connecting" | "stalled" }) {
  const cfg = CONFIG[status] ?? CONFIG.running;
  return (
    <span className="status-badge" style={{ color: cfg.color, borderColor: cfg.color }}>
      <span className={`status-dot ${cfg.pulse ? "pulse" : ""}`} style={{ background: cfg.color }} />
      {cfg.label}
    </span>
  );
}
