import type { ProvisionStep } from "../api";
import "./StepList.css";

/* The step-by-step outcome of a provisioning run, shared by the Settings ->
 * Projects wizard and the planner's "New project..." door. Every step is
 * listed, not just the failed one: "config failed" tells an operator nothing
 * on its own, whereas seeing that git init and the worktree already succeeded
 * is what says whether to retry or to clean up by hand first. */
export function StepList({ steps }: { steps: ProvisionStep[] }) {
  return (
    <ul className="wiz-steps">
      {steps.map((s) => (
        <li key={s.step} className={s.ok ? "ok" : "bad"}>
          <span>{s.ok ? "✓" : "✕"}</span>
          <strong>{s.step}</strong>
          <span className="wiz-step-detail">{s.detail}</span>
        </li>
      ))}
    </ul>
  );
}
