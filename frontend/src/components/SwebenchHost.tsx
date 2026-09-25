import type { SwebenchHost, SwebenchHostField } from "../types";
import { hostNum, hostPct, hostPeaks, hostSec } from "../swebenchFormat";

/**
 * "Host during the run": what the box and the model router did while a
 * SWE-bench run went, from the minute-by-minute samples the runner keeps
 * (agent/evals/host_metrics.py). One view of what used to take sysstat, the
 * kernel log and the router ledger to answer: "can we run ten at once?"
 *
 * Every figure is host-wide: shards of one run share the box, so a shard's
 * block shows the whole box, not its own share.
 */
export function SwebenchHostBlock({ host }: { host: SwebenchHost }) {
  if (host.series.length === 0) return null;
  const peaks = hostPeaks(host);
  return (
    <section className="swebench-host" aria-label="Host during the run">
      <h3 className="evals-subhead">Host during the run</h3>
      <dl className="swebench-host-peaks">
        {peaks.map((p) => (
          <div key={p.key} className="swebench-host-peak">
            <dt>{p.label}</dt>
            <dd>{p.value}</dd>
          </div>
        ))}
      </dl>
      <div className="swebench-host-charts">
        <Sparkline host={host} field="mem_pct" name="Memory used" format={hostPct} />
        <Sparkline host={host} field="router_inflight" name="Model calls in flight" format={(v) => hostNum(v)} />
        <Sparkline host={host} field="router_p90_s" name="p90 model-call latency" format={hostSec} />
        <Sparkline host={host} field="containers" name="Containers running" format={(v) => hostNum(v)} />
      </div>
      <p className="analytics-section-sub swebench-host-note">
        Sampled every {host.interval_s} s by the runner; every figure is host-wide (shards of one run share the box).
      </p>
    </section>
  );
}

const W = 240;
const H = 56;
const PAD = 6;

/** One field over the run: a 2px line from a zero baseline with a light
 *  wash under it, a hairline at the peak, and a ring-marked dot on the last
 *  sample. The last value and the peak are written beside it, so the numbers
 *  never depend on reading the picture. */
function Sparkline({ host, field, name, format }: {
  host: SwebenchHost;
  field: SwebenchHostField;
  name: string;
  format: (v: number | undefined) => string;
}) {
  const pts = host.series.flatMap((s) => (s[field] == null ? [] : [{ t: s.t, v: s[field] }]));
  const last = pts.length ? pts[pts.length - 1].v : undefined;
  const seriesMax = pts.reduce((m, p) => Math.max(m, p.v), 0);
  const peak = Math.max(host.peaks[field] ?? 0, seriesMax);
  const peakShown = pts.length || host.peaks[field] != null ? peak : undefined;
  const t0 = pts.length ? pts[0].t : 0;
  const span = pts.length ? Math.max(1, pts[pts.length - 1].t - t0) : 1;
  const top = peak > 0 ? peak : 1;
  const x = (t: number) => PAD + ((t - t0) / span) * (W - 2 * PAD);
  const y = (v: number) => H - PAD - (v / top) * (H - 2 * PAD);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const area = pts.length
    ? `${line} L${x(pts[pts.length - 1].t).toFixed(1)},${H - PAD} L${x(pts[0].t).toFixed(1)},${H - PAD} Z`
    : "";
  const end = pts.length ? pts[pts.length - 1] : null;
  return (
    <figure className="swebench-spark">
      <figcaption className="swebench-spark-head">
        <span className="swebench-spark-name">{name}</span>
        <span className="swebench-spark-vals">
          <span className="swebench-spark-now">{format(last)}</span>
          <span className="swebench-spark-peak">peak {format(peakShown)}</span>
        </span>
      </figcaption>
      <svg
        className="swebench-spark-svg"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`${name} over the run: now ${format(last)}, peak ${format(peakShown)}`}
        data-testid={`sparkline-${field}`}
      >
        {pts.length === 0 ? (
          <text x={W / 2} y={H / 2 + 4} textAnchor="middle" className="swebench-spark-empty">no samples</text>
        ) : (
          <>
            <line className="swebench-spark-grid" x1={PAD} x2={W - PAD} y1={H - PAD} y2={H - PAD} />
            <line className="swebench-spark-grid" x1={PAD} x2={W - PAD} y1={y(top)} y2={y(top)} />
            <path className="swebench-spark-area" d={area} />
            <path className="swebench-spark-line" d={line} />
            {end && <circle className="swebench-spark-dot" cx={x(end.t)} cy={y(end.v)} r={4} />}
          </>
        )}
      </svg>
    </figure>
  );
}
