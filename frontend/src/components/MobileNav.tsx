import { Icon, type IconName } from "./Icon";
import "./MobileNav.css";

/* Bottom tab bar — mobile only.
 *
 * Navigation previously lived exclusively in the sidebar, which on a phone IS
 * the list pane. So changing view meant: back out to the list, scroll, tap.
 * Three gestures to reach Analytics. A phone's reachable area is the bottom
 * third of the screen, and this puts every primary destination there.
 *
 * "Tasks" is not a view — it returns to the list pane, which is what the task
 * list actually is on mobile. That is why it takes `pane` as well as `view`.
 */
type NavView = "new-task" | "task" | "analytics" | "benchmarks" | "models" | "planning" | "users" | "settings" | "github";

interface Tab {
  key: string;
  label: string;
  icon: IconName;
  admin?: boolean;
  /** Highlighted when the current view is one of these, or the list is showing. */
  match: (view: NavView, pane: "list" | "main") => boolean;
  go: () => void;
}

/* This bar is the ONLY navigation on a phone: the sidebar's header buttons are
 * hidden there (Sidebar.css), because six full-width rows above the task list
 * left a third of the screen for the list itself. So every destination the
 * sidebar offers has a tab here, including Users, and "Plan" starts a
 * planning session -- the same plan-first primary action as the sidebar's
 * New Plan, not the raw task composer, which stays reachable from the
 * Building section header. */
export function MobileNav({
  view, pane, isAdmin, onTasks, onNewPlan, onAnalytics, onBenchmarks, onModels, onUsers, onSettings, onGitHub,
}: {
  view: NavView;
  pane: "list" | "main";
  isAdmin: boolean;
  onTasks: () => void;
  onNewPlan: () => void;
  onAnalytics: () => void;
  onBenchmarks: () => void;
  onModels: () => void;
  onUsers: () => void;
  onSettings: () => void;
  onGitHub: () => void;
}) {
  const tabs: Tab[] = [
    { key: "tasks", label: "Tasks", icon: "tasks",
      match: (_v, p) => p === "list", go: onTasks },
    { key: "plan", label: "Plan", icon: "plus",
      match: (v, p) => p === "main" && v === "planning", go: onNewPlan },
    { key: "analytics", label: "Stats", icon: "chart", admin: true,
      match: (v, p) => p === "main" && v === "analytics", go: onAnalytics },
    { key: "benchmarks", label: "Bench", icon: "check", admin: true,
      match: (v, p) => p === "main" && v === "benchmarks", go: onBenchmarks },
    { key: "models", label: "Models", icon: "cpu", admin: true,
      match: (v, p) => p === "main" && v === "models", go: onModels },
    { key: "users", label: "Users", icon: "users", admin: true,
      match: (v, p) => p === "main" && v === "users", go: onUsers },
    { key: "github", label: "GitHub", icon: "github",
      match: (v, p) => p === "main" && v === "github", go: onGitHub },
    { key: "settings", label: "Settings", icon: "settings",
      match: (v, p) => p === "main" && v === "settings", go: onSettings },
  ];

  const visible = tabs.filter((t) => !t.admin || isAdmin);

  return (
    <nav className="mnav" aria-label="Primary">
      {visible.map((t) => {
        const active = t.match(view, pane);
        return (
          <button
            key={t.key}
            className={`mnav-tab${active ? " is-active" : ""}`}
            onClick={t.go}
            aria-current={active ? "page" : undefined}
          >
            <span className="mnav-ico"><Icon name={t.icon} size={20} /></span>
            <span className="mnav-label">{t.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
