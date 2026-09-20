import { useEffect, useMemo, useState } from "react";
import { getGitHubSettings, listGitHubRepos, onboardFromGitHub, pollGitHubNow, saveGitHubSettings, testGitHubToken, type GitHubMode, type GitHubProjectSettings, type GitHubSettings, type GitHubSettingsPatch, type GitHubSettingsResponse, type GitHubSource, type GitHubRepo, type GitHubTokenProbe } from "../api";
import { useSettingsSave } from "./SettingsSaveBar";
import "./GitHubSettingsCard.css";

/* Settings -> GitHub.
 *
 * Three things live here, in the order an operator sets them up:
 *   1. tokens    -- fine-grained PATs, stored encrypted server-side. The
 *                   value is write-only: the card shows a name, the last four
 *                   characters and a Test button, never the token again.
 *   2. delivery  -- where approve links go (Telegram, email) and the public
 *                   URL those links are built on.
 *   3. policies  -- per project, per source: Off / Propose / Auto, with the
 *                   budget and caps that bound Auto.
 *
 * Edits report to the page's single save bar (SettingsSaveBar). Test and
 * Poll now act immediately: they read, they change nothing.
 */

const SOURCE_ORDER: GitHubSource[] = ["dependabot_prs", "security_alerts", "code_scanning", "review_requests", "ci_failures"];
const ENV_TOKEN = "__env__";

const MODE_HELP: Record<GitHubMode, string> = {
  off: "Ignore. The inbox still lists what was seen.",
  propose: "Put it in the inbox and send an approve link. Nothing starts until you say so.",
  auto: "Start the task at once, within this project's budget and cap. It still goes through the review gate and your merge approval.",
};

type Draft = Omit<GitHubSettings, "tokens">;

function draftOf(s: GitHubSettings, projects: string[]): Draft {
  const out: Draft = {
    poll_interval_min: s.poll_interval_min,
    public_url: s.public_url,
    notify: { ...s.notify },
    projects: {},
  };
  for (const name of projects) {
    const p = s.projects[name];
    out.projects[name] = p
      ? { ...p, policies: { ...p.policies } }
      : { token: null, policies: { dependabot_prs: "off", security_alerts: "off", code_scanning: "off", review_requests: "off", ci_failures: "off" }, budget_usd: 3, max_open_auto: 2, authors: "dependabot", route: "auto" };
  }
  return out;
}

function projectChanged(a: GitHubProjectSettings, b: GitHubProjectSettings): boolean {
  return a.token !== b.token || a.budget_usd !== b.budget_usd || a.max_open_auto !== b.max_open_auto
    || a.authors !== b.authors || a.route !== b.route
    || SOURCE_ORDER.some((s) => a.policies[s] !== b.policies[s]);
}

