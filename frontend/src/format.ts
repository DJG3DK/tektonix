/* Small display formatters shared by the views. One copy each: Sidebar,
 * ChatMessage and AnalyticsView used to carry their own, and the copies had
 * already drifted (one relativeTime stopped at hours, one shortModel carried
 * a no-op replace). */

/** "42s" / "7m" / "3h" / "2d" since `ts`: epoch SECONDS (the store's
 * created_at/updated_at) or an ISO string (a log entry's timestamp). */
export function relativeTime(ts: number | string): string {
  const then = typeof ts === "number" ? ts * 1000 : new Date(ts).getTime();
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

/** Family-based color so different pool models are visually distinct at a
 * glance (the router picks per call -- one alias, many real models). */
const MODEL_FAMILY_COLORS: [RegExp, string][] = [
  [/claude|anthropic/i, "#d97757"],
  [/gpt|openai|codex/i, "#74c99a"],
  [/deepseek/i, "#5d8bf4"],
  [/qwen/i, "#a56ef5"],
  [/glm|z-ai/i, "#3fb9a5"],
  [/grok|x-ai/i, "#c9cdd6"],
  [/kimi|moonshot/i, "#e3702e"],
  [/nova|amazon/i, "#e3a72e"],
  [/gemini|google/i, "#6ea8f5"],
];

export function modelColor(model: string): string {
  for (const [re, color] of MODEL_FAMILY_COLORS) if (re.test(model)) return color;
  return "#8b93a1";
}

/** "deepseek/deepseek-v4-pro-0813" -> "deepseek-v4-pro" (short, no provider
 * prefix, no date suffix -- a badge has to stay compact). The backend
 * (agent/nodes/work.py) already resolves a pinned role's bare alias
 * ("agent-investigator") to the real underlying model, reading current pins
 * from config.yaml each time, so this only ever shortens an already-real id. */
export function shortModel(model: string): string {
  return (model.split("/").pop() ?? model).replace(/-\d{4,8}$/, "");
}
