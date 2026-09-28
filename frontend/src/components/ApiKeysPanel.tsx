import { useEffect, useState } from "react";
import { getEnvConfig, saveEnvConfig, restartServices, type EnvConfig, type EnvKey } from "../api";
import { Icon } from "./Icon";
import { useSettingsSave } from "./SettingsSaveBar";
import "./ApiKeysPanel.css";

/* Editing the credentials this deployment runs on.
 *
 * Two decisions shape the whole component:
 *
 * 1. A secret's real value is never fetched. The API returns a masked hint
 *    (last four characters) and whether it is set — enough to confirm WHICH key
 *    is installed without being enough to use it. So an empty field means "leave
 *    unchanged", never "clear this", and the placeholder says so.
 * 2. Restarts are offered, not performed. Restarting the router interrupts every
 *    in-flight model call, so saving a form must not do it as a side effect. The
 *    save reports which services need it and the operator decides when.
 */
export function ApiKeysPanel() {
  const [keys, setKeys] = useState<EnvKey[] | null>(null);
  const [managed, setManaged] = useState<Pick<EnvConfig, "managed_by" | "note"> | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [reveal, setReveal] = useState<Record<string, boolean>>({});
  const [restarting, setRestarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingRestart, setPendingRestart] = useState<string[]>([]);
  const [savedNote, setSavedNote] = useState<string | null>(null);

  async function load() {
    try {
      const cfg = await getEnvConfig();
      setKeys(cfg.keys);
      setManaged({ managed_by: cfg.managed_by, note: cfg.note });
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not load");
    }
  }
  useEffect(() => { void load(); }, []);

  const readOnly = managed?.managed_by === "compose";
  const dirty = readOnly ? [] : Object.entries(edits).filter(([, v]) => v.trim() !== "");

  // No Save button of its own: the page's single sticky bar collects pending
  // edits from every panel and commits them. Errors propagate so the bar
  // reports them next to the button that caused them.
  async function handleSave() {
    if (!dirty.length) return;
    setError(null); setSavedNote(null);
    const r = await saveEnvConfig(Object.fromEntries(dirty));
    setEdits({});
    setPendingRestart(r.restart_required);
    setSavedNote(`Updated ${r.updated.join(", ")}.`);
    await load();
  }

  useSettingsSave("api-keys", dirty.length, handleSave, () => setEdits({}));

  async function handleRestart() {
    setRestarting(true);
    try {
      await restartServices(pendingRestart);
      setPendingRestart([]);
      setSavedNote("Services restarted.");
    } catch {
      // Restarting the agent kills this very request — a dropped connection here
      // is the expected outcome, not a failure worth alarming about.
      setPendingRestart([]);
      setSavedNote("Restart issued. If the agent itself restarted, reload the page.");
    } finally {
      setRestarting(false);
    }
  }

  if (error && !keys) return <section className="settings-card"><h2>API keys</h2><div className="settings-error">{error}</div></section>;
  if (!keys) return <section className="settings-card"><h2>API keys</h2><p className="settings-hint">Loading…</p></section>;

  const groups = [...new Set(keys.map((k) => k.group))];
  // The backend's group tag becomes the card title -- one CARD per group
  // (2026-08-28: "this box needs to be broken up into sections"), not one
  // giant card with faint sub-headers.
  const GROUP_TITLES: Record<string, string> = {
    Models: "Model routing",
    Tracing: "Tracing (LangSmith)",
    Email: "Email (SMTP)",
  };

  return (
    <>
      {/* No section heading of its own: the settings page names the section
          now (SettingsPage's own nav), and printing "API keys & integrations"
          directly under a header that already says Environment / "API keys and
          integration settings" was the same sentence twice. */}
      {readOnly ? (
        <p className="settings-hint akey-compose-note" data-testid="env-compose-note">
          {managed?.note} Values are shown masked and cannot be changed from this page.
        </p>
      ) : (
        <p className="settings-hint">
          Stored in this deployment's <code>.env</code> files, never in the database and never sent to
          the browser. Existing values are shown masked — leave a field blank to keep it unchanged.
        </p>
      )}

      <div className="settings-stack akey-groups">
      {groups.map((g) => (
        <section key={g} className="settings-card akey-group">
          <h2>{GROUP_TITLES[g] ?? g}</h2>
          {keys.filter((k) => k.group === g).map((k) => (
            <div key={k.key} className="akey-row">
              <div className="akey-head">
                <label className="akey-label" htmlFor={`k-${k.key}`}>{k.label}</label>
                <span className={`akey-state ${k.is_set ? "is-set" : "is-unset"}`}>
                  {k.is_set ? "set" : "not set"}
                </span>
              </div>
              <code className="akey-name">{k.key}</code>
              <p className="akey-help">{k.help}</p>
              <div className="akey-input-row">
                <input
                  id={`k-${k.key}`}
                  className="akey-input"
                  type={k.secret && !reveal[k.key] ? "password" : "text"}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={k.is_set ? (readOnly ? k.display : `${k.display} — blank keeps it`) : "not set"}
                  value={edits[k.key] ?? ""}
                  disabled={readOnly}
                  onChange={(e) => setEdits((p) => ({ ...p, [k.key]: e.target.value }))}
                />
                {k.secret && (
                  <button
                    type="button"
                    className="btn btn--ghost btn--icon"
                    title={reveal[k.key] ? "Hide what you typed" : "Show what you typed"}
                    onClick={() => setReveal((p) => ({ ...p, [k.key]: !p[k.key] }))}
                  >
                    <Icon name={reveal[k.key] ? "x" : "search"} size={15} />
                  </button>
                )}
              </div>
            </div>
          ))}
        </section>
      ))}
      </div>

      {error && <div className="settings-error">{error}</div>}
      {savedNote && <div className="settings-ok">{savedNote}</div>}

      {pendingRestart.length > 0 && (
        <div className="akey-restart">
          <Icon name="alert" size={15} />
          <span>
            Takes effect after restarting: <strong>{pendingRestart.join(", ")}</strong>.
            Restarting the router interrupts any model call in flight.
          </span>
          <button className="btn btn--sm" disabled={restarting} onClick={handleRestart}>
            {restarting ? "Restarting…" : "Restart now"}
          </button>
        </div>
      )}
    </>
  );
}