export function GitHubSettingsCard() {
  const [data, setData] = useState<GitHubSettingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [pendingTokens, setPendingTokens] = useState<Record<string, string>>({});
  const [removed, setRemoved] = useState<string[]>([]);
  /* {old: new}, staged like every other edit so one Save applies the lot. */
  const [renames, setRenames] = useState<Record<string, string>>({});
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [newToken, setNewToken] = useState("");
  const [probe, setProbe] = useState<{ name: string; result: GitHubTokenProbe | null; error: string | null; busy: boolean } | null>(null);
  /* What a token can actually reach. Asked for by name, answered by GitHub,
     so it reflects the grant rather than anything stored here. */
  const [repoList, setRepoList] = useState<{ name: string; repos: GitHubRepo[]; error: string | null; busy: boolean } | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [addMsg, setAddMsg] = useState<string | null>(null);
  const [pollMsg, setPollMsg] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getGitHubSettings()
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setDraft(draftOf(d.settings, d.projects));
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "failed to load GitHub settings"));
    return () => { cancelled = true; };
  }, []);

  const base = useMemo(() => (data ? draftOf(data.settings, data.projects) : null), [data]);

  const dirtyCount = useMemo(() => {
    if (!draft || !base) return 0;
    let n = Object.keys(pendingTokens).length + removed.length + Object.keys(renames).length;
    if (draft.poll_interval_min !== base.poll_interval_min) n++;
    if (draft.public_url !== base.public_url) n++;
    if (draft.notify.telegram !== base.notify.telegram || draft.notify.email !== base.notify.email || draft.notify.email_to !== base.notify.email_to) n++;
    for (const name of Object.keys(draft.projects)) if (projectChanged(draft.projects[name], base.projects[name])) n++;
    return n;
  }, [draft, base, pendingTokens, removed, renames]);

  async function save() {
    if (!draft || !base || !data) return;
    const patch: GitHubSettingsPatch = {};
    if (Object.keys(pendingTokens).length) patch.add_tokens = pendingTokens;
    if (removed.length) patch.remove_tokens = removed;
    if (Object.keys(renames).length) patch.rename_tokens = renames;
    if (draft.poll_interval_min !== base.poll_interval_min) patch.poll_interval_min = draft.poll_interval_min;
    if (draft.public_url !== base.public_url) patch.public_url = draft.public_url;
    if (draft.notify.telegram !== base.notify.telegram || draft.notify.email !== base.notify.email || draft.notify.email_to !== base.notify.email_to) patch.notify = draft.notify;
    const projects: NonNullable<GitHubSettingsPatch["projects"]> = {};
    for (const name of Object.keys(draft.projects)) {
      if (projectChanged(draft.projects[name], base.projects[name])) projects[name] = draft.projects[name];
    }
    if (Object.keys(projects).length) patch.projects = projects;
    // Errors propagate: the save bar reports them beside its own button.
    const res = await saveGitHubSettings(patch);
    const next = { ...data, settings: res.settings };
    setData(next);
    setDraft(draftOf(res.settings, next.projects));
    setPendingTokens({});
    setRemoved([]);
    setRenames({});
    setRenaming(null);
  }

  function discard() {
    if (!data) return;
    setDraft(draftOf(data.settings, data.projects));
    setPendingTokens({});
    setRemoved([]);
    setRenames({});
    setRenaming(null);
  }

  useSettingsSave("github", dirtyCount, save, discard);

  function addToken() {
    const name = newName.trim();
    const token = newToken.trim();
    if (!name || !token) return;
    setPendingTokens((p) => ({ ...p, [name]: token }));
    setRemoved((r) => r.filter((n) => n !== name));
    setNewName("");
    setNewToken("");
  }

  async function showRepos(name: string) {
    setRepoList({ name, repos: [], error: null, busy: true });
    setAddMsg(null);
    try {
      const arg = pendingTokens[name] ? { token: pendingTokens[name] } : { name };
      const res = await listGitHubRepos(arg);
      setRepoList({ name, repos: res.repos, error: null, busy: false });
    } catch (e) {
      setRepoList({ name, repos: [], error: e instanceof Error ? e.message : "failed", busy: false });
    }
  }

  async function addRepo(slug: string, tokenName: string, ship: "push" | "pr") {
    setAdding(slug);
    setAddMsg(null);
    try {
      const res = await onboardFromGitHub({ slug, token_name: tokenName, ship });
      setAddMsg(`Added ${res.name} — ${ship === "pr" ? "opens pull requests" : "merges and deploys"}.`);
      await showRepos(tokenName);   // re-read, so it moves to "already added"
    } catch (e) {
      setAddMsg(e instanceof Error ? e.message : "could not add it");
    } finally {
      setAdding(null);
    }
  }

  async function runTest(name: string) {
    setProbe({ name, result: null, error: null, busy: true });
    try {
      const arg = pendingTokens[name] ? { token: pendingTokens[name] } : { name };
      const result = await testGitHubToken(arg);
      setProbe({ name, result, error: result.ok ? null : (result.error ?? "token rejected"), busy: false });
    } catch (e) {
      setProbe({ name, result: null, error: e instanceof Error ? e.message : "test failed", busy: false });
    }
  }

  async function pollNow() {
    setPolling(true);
    setPollMsg(null);
    try {
      const res = await pollGitHubNow();
      const parts = res.results.map((r) => r.error ? `${r.repo}: ${r.error}` : r.skipped ? `${r.repo}: skipped (${r.skipped})`
        : `${r.repo}: ${r.found ?? 0} found, ${r.proposed ?? 0} proposed, ${r.created ?? 0} started`);
      setPollMsg(parts.length ? parts.join(" · ") : "No project has a source switched on yet.");
    } catch (e) {
      setPollMsg(e instanceof Error ? e.message : "poll failed");
    } finally {
      setPolling(false);
    }
  }

  function setProject(name: string, patch: Partial<GitHubProjectSettings>) {
    setDraft((d) => d && { ...d, projects: { ...d.projects, [name]: { ...d.projects[name], ...patch } } });
  }
  function setPolicy(name: string, source: GitHubSource, mode: GitHubMode) {
    setDraft((d) => d && { ...d, projects: { ...d.projects, [name]: { ...d.projects[name], policies: { ...d.projects[name].policies, [source]: mode } } } });
  }

  if (error && !data) {
    return <section className="settings-card wiz-card"><h2>GitHub</h2><div className="settings-error">{error}</div></section>;
  }
  if (!data || !draft) {
    return <section className="settings-card wiz-card"><h2>GitHub</h2><p className="settings-hint">Loading…</p></section>;
  }

  const storedNames = Object.keys(data.settings.tokens).filter((n) => !removed.includes(n));
  const tokenNames = [...storedNames, ...Object.keys(pendingTokens).filter((n) => !storedNames.includes(n))];
  const anyOn = Object.values(draft.projects).some((p) => SOURCE_ORDER.some((s) => p.policies[s] !== "off"));

  return (
    <section className={`settings-card wiz-card gh-card ${anyOn ? "settings-card--armed" : ""}`}>
      <h2>
        GitHub
        <span className={`settings-pill ${anyOn ? "settings-pill--on" : ""}`}>{anyOn ? "ON" : "OFF"}</span>
      </h2>
      <p className="settings-card-sub">
        Pick up Dependabot pull requests, security alerts, review comments and failing checks, and turn them
        into tasks — proposed to you first, or started automatically. Every task still passes the review gate and your merge approval.
      </p>

      {/* ---- tokens ---- */}
      <h3 className="gh-h3">Tokens</h3>
      <p className="settings-hint">
        Fine-grained personal access tokens, one per GitHub account or repo set. Repository permissions: Metadata, Pull requests and Contents (read);
        Dependabot alerts (read) for security alerts; Checks (read) or Actions (read) for failing checks. Stored encrypted; the value is never shown again.
        {data.env_token && " A GITHUB_TOKEN from the server's .env is the fallback for projects without one."}
      </p>
      <ul className="gh-tokens">
        {tokenNames.map((name) => {
          const pending = name in pendingTokens;
          const meta = data.settings.tokens[name];
          return (
            <li key={name} className="gh-token">
              {renaming === name ? (
                <input
                  className="gh-input gh-token-rename"
                  defaultValue={renames[name] ?? name}
                  autoFocus
                  aria-label={`rename ${name}`}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setRenaming(null);
                    if (e.key === "Enter") {
                      const next = (e.target as HTMLInputElement).value.trim();
                      if (next && next !== name) setRenames((r) => ({ ...r, [name]: next }));
                      else setRenames((r) => { const { [name]: _d, ...rest } = r; return rest; });
                      setRenaming(null);
                    }
                  }}
                  onBlur={(e) => {
                    const next = e.target.value.trim();
                    if (next && next !== name) setRenames((r) => ({ ...r, [name]: next }));
                    setRenaming(null);
                  }}
                />
              ) : (
                <span className="gh-token-name">
                  {renames[name] ?? name}
                  {renames[name] && <span className="gh-token-was"> (was {name})</span>}
                </span>
              )}
              <span className="gh-token-hint">
                {pending ? "unsaved" : `${meta?.hint ?? ""}${meta?.created_at ? ` · added ${new Date(meta.created_at * 1000).toLocaleDateString()}` : ""}`}
              </span>
              {/* Renaming is the common correction: the label here is the
                  operator's, and they rename tokens in GitHub as they work out
                  what each one is for. Remove-and-re-add meant pasting the
                  secret again and losing every project mapped to it. */}
              <button type="button" className="gh-btn" disabled={pending} onClick={() => setRenaming(name)}>
                Rename
              </button>
              <button type="button" className="gh-btn" disabled={probe?.busy} onClick={() => runTest(name)}>
                {probe?.name === name && probe.busy ? "Testing…" : "Test"}
              </button>
              <button
                type="button"
                className="gh-btn"
                disabled={pending || repoList?.busy}
                onClick={() => void showRepos(name)}
              >
                {repoList?.name === name && repoList.busy ? "Listing…" : "Repositories"}
              </button>
              <button type="button" className="gh-btn gh-btn--danger" onClick={() => {
                if (pending) setPendingTokens((p) => { const { [name]: _drop, ...rest } = p; return rest; });
                else setRemoved((r) => [...r, name]);
              }}>Remove</button>
            </li>
          );
        })}
        {tokenNames.length === 0 && <li className="settings-hint">No tokens yet.</li>}
      </ul>
      {probe && !probe.busy && (
        <div className={`gh-probe ${probe.error ? "gh-probe--bad" : ""}`}>
          {probe.error ? (
            <span>{probe.name}: {probe.error}</span>
          ) : (
            <>
              <span>{probe.name}: authenticated as <b>{probe.result?.login ?? "?"}</b>.{" "}
                {probe.result?.matched.length
                  ? `Reaches ${probe.result.matched.length} configured project${probe.result.matched.length === 1 ? "" : "s"}:`
                  : "Reaches none of the configured projects (check the token's repository access)."}
              </span>
              {probe.result?.matched.map((m) => (
                <span key={m.slug} className="gh-probe-repo">
                  {m.project} ({m.slug}) — {m.push ? "read/write" : "read-only"}
                  {m.dependabot_alerts === true ? ", alerts ok" : m.dependabot_alerts === false ? ", no alert permission" : ""}
                  {m.checks === true ? ", checks ok" : m.checks === false ? ", no checks permission" : ""}
                </span>
              ))}
              {probe.result?.warning && <span className="gh-probe-warn">{probe.result.warning}</span>}
            </>
          )}
        </div>
      )}
      {repoList && !repoList.busy && (
        <div className="gh-repos">
          {repoList.error ? (
            <p className="gh-probe gh-probe--bad">{repoList.name}: {repoList.error}</p>
          ) : (
            <>
              <p className="settings-hint">
                What <b>{repoList.name}</b> can reach, straight from GitHub. Change what a
                token may see in GitHub and this list follows. Adding one clones it here and
                onboards it.
              </p>
              {addMsg && <p className="gh-repos-msg">{addMsg}</p>}
              <ul className="gh-repo-list">
                {repoList.repos.map((r) => (
                  <li key={r.slug} className={`gh-repo ${r.onboarded_as ? "is-onboarded" : ""}`}>
                    <span className="gh-repo-slug">{r.slug}</span>
                    <span className="gh-repo-meta">
                      {r.private ? "private" : "public"}
                      {r.archived ? " · archived" : ""}
                      {r.push ? "" : " · read-only"}
                      {r.default_branch !== "main" ? ` · ${r.default_branch}` : ""}
                    </span>
                    {r.onboarded_as ? (
                      <span className="gh-repo-added">already added as {r.onboarded_as}</span>
                    ) : (
                      <span className="gh-repo-actions">
                        {/* Two buttons rather than a default: how work lands is
                            the operator's call, and burying it in a rule about
                            where the project came from made it invisible. */}
                        <button
                          type="button"
                          className="gh-btn"
                          disabled={!r.push || adding !== null || r.archived}
                          title={r.push ? "Opens a pull request; your base branch is never written"
                                        : "This token cannot write to that repository"}
                          onClick={() => void addRepo(r.slug, repoList.name, "pr")}
                        >
                          {adding === r.slug ? "Adding…" : "Add · pull requests"}
                        </button>
                        <button
                          type="button"
                          className="gh-btn"
                          disabled={!r.push || adding !== null || r.archived}
                          title="Merges into the base branch and deploys"
                          onClick={() => void addRepo(r.slug, repoList.name, "push")}
                        >
                          Add · merge
                        </button>
                      </span>
                    )}
                  </li>
                ))}
                {repoList.repos.length === 0 && (
                  <li className="settings-hint">This token reaches no repositories.</li>
                )}
              </ul>
            </>
          )}
        </div>
      )}
      <div className="gh-add">
        {/* "name (e.g. main)" read like a branch. It is a label of the
            operator's choosing, and saying so is the whole fix. */}
        <input className="gh-input" placeholder="label, e.g. DJG3dk-Projects" value={newName}
          onChange={(e) => setNewName(e.target.value)} aria-label="token name" />
        <input className="gh-input gh-input--wide" placeholder="paste the token — github_pat_…" type="password" autoComplete="off" value={newToken}
          onChange={(e) => setNewToken(e.target.value)} aria-label="token value" />
        <button type="button" className="gh-btn gh-btn--primary" disabled={!newName.trim() || !newToken.trim()} onClick={addToken}>Add token</button>
      </div>

      {/* ---- delivery ---- */}
      <h3 className="gh-h3">Approve links</h3>
      <div className="gh-grid2">
        <label className="field">
          <span>Dashboard URL for links</span>
          <input className="gh-input" placeholder="https://agent.example.com/v2" value={draft.public_url}
            onChange={(e) => setDraft({ ...draft, public_url: e.target.value })} />
        </label>
        <label className="field">
          <span>Poll every (minutes)</span>
          <input className="gh-input" type="number" min={2} max={1440} value={draft.poll_interval_min}
            onChange={(e) => setDraft({ ...draft, poll_interval_min: Number(e.target.value) || 10 })} />
        </label>
      </div>
      <div className="gh-checks">
        <label className="gh-check">
          <input type="checkbox" checked={draft.notify.telegram} onChange={(e) => setDraft({ ...draft, notify: { ...draft.notify, telegram: e.target.checked } })} />
          Telegram (everyone with Telegram set up)
        </label>
        <label className="gh-check">
          <input type="checkbox" checked={draft.notify.email} onChange={(e) => setDraft({ ...draft, notify: { ...draft.notify, email: e.target.checked } })} />
          Email
        </label>
        {draft.notify.email && (
          <input className="gh-input" placeholder="to (defaults to the admin email)" value={draft.notify.email_to}
            onChange={(e) => setDraft({ ...draft, notify: { ...draft.notify, email_to: e.target.value } })} aria-label="email recipient" />
        )}
      </div>
      <p className="settings-hint">
        Links open a confirmation page with one button; the page is public but the link is signed, single-use and expires after 48 hours.
        Without a dashboard URL, alerts say to open the inbox instead.
      </p>

      {/* ---- policies ---- */}
      <h3 className="gh-h3">Per project</h3>
      <div className="gh-table-wrap">
        <table className="gh-table">
          <thead>
            <tr>
              <th>Project</th>
              <th>Token</th>
              {SOURCE_ORDER.map((s) => <th key={s} title={data.sources[s].help}>{data.sources[s].label}</th>)}
              <th title="Budget for each task created from the inbox">Budget $</th>
              <th title="How many auto-started tasks may be open at once; past it, items are proposed instead">Max auto</th>
              <th title="Which PR authors count for the Dependabot source">Authors</th>
              <th title="Coder seat for tasks from this project">Route</th>
            </tr>
          </thead>
          <tbody>
            {data.projects.map((name) => {
              const p = draft.projects[name];
              const changed = projectChanged(p, base!.projects[name]);
              return (
                <tr key={name} className={changed ? "gh-row--dirty" : ""}>
                  <td className="gh-td-name">{name}</td>
                  <td>
                    <select className="gh-select" value={p.token ?? ENV_TOKEN} onChange={(e) => setProject(name, { token: e.target.value === ENV_TOKEN ? null : e.target.value })} aria-label={`${name} token`}>
                      <option value={ENV_TOKEN}>{data.env_token ? "server .env token" : "none"}</option>
                      {tokenNames.map((t) => <option key={t} value={t}>{t}</option>)}
                    </select>
                  </td>
                  {SOURCE_ORDER.map((s) => (
                    <td key={s}>
                      <select className={`gh-select gh-mode gh-mode--${p.policies[s]}`} value={p.policies[s]} title={MODE_HELP[p.policies[s]]}
                        onChange={(e) => setPolicy(name, s, e.target.value as GitHubMode)} aria-label={`${name} ${data.sources[s].label}`}>
                        {data.modes.map((m) => <option key={m} value={m}>{m === "off" ? "Off" : m === "propose" ? "Propose" : "Auto"}</option>)}
                      </select>
                    </td>
                  ))}
                  <td><input className="gh-input gh-input--num" type="number" min={0.25} step={0.25} value={p.budget_usd} onChange={(e) => setProject(name, { budget_usd: Number(e.target.value) || 0.25 })} aria-label={`${name} budget`} /></td>
                  <td><input className="gh-input gh-input--num" type="number" min={1} max={20} value={p.max_open_auto} onChange={(e) => setProject(name, { max_open_auto: Number(e.target.value) || 1 })} aria-label={`${name} max auto`} /></td>
                  <td>
                    <select className="gh-select" value={p.authors} onChange={(e) => setProject(name, { authors: e.target.value as GitHubProjectSettings["authors"] })} aria-label={`${name} authors`}>
                      <option value="dependabot">dependabot</option><option value="bots">any bot</option><option value="anyone">anyone</option>
                    </select>
                  </td>
                  <td>
                    <select className="gh-select" value={p.route} onChange={(e) => setProject(name, { route: e.target.value as GitHubProjectSettings["route"] })} aria-label={`${name} route`}>
                      <option value="auto">auto</option><option value="frontend">frontend</option><option value="general">general</option>
                    </select>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="settings-hint">
        <b>Propose</b> puts the item in the GitHub inbox and sends an approve link. <b>Auto</b> starts the task immediately, up to the cap.
      </p>
      <div className="settings-note settings-note--keep gh-invariant">
        <strong>A task from the inbox is never unattended:</strong> whoever started it, it ignores Auto mode
        and still prompts for gated actions, and it always requires your merge approval — even if you have
        turned both off for your own typed tasks. Nobody typed these goals, so the switches do not apply.
        <br />
        <strong>Auto needs a gate with something in it:</strong> a project whose review runs no checks
        (no tests, no lint, no build) cannot use Auto — saving is refused, and if a project's checks
        disappear later, its items are proposed instead of started. Propose always works.
      </div>
      <div className="settings-actions gh-actions">
        <button type="button" className="gh-btn" disabled={polling} onClick={pollNow}>{polling ? "Polling…" : "Poll now"}</button>
        {pollMsg && <span className="settings-hint gh-poll-msg">{pollMsg}</span>}
      </div>
    </section>
  );
}
