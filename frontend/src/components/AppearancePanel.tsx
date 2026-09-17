import { useEffect, useState } from "react";
import { setTheme as saveTheme } from "../api";
import { applyTheme, isThemeId, THEMES, type ThemeId } from "../themes";
import type { CurrentUser } from "../types";
import "./AppearancePanel.css";

/* Pick a colour scheme, see it, then commit it.
 *
 * The preview is the real thing rather than a picture of it: the pane carries
 * `data-theme`, and every [data-theme] block in theme.css is scoped by
 * attribute, so the custom properties inside it resolve to the chosen scheme
 * while the rest of the page stays on the saved one. A mock built from
 * hard-coded hexes would drift from the stylesheet the first time a token
 * changed, and drift silently -- it would still look like a preview.
 *
 * Nothing is applied to the app until Save. Choosing a scheme and watching the
 * whole console repaint under you, twice, while you compare, is worse than a
 * small honest sample -- and there would be nothing to cancel back to.
 */

/** A miniature of the console: rail, a card, a couple of status chips, a
 *  primary button. Enough surfaces that a scheme's ground/accent relationship
 *  is visible, small enough to sit under the picker. */
function Preview({ theme }: { theme: ThemeId }) {
  return (
    <div
      className="appearance-preview"
      // Drafting lives on :root, so previewing it means no attribute at all --
      // exactly what applyTheme does to the document.
      data-theme={theme === "drafting" ? undefined : theme}
      aria-label="Preview"
    >
      <div className="apv-rail">
        <div className="apv-brand" />
        <div className="apv-primary">New Plan</div>
        <div className="apv-navitem apv-navitem--on">Analytics</div>
        <div className="apv-navitem">Models</div>
        <div className="apv-navitem">Settings</div>
      </div>
      <div className="apv-main">
        <div className="apv-card">
          <div className="apv-row">
            <span className="apv-title">Backtest pair grouping</span>
            <span className="apv-badge apv-badge--run">running</span>
          </div>
          <div className="apv-meta">webapp · 7/12 steps · $1.53</div>
          <div className="apv-bar"><i style={{ width: "58%" }} /></div>
        </div>
        <div className="apv-card">
          <div className="apv-row">
            <span className="apv-title">Health endpoints</span>
            <span className="apv-badge apv-badge--done">done</span>
          </div>
          <div className="apv-meta">storefront · merged</div>
        </div>
        <div className="apv-actions">
          <span className="apv-btn">Approve</span>
          <span className="apv-btn apv-btn--ghost">Changes</span>
        </div>
      </div>
    </div>
  );
}

export function AppearancePanel({ user, onUserChanged }: {
  user: CurrentUser;
  onUserChanged: (u: CurrentUser) => void;
}) {
  const saved: ThemeId = isThemeId(user.theme) ? user.theme : "drafting";
  const [choice, setChoice] = useState<ThemeId>(saved);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);

  // The account is the source of truth: if it changes underneath this panel
  // (another device saved, or /api/auth/me refreshed), follow it rather than
  // keeping a selection the server has already disagreed with.
  useEffect(() => { setChoice(saved); }, [saved]);

  const dirty = choice !== saved;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await saveTheme(choice);
      // Only now does the whole app change -- and only after the server has
      // accepted it, so a rejected scheme never leaves the console painted
      // in something the account does not actually have.
      applyTheme(choice);
      onUserChanged({ ...user, theme: choice });
      setJustSaved(true);
      window.setTimeout(() => setJustSaved(false), 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not save the theme");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="appearance-panel">
      <div className="appearance-choices" role="radiogroup" aria-label="Colour scheme">
        {THEMES.map((t) => (
          <button
            key={t.id}
            type="button"
            role="radio"
            aria-checked={choice === t.id}
            className={`appearance-choice ${choice === t.id ? "is-chosen" : ""}`}
            onClick={() => setChoice(t.id)}
          >
            <span className="appearance-swatch" aria-hidden="true">
              {t.swatch.map((c, i) => <i key={i} style={{ background: c }} />)}
            </span>
            <span className="appearance-choice-label">
              {t.label}
              {t.id === saved && <span className="appearance-current">current</span>}
            </span>
            <span className="appearance-choice-blurb">{t.blurb}</span>
          </button>
        ))}
      </div>

      <Preview theme={choice} />

      <div className="appearance-actions">
        <button type="button" className="submit-btn" disabled={!dirty || saving} onClick={save}>
          {saving ? "Saving…" : dirty ? `Apply ${THEMES.find((t) => t.id === choice)?.label}` : "Saved"}
        </button>
        {dirty && !saving && (
          <button type="button" className="settings-btn" onClick={() => setChoice(saved)}>
            Cancel
          </button>
        )}
        {justSaved && <span className="appearance-ok" role="status">Applied.</span>}
        {error && <span className="appearance-error" role="alert">{error}</span>}
      </div>
      <p className="appearance-note">
        The scheme is saved to your account, so every browser you sign in from uses it.
        The signed-out landing page always stays in Drafting — it is the product's own colours,
        not a preference.
      </p>
    </div>
  );
}
