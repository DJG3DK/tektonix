/* Wording for the SWE-bench panel (components/SwebenchPanel.tsx), apart from
 * the component so it can be tested on its own.
 *
 * The rules are evals/SWEBENCH.md's: only a full run of all 500 tasks is "X%
 * on SWE-bench Verified". A sample is "N of a 50-task sample", never a bare
 * percentage, and a diagnostic run is never a score at all. */
import type { SwebenchRunSummary } from "./types";
import { day, minutes, usd } from "./evalsFormat";

export const KIND_LABEL: Record<SwebenchRunSummary["kind"], string> = {
  full: "full run",
  sample: "sample",
  selected: "selected tasks",
  diagnostic: "diagnostic",
};

/** The run the scorecard shows: the newest full run, else the newest run that is a score. */
export function headlineRun(runs: SwebenchRunSummary[]): SwebenchRunSummary | undefined {
  return runs.find((r) => r.kind === "full" && r.graded)
    ?? runs.find((r) => r.kind === "full")
    ?? runs.find((r) => r.kind !== "diagnostic");
}

/** "412/500", or while running "9/12 graded so far". */
export function scoreText(r: SwebenchRunSummary): string {
  if (r.graded) return `${r.resolved ?? 0}/${r.total}`;
  if (r.graded_count > 0) return `${r.resolved ?? 0}/${r.graded_count}`;
  return "—";
}

export function scoreLabel(r: SwebenchRunSummary): string {
  const pct = r.graded && r.total ? ` · ${((100 * (r.resolved ?? 0)) / r.total).toFixed(1)}%` : "";
  if (r.kind === "full") return `resolved${pct}`;
  if (!r.graded) return r.graded_count > 0 ? "resolved among the tasks graded so far" : "not graded yet";
  return `resolved in a ${r.total}-task ${r.kind === "sample" ? "sample" : "selection"} — not the published number`;
}

/** The first model of each role, "agent-coder -> deepseek/x" as "coder: x". */
export function modelList(models: Record<string, number>): string[] {
  const seen = new Map<string, string>();
  for (const key of Object.keys(models)) {
    const [alias, model] = key.split(" -> ");
    const role = (alias || "").replace(/^agent-/, "");
    if (!seen.has(role) && model && model !== "undefined") seen.set(role, model);
  }
  return [...seen].map(([role, model]) => `${role}: ${model}`);
}

/** The paragraph "Copy scorecard" puts on the clipboard. */
export function swebenchScorecard(r: SwebenchRunSummary, referenceFails: string[]): string {
  const models = modelList(r.models).join(", ");
  const resolved = r.resolved ?? 0;
  const head = r.kind === "full"
    ? `Tektonix resolves ${resolved} of the ${r.total} SWE-bench Verified tasks (${((100 * resolved) / r.total).toFixed(1)}%)`
    : `Tektonix resolved ${resolved} of a ${r.total}-task ${r.kind === "sample" ? "sample" : "selection"} of SWE-bench Verified`;
  const perTask = r.total_cost_usd != null && r.total ? `${usd(r.total_cost_usd / r.total)} per task` : "";
  return [
    `${head}, ${day(r.started_at)}, graded by the official SWE-bench harness.`,
    "One attempt per task, the official task images, no internet access.",
    [perTask, r.duration_s ? `${minutes(r.duration_s)} in total` : ""].filter(Boolean).join(", ") + ".",
    models ? `Models: ${models}.` : "",
    referenceFails.length
      ? `The official reference fix itself fails ${referenceFails.length} task${referenceFails.length === 1 ? "" : "s"} in these images (${referenceFails.join(", ")}).`
      : "",
  ].filter((s) => s && s !== ".").join(" ");
}
