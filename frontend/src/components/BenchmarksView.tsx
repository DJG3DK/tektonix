import { EvalsPanel } from "./EvalsPanel";
import { SwebenchPanel } from "./SwebenchPanel";
import "./AnalyticsView.css";

/**
 * Benchmarks: the two fixed suites the agent is measured against. They used
 * to sit at the bottom of Analytics, a page about spend and outcomes on real
 * work; a page of their own is where you go to start a run and read a score.
 *
 * The windowed outcome numbers (BenchmarkPanel) stay on Analytics: they are
 * about the operator's own tasks, not a fixed suite.
 */
export function BenchmarksView() {
  return (
    <div className="analytics-view benchmarks-view">
      <h1 className="analytics-title">Benchmarks</h1>
      <p className="analytics-section-sub benchmarks-intro">
        The golden suite is ours; SWE-bench Verified is the public benchmark, graded by its official harness.
      </p>
      <EvalsPanel />
      <SwebenchPanel />
    </div>
  );
}
