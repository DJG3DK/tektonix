import type { BenchmarkWindow, Benchmarks } from "../types";
import "./BenchmarkPanel.css";

/**
 * "Did the last change make the agent better?"
 *
 * The rest of Analytics answers what happened. This answers whether it is
 * improving, which is a different question and needs a different shape: every
 * number is shown against the same-length window immediately before it, and
 * the delta is the thing worth reading.
 *
 * Its own file rather than another block inside AnalyticsView.tsx, which is
 * already 530 lines: this panel has real presentation logic (which direction
 * is good, when a delta is meaningless) that deserves to be unit-tested
 * without mounting the whole dashboard.
 */

/** Which way is up. A falling escalation rate is an improvement; a falling
 *  first-pass rate is not, and colouring both green because the arrow points
 *  the same way is exactly the bug this table exists to prevent. */
const LOWER_IS_BETTER = new Set<keyof BenchmarkWindow>([
  "escalation_rate",
  "iterations_median",
  "iterations_p90",
  "cost_median",
  "cost_p90",
  "cost_per_shipped_median",
]);

/** Metrics that are diagnostics, not scores. Memory section reads going up
 *  could mean the index got more useful or that the pinned set got worse;
 *  either way it is something to look into, not something to celebrate, so
 *  it renders without a colour. */
const NO_DIRECTION = new Set<keyof BenchmarkWindow>(["section_reads_per_prompt"]);

export type Tone = "good" | "bad" | "flat" | "none";

export function deltaTone(key: keyof BenchmarkWindow, delta: number | undefined): Tone {
  if (delta == null) return "none";
  if (NO_DIRECTION.has(key)) return "flat";
  if (delta === 0) return "flat";
  const better = LOWER_IS_BETTER.has(key) ? delta < 0 : delta > 0;
  return better ? "good" : "bad";
}

type Fmt = "pct" | "usd" | "num";

export function formatValue(value: number | null | undefined, fmt: Fmt): string {
  // An em dash, not "0". Null here means the window had nothing to divide by,
  // and a zero would read as a real measurement.
  if (value == null) return "—";
  if (fmt === "pct") return `${value.toFixed(0)}%`;
  if (fmt === "usd") return `$${value.toFixed(2)}`;
  return value.toFixed(value < 10 ? 1 : 0);
}

export function formatDelta(delta: number | undefined, fmt: Fmt): string {
  if (delta == null) return "";
  const sign = delta > 0 ? "+" : delta < 0 ? "−" : "±";
  const mag = Math.abs(delta);
  if (fmt === "pct") return `${sign}${mag.toFixed(0)} pts`;
  if (fmt === "usd") return `${sign}$${mag.toFixed(2)}`;
  return `${sign}${mag.toFixed(mag < 10 ? 1 : 0)}`;
}

/** Counts in the sub-line get separators; "15151 sections offered" is a
 *  number the eye has to stop and parse. */
const n = (v: number) => v.toLocaleString("en-US");

interface MetricSpec {
  key: keyof BenchmarkWindow;
  label: string;
  fmt: Fmt;
  sub: (w: BenchmarkWindow) => string;
}

/** The six that a change is actually judged by. Deliberately short: a panel
 *  with twenty numbers on it is one nobody reads, and the raw counts behind
 *  each of these are already on the rest of the page. */
const METRICS: MetricSpec[] = [
  {
    key: "first_pass_rate",
    label: "First-pass reviews",
    fmt: "pct",
    sub: (w) => `${n(w.first_pass)} of ${n(w.reviewed)} reviewed`,
  },
  {
    key: "iterations_median",
    label: "Fix cycles (median)",
    fmt: "num",
    sub: (w) => (w.iterations_p90 != null ? `p90 ${w.iterations_p90.toFixed(0)}` : "—"),
  },
  {
    key: "escalation_rate",
    label: "Escalations",
    fmt: "pct",
    sub: (w) => `${n(w.escalated)} of ${n(w.tasks)} tasks`,
  },
  {
    key: "cost_per_shipped_median",
    label: "Cost per shipped task",
    fmt: "usd",
    sub: (w) => `${n(w.shipped)} shipped`,
  },
  {
    key: "history_follow_rate",
    label: "History searches used",
    fmt: "pct",
    sub: (w) => `${n(w.history_used)} of ${n(w.history_queries)} searches`,
  },
  {
    key: "section_reads_per_prompt",
    label: "Memory reads / prompt",
    fmt: "num",
    sub: (w) => `${n(w.sections_offered)} sections offered`,
  },
];

export function BenchmarkPanel({ data, error }: { data: Benchmarks | null; error?: string | null }) {
  if (error) {
    // Said out loud rather than left spinning. "route-missing" has one cause
    // worth naming (see getBenchmarks): the running backend predates this
    // route, which is what a frontend deployed ahead of its backend looks
    // like -- a state this deployment reaches on purpose, because a restart
    // kills whatever task is in flight.
    return (
      <div className="analytics-section">
        <h2>Benchmarks</h2>
        <p className="bench-warning" role="note">
          {error === "route-missing" || error.includes("404")
            ? "Not available yet — the running backend predates this panel. It appears after the next restart."
            : `Couldn't load benchmarks: ${error}`}
        </p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="analytics-section">
        <h2>Benchmarks</h2>
        <p className="analytics-section-sub">Loading…</p>
      </div>
    );
  }

  const { current, previous, delta } = data;

  return (
    <div className="analytics-section">
      <h2>Benchmarks</h2>
      <p className="analytics-section-sub">
        Whether the agent is getting better, not just what it did. Each number covers the last{" "}
        {data.window_days} days, compared with the {data.window_days} days before that
        ({n(current.tasks)} vs {n(previous.tasks)} tasks).
      </p>

      {/* Said plainly rather than left to the reader. With a handful of tasks
          a moved percentage is noise, and a dashboard that draws a confident
          green arrow over three tasks is actively misleading. */}
      {data.sample_warning && (
        <p className="bench-warning" role="note">
          Small sample — {data.sample_warning}. Read the deltas as hints, not results.
        </p>
      )}

      <div className="analytics-cards">
        {METRICS.map((m) => {
          const value = current[m.key] as number | null;
          const d = delta[m.key];
          const tone = deltaTone(m.key, d);
          return (
            <div className="analytics-card" key={m.key}>
              <span className="analytics-card-label">{m.label}</span>
              <span className="analytics-card-value">{formatValue(value, m.fmt)}</span>
              <span className="analytics-card-sub">
                <span className={`bench-delta bench-delta-${tone}`}>
                  {tone === "none" ? "no prior window" : formatDelta(d, m.fmt)}
                </span>
                {" · "}
                {m.sub(current)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
