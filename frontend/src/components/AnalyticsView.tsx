import { useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { getAgentModelUsage, getAnalytics, getBenchmarks, getRouterBalance, getToolReliability, getTraceSummary } from "../api";
import type { AgentModelUsage, Analytics, Benchmarks, RouterBalance, ToolReliability, TraceSummary } from "../types";
import { BenchmarkPanel } from "./BenchmarkPanel";
import { repoColor } from "../repoColor";
import "./AnalyticsView.css";
import { modelColor, shortModel } from "../format";

// Fixed order + color per agent/classify.py's own taxonomy -- a category
// never shifts color as the mix of tasks changes.
export const CATEGORY_COLORS: Record<string, string> = {
  "bug-fix": "#f85149",
  feature: "#3fb950",
  "ui-styling": "#a56ef5",
  performance: "#e3a72e",
  investigation: "#58a6ff",
  other: "#5c6472",
};
import { CATEGORY_ORDER, CATEGORY_LABELS } from "../categories";

const OUTCOME_COLORS: Record<string, string> = {
  done: "#3fb950",
  escalated: "#f85149",
  running: "#58a6ff",
  stopped: "#e3a72e",
  error: "#f85149",
  awaiting_approval: "#e3a72e",
};

function formatTokenCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

// No model names here, deliberately -- the actual model each role is
// currently pinned to changes from the Models tab, and the real
// per-model call breakdown already renders live right below this
// subtitle, so a hardcoded name here would just be a second, staler copy
// of that same fact.
// "general-purpose" deliberately excluded from this dashboard breakdown --
// it's a safety-net subagent definition (closes a real budget-enforcement
// hole deepagents would otherwise leave open), not something the coordinator
// is ever actually instructed to delegate to, so it would only ever render
// as a permanently-empty section here. The underlying subagent stays
// defined in deep_agent.py regardless of this display choice.
const ROLE_INFO: Record<string, { label: string; sub: string }> = {
  planner: { label: "Planner", sub: "new goals & every feedback round" },
  coder: { label: "Coder", sub: "every turn after the plan" },
  investigator: { label: "Investigator", sub: "research & investigation" },
  "test-writer": { label: "Test-writer", sub: "test coverage" },
  summarizer: { label: "Context summarizer", sub: "compresses long conversations" },
  classifier: { label: "Task classifier", sub: "one-shot goal categorization at task creation" },
  vision: { label: "Vision", sub: "describe_image tool calls, any agent" },
  "planning-chat": { label: "Planning chat", sub: "the Planning tab's research/design conversation" },
  cartographer: { label: "Cartographer", sub: "codebase maps (scheduled)" },
  consolidator: { label: "Consolidator", sub: "nightly memory consolidation" },
  reviewer: { label: "Commit reviewer", sub: "adversarial review before merge" },
  "demo-chat": { label: "Demo chat", sub: "the public portfolio bot" },
  consolidation: { label: "Consolidation (legacy tag)", sub: "" },
  background: { label: "Background / untracked", sub: "benchmarks, probes, direct calls — no pinned role" },
};
const ROLE_ORDER = ["planner", "coder", "investigator", "test-writer", "summarizer", "classifier", "vision", "planning-chat"];

/** Roles beyond the core eight render ONLY when they have calls — the core
 *  list documents the pin structure, the rest is elastically appended so a
 *  role the backend starts reporting (cartographer, background) can never be
 *  silently dropped the way agent-demo-chat once was from the models page. */
export function rolesToRender(models: { role: string }[]): string[] {
  const extra = [...new Set(models.map((m) => m.role))]
    .filter((r) => !ROLE_ORDER.includes(r))
    .sort();
  return [...ROLE_ORDER, ...extra];
}

const chartTooltipStyle = {
  background: "#181d26",
  border: "1px solid #262c37",
  borderRadius: 8,
  fontSize: 12,
  color: "#e8ecf1",
};

export function AnalyticsView() {
  const [data, setData] = useState<Analytics | null>(null);
  const [balance, setBalance] = useState<RouterBalance | null>(null);
  const [models, setModels] = useState<AgentModelUsage[]>([]);
  const [toolReliability, setToolReliability] = useState<ToolReliability | null>(null);
  const [traceSummary, setTraceSummary] = useState<TraceSummary | null>(null);
  const [benchmarks, setBenchmarks] = useState<Benchmarks | null>(null);
  const [benchmarksError, setBenchmarksError] = useState<string | null>(null);

  useEffect(() => {
    getAnalytics().then(setData).catch(() => {});
    getRouterBalance().then(setBalance).catch(() => {});
    getTraceSummary().then(setTraceSummary).catch(() => {});
    // No retry loop around this one: unlike the model/tool panels it is
    // computed from the store on every call, so an empty answer means there
    // genuinely are no episodes yet, and asking again would say the same.
    getBenchmarks().then(setBenchmarks).catch((e) => setBenchmarksError(String(e?.message ?? e)));
    // Retry a few times. The warming LangSmith cache this guarded is gone --
    // these read local logs now and answer on the first call -- but an empty
    // first answer is still possible while a fresh deployment has no history
    // yet, and a silent give-up would make the section vanish rather than
    // fill in.
    const timers: ReturnType<typeof setTimeout>[] = [];
    function retryUntilNonEmpty<T>(load: () => Promise<T>, isEmpty: (v: T) => boolean, onData: (v: T) => void) {
      let attempts = 0;
      const attempt = () => {
        load()
          .then((v) => {
            if (!isEmpty(v)) onData(v);
            else if (++attempts < 4) timers.push(setTimeout(attempt, 8000));
          })
          .catch(() => {
            if (++attempts < 4) timers.push(setTimeout(attempt, 8000));
          });
      };
      attempt();
    }
    retryUntilNonEmpty(getAgentModelUsage, (r) => r.models.length === 0, (r) => setModels(r.models));
    retryUntilNonEmpty(getToolReliability, (r) => r.tools.length === 0, setToolReliability);
    return () => timers.forEach(clearTimeout);
  }, []);

  const outcomes = data
    ? Object.entries(data.outcomes).map(([name, value]) => ({ name, value }))
    : [];
  const repoRows = data
    ? Object.entries(data.per_repo).map(([repo, v]) => ({ repo, ...v }))
    : [];
  // Fixed category order, zero-filled -- a category with no tasks yet still
  // renders (at zero) so the taxonomy itself is always visible, the same
  // "every pinned role renders, used or not" reasoning the model-usage
  // section below already uses.
  const categoryRows = CATEGORY_ORDER.map((category) => {
    const row = data?.by_category.find((c) => c.category === category);
    return { category, tasks: row?.tasks ?? 0, cost: row?.cost ?? 0 };
  });
  const allTools = toolReliability?.tools ?? [];
  const toolRows = [...allTools].sort((a, b) => b.errors - a.errors).slice(0, 10);
  const totalToolCalls = allTools.reduce((s, t) => s + t.calls, 0);
  const totalToolErrors = allTools.reduce((s, t) => s + t.errors, 0);
  // Lifted out of the JSX: `data` is `Analytics | null` with inline guards and
  // no early return, so reaching through it inside a nested callback cannot be
  // narrowed. One local keeps the section below free of `!` assertions.
  const reviewer = data?.reviewer;
  const agentCost = data?.total_cost ?? 0;

  const avgIterations = data && data.episodes.length
    ? data.episodes.reduce((s, e) => s + (e.iterations ?? 0), 0) / data.episodes.length
    : null;

  return (
    <div className="analytics-view">
      <h1 className="analytics-title">Analytics</h1>

      <div className="analytics-cards">
        <div className="analytics-card">
          <span className="analytics-card-label">Agent spend</span>
          <span className="analytics-card-value">{data ? `$${data.total_cost.toFixed(2)}` : "—"}</span>
          {data && <span className="analytics-card-sub">last 14 days, incl. deleted tasks</span>}
        </div>
        <div className="analytics-card">
          <span className="analytics-card-label">API balance</span>
          <span className="analytics-card-value">{balance ? `$${balance.remaining.toFixed(2)}` : "—"}</span>
          {balance && <span className="analytics-card-sub">of ${balance.totalCredits.toFixed(0)} credits</span>}
        </div>
        <div className="analytics-card">
          <span className="analytics-card-label">Avg fix cycles / task</span>
          <span className="analytics-card-value">{avgIterations != null ? avgIterations.toFixed(1) : "—"}</span>
          {data && <span className="analytics-card-sub">from {data.episodes.length} recorded outcomes</span>}
        </div>
        <div className="analytics-card">
          <span className="analytics-card-label">Outcomes</span>
          <div className="outcome-pills">
            {outcomes.map((o) => (
              <span key={o.name} className="outcome-pill" style={{ color: OUTCOME_COLORS[o.name] ?? "var(--text-dim)" }}>
                {o.value} {o.name}
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Directly under the headline cards, and above everything else: this
          is the section somebody opens the page FOR after changing a prompt
          or a model pin. Everything below it is the detail behind it. */}
      <BenchmarkPanel data={benchmarks} error={benchmarksError} />

      {/* Reviewer spend. Its own section, not folded into the totals above:
          the agent's budget and the gate's budget are different things, and
          merging them would change what every existing number here means.
          This was invisible until 2026-08-25 — the reviewer called OpenRouter
          directly and never read the response's usage block. */}
      {reviewer && reviewer.reviews > 0 && (
        <div className="analytics-section">
          <h2>Commit reviewer</h2>
          <p className="analytics-section-sub">
            Spend by the review gate itself — separate from the agent's task budget above.
            {reviewer.model ? ` Currently ${reviewer.model}.` : ""}
            {!reviewer.cost_known && reviewer.reviews_missing_cost
              ? ` ${reviewer.reviews_missing_cost} review(s) reported no cost, so this total is a floor.`
              : ""}
          </p>
          <div className="analytics-cards">
            <div className="analytics-card">
              <div className="analytics-card-label">Review spend</div>
              <div className="analytics-card-value">
                {reviewer.cost_known ? "" : "≥ "}${reviewer.cost.toFixed(2)}
              </div>
              <div className="analytics-card-sub">{reviewer.reviews} reviews</div>
            </div>
            <div className="analytics-card">
              <div className="analytics-card-label">Per review</div>
              <div className="analytics-card-value">
                ${(reviewer.cost / Math.max(1, reviewer.reviews)).toFixed(3)}
              </div>
              <div className="analytics-card-sub">average</div>
            </div>
            <div className="analytics-card">
              <div className="analytics-card-label">Tokens in</div>
              <div className="analytics-card-value">{(reviewer.tokens_in / 1000).toFixed(0)}k</div>
              <div className="analytics-card-sub">diff + gathered context</div>
            </div>
            <div className="analytics-card">
              <div className="analytics-card-label">Share of spend</div>
              <div className="analytics-card-value">
                {agentCost + reviewer.cost > 0
                  ? ((reviewer.cost / (agentCost + reviewer.cost)) * 100).toFixed(0)
                  : "0"}%
              </div>
              <div className="analytics-card-sub">of agent + review</div>
            </div>
          </div>

          {reviewer.per_repo.length > 0 && (
            <div className="model-stats-list">
              {reviewer.per_repo.map((r) => {
                const max = Math.max(...reviewer.per_repo.map((x) => x.cost), 0.0001);
                return (
                  <div key={r.repo} className="model-stats-row">
                    <span className="model-stats-label">{r.repo}</span>
                    <div className="model-stats-bar-track">
                      <div
                        className="model-stats-bar-fill"
                        style={{ width: `${(r.cost / max) * 100}%`, background: "var(--amber)" }}
                      />
                    </div>
                    <span className="model-stats-cost">${r.cost.toFixed(3)}</span>
                    <span className="model-stats-requests">{r.reviews} reviews</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      <div className="analytics-section">
        <h2>Runs</h2>
        <p className="analytics-section-sub">
          From this box's own router ledger — one row per task that spent money, last 14 days. No tracing required.
        </p>
        <div className="analytics-cards">
          <div className="analytics-card">
            <span className="analytics-card-label">Tasks</span>
            <span className="analytics-card-value">{traceSummary ? traceSummary.trace_count : "—"}</span>
          </div>
          <div className="analytics-card">
            <span className="analytics-card-label">Avg task wall-clock</span>
            <span className="analytics-card-value">
              {traceSummary?.avg_latency_s != null ? `${traceSummary.avg_latency_s.toFixed(1)}s` : "—"}
            </span>
          </div>
          <div className="analytics-card">
            <span className="analytics-card-label">Model call error rate</span>
            <span className="analytics-card-value">{traceSummary ? `${(traceSummary.error_rate * 100).toFixed(1)}%` : "—"}</span>
          </div>
          <div className="analytics-card">
            <span className="analytics-card-label">Input tokens</span>
            <span className="analytics-card-value">{traceSummary ? formatTokenCount(traceSummary.total_input_tokens) : "—"}</span>
          </div>
          <div className="analytics-card">
            <span className="analytics-card-label">Output tokens</span>
            <span className="analytics-card-value">{traceSummary ? formatTokenCount(traceSummary.total_output_tokens) : "—"}</span>
          </div>
        </div>
      </div>

      <div className="analytics-grid">
        <section className="analytics-panel analytics-panel--wide">
          <h2>Daily spend</h2>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={data?.daily ?? []} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
              <defs>
                <linearGradient id="spendFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#8b7cf6" stopOpacity={0.45} />
                  <stop offset="100%" stopColor="#8b7cf6" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#1c212b" vertical={false} />
              <XAxis dataKey="date" stroke="#5c6472" fontSize={11} tickLine={false} axisLine={false}
                tickFormatter={(d: string) => d.slice(5)} />
              <YAxis stroke="#5c6472" fontSize={11} tickLine={false} axisLine={false}
                tickFormatter={(v: number) => `$${v}`} />
              <Tooltip contentStyle={chartTooltipStyle}
                formatter={(v: unknown, key: unknown) => (key === "cost" ? [`$${Number(v).toFixed(3)}`, "spend"] : [String(v), String(key)])} />
              <Area type="monotone" dataKey="cost" stroke="#8b7cf6" strokeWidth={2} fill="url(#spendFill)" dot={{ r: 3, fill: "#8b7cf6", strokeWidth: 0 }} activeDot={{ r: 5 }} />
            </AreaChart>
          </ResponsiveContainer>
        </section>

        <section className="analytics-panel">
          <h2>Outcomes</h2>
          <ResponsiveContainer width="100%" height={220}>
            <PieChart>
              <Pie data={outcomes} dataKey="value" nameKey="name" innerRadius={52} outerRadius={80}
                paddingAngle={3} strokeWidth={0}>
                {outcomes.map((o) => (
                  <Cell key={o.name} fill={OUTCOME_COLORS[o.name] ?? "#5c6472"} />
                ))}
              </Pie>
              <Tooltip contentStyle={chartTooltipStyle} />
            </PieChart>
          </ResponsiveContainer>
          <div className="donut-legend">
            {outcomes.map((o) => (
              <span key={o.name} className="donut-legend-item">
                <i style={{ background: OUTCOME_COLORS[o.name] ?? "#5c6472" }} /> {o.name}
              </span>
            ))}
          </div>
        </section>

        <section className="analytics-panel analytics-panel--wide">
          <h2>Cost by category <span className="analytics-h2-sub">(what kind of work is costing the most)</span></h2>
          <ResponsiveContainer width="100%" height={230}>
            <BarChart data={categoryRows} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
              <CartesianGrid stroke="#1c212b" vertical={false} />
              <XAxis dataKey="category" stroke="#5c6472" fontSize={11} tickLine={false} axisLine={false}
                tickFormatter={(c: string) => CATEGORY_LABELS[c] ?? c} />
              <YAxis stroke="#5c6472" fontSize={11} tickLine={false} axisLine={false}
                tickFormatter={(v: number) => `$${v}`} />
              <Tooltip contentStyle={chartTooltipStyle}
                formatter={(v: unknown, key: unknown) => (key === "cost" ? [`$${Number(v).toFixed(3)}`, "cost"] : [String(v), "tasks"])}
                labelFormatter={(c: unknown) => CATEGORY_LABELS[String(c)] ?? String(c)} />
              <Bar dataKey="cost" radius={[4, 4, 0, 0]}>
                {categoryRows.map((c) => (
                  <Cell key={c.category} fill={CATEGORY_COLORS[c.category] ?? "#5c6472"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <div className="donut-legend">
            {categoryRows.map((c) => (
              <span key={c.category} className="donut-legend-item">
                <i style={{ background: CATEGORY_COLORS[c.category] ?? "#5c6472" }} /> {CATEGORY_LABELS[c.category] ?? c.category} ({c.tasks})
              </span>
            ))}
          </div>
        </section>

        <section className="analytics-panel">
          <h2>Spend by repo</h2>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={repoRows} layout="vertical" margin={{ top: 8, right: 24, bottom: 0, left: 8 }}>
              <CartesianGrid stroke="#1c212b" horizontal={false} />
              <XAxis type="number" stroke="#5c6472" fontSize={11} tickLine={false} axisLine={false}
                tickFormatter={(v: number) => `$${v}`} />
              <YAxis type="category" dataKey="repo" stroke="#8b93a1" fontSize={11.5} tickLine={false} axisLine={false} width={104} />
              <Tooltip contentStyle={chartTooltipStyle}
                formatter={(v: unknown) => [`$${Number(v).toFixed(3)}`, "spend"]} />
              <Bar dataKey="cost" radius={[0, 4, 4, 0]} barSize={18}>
                {repoRows.map((r) => (
                  <Cell key={r.repo} fill={repoColor(r.repo)} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </section>
      </div>

      {models.length > 0 && (
        <section className="analytics-panel analytics-panel--full">
          <h2>Model usage by role <span className="analytics-h2-sub">(from the router's own per-call ledger — last 14 days)</span></h2>
          {rolesToRender(models).map((role) => {
            // Every pinned role renders, used or not — the section documents
            // the pin structure itself, not just whatever happened to run.
            const allRoleModels = models.filter((m) => m.role === role).sort((a, b) => b.calls - a.calls);
              // One-call strays (a probe, a fallback firing once) turned the
              // panel into a wall of hairline bars. Fold them into one
              // "other" line instead of hiding them — totals stay truthful.
              const strays = allRoleModels.filter((m) => m.calls <= 2 && allRoleModels.length > 3);
              const roleModels = allRoleModels.filter((m) => !strays.includes(m));
              if (strays.length) {
                roleModels.push({
                  ...strays[0],
                  model: `other (${strays.length} models)`,
                  calls: strays.reduce((s2, m) => s2 + m.calls, 0),
                  tokens_in: strays.reduce((s2, m) => s2 + m.tokens_in, 0),
                  tokens_out: strays.reduce((s2, m) => s2 + m.tokens_out, 0),
                });
              }
            const roleTotal = roleModels.reduce((sum, m) => sum + m.calls, 0);
            const roleMax = Math.max(1, ...roleModels.map((m) => m.calls));
            const info = ROLE_INFO[role] ?? { label: role, sub: "" };
            return (
              <div key={role} className="role-group">
                <div className="role-group-head">
                  <span className="role-group-label">{info.label}</span>
                  <span className="role-group-sub">{info.sub}</span>
                  <span className="role-group-total">{roleTotal} calls</span>
                </div>
                <div className="model-stats-list">
                  {roleModels.length === 0 && (
                    <div className="role-group-empty">no calls yet since pinning</div>
                  )}
                  {roleModels.map((m) => (
                    <div key={m.model} className="model-stats-row">
                      <span className="model-stats-label" style={{ color: modelColor(m.model) }} title={m.model}>
                        {shortModel(m.model)}
                      </span>
                      <div className="model-stats-bar-track">
                        <div
                          className="model-stats-bar-fill"
                          style={{ width: `${(m.calls / roleMax) * 100}%`, background: modelColor(m.model) }}
                        />
                      </div>
                      <span className="model-stats-cost">{m.calls} calls</span>
                      <span className="model-stats-requests">{((m.tokens_in + m.tokens_out) / 1000).toFixed(0)}k tok</span>
                      <span className="model-stats-latency">{m.avg_latency_s != null ? `${m.avg_latency_s.toFixed(1)}s avg` : "—"}</span>
                      {/* Two columns the traces never had: what the router was
                          billed, and how much of the prompt the provider
                          served from cache. A role sitting at 0% on a long
                          conversation is money being spent twice. */}
                      <span className="model-stats-billed">
                        {m.cost_usd != null ? `$${m.cost_usd.toFixed(2)}` : "—"}
                      </span>
                      <span className="model-stats-cache" title="share of prompt tokens served from the provider's cache">
                        {m.cache_hit_rate != null ? `${Math.round(m.cache_hit_rate * 100)}% cached` : "—"}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </section>
      )}

      {toolReliability && toolReliability.tools.length > 0 && (
        <section className="analytics-panel analytics-panel--full">
          <h2>
            Tool call reliability{" "}
            <span className="analytics-h2-sub">
              {totalToolErrors} error{totalToolErrors === 1 ? "" : "s"} in {totalToolCalls} calls
              {totalToolCalls > 0 ? ` (${((totalToolErrors / totalToolCalls) * 100).toFixed(1)}%)` : ""} — last 14 days
            </span>
          </h2>
          <div className="tool-reliability-grid">
            <ResponsiveContainer width="100%" height={Math.max(180, toolRows.length * 32)}>
              <BarChart data={toolRows} layout="vertical" margin={{ top: 8, right: 24, bottom: 0, left: 8 }}>
                <CartesianGrid stroke="#1c212b" horizontal={false} />
                <XAxis type="number" stroke="#5c6472" fontSize={11} tickLine={false} axisLine={false} allowDecimals={false} />
                <YAxis type="category" dataKey="tool" stroke="#8b93a1" fontSize={11.5} tickLine={false} axisLine={false} width={90} />
                <Tooltip contentStyle={chartTooltipStyle}
                  formatter={(v: unknown, _key: unknown, item) => {
                    const row = item?.payload as { calls: number; error_rate: number } | undefined;
                    return [`${v} of ${row?.calls ?? "?"} calls (${row ? (row.error_rate * 100).toFixed(1) : "?"}%)`, "errors"];
                  }} />
                <Bar dataKey="errors" radius={[0, 4, 4, 0]} barSize={16} fill="#f85149" />
              </BarChart>
            </ResponsiveContainer>
            <ResponsiveContainer width="100%" height={Math.max(180, toolRows.length * 32)}>
              <AreaChart data={toolReliability.daily} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                <defs>
                  <linearGradient id="errorFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#f85149" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="#f85149" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#1c212b" vertical={false} />
                <XAxis dataKey="date" stroke="#5c6472" fontSize={11} tickLine={false} axisLine={false}
                  tickFormatter={(d: string) => d.slice(5)} />
                <YAxis stroke="#5c6472" fontSize={11} tickLine={false} axisLine={false} allowDecimals={false} />
                <Tooltip contentStyle={chartTooltipStyle}
                  formatter={(v: unknown) => [String(v), "errors"]} />
                <Area type="monotone" dataKey="errors" stroke="#f85149" strokeWidth={2} fill="url(#errorFill)"
                  dot={{ r: 3, fill: "#f85149", strokeWidth: 0 }} activeDot={{ r: 5 }} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
          {(toolReliability.nudges?.length ?? 0) > 0 && (
            /* Shell calls the harness pointed at a cheaper tool -- reading or
             * editing a file through bash (a container each time) instead of
             * read/edit, or reaching for the agent's own memory from inside a
             * sandbox that cannot see it. Not errors: the call ran. */
            <p className="tool-nudges">
              {toolReliability.nudges!.reduce((n, x) => n + x.count, 0)} shell call
              {toolReliability.nudges!.reduce((n, x) => n + x.count, 0) === 1 ? "" : "s"} nudged
              toward a cheaper tool —{" "}
              {toolReliability.nudges!.map((n) => `${n.kind} ${n.count}`).join(", ")}
            </p>
          )}
        </section>
      )}
    </div>
  );
}
