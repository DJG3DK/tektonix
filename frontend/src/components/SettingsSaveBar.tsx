/* oxlint-disable react/only-export-components -- helpers exported for their tests; an edit here remounts the file under Fast Refresh, which is fine */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import "./SettingsSaveBar.css";

/* ONE save control for the whole settings page.
 *
 * Every editable card used to carry its own Save button, which meant a row of
 * controls that were disabled almost all of the time, and an edit made in a
 * card you had since scrolled past was easy to abandon unsaved. Instead each
 * panel REGISTERS how many pending changes it holds and how to commit them;
 * the page renders a single bar, pinned bottom-right, that appears only when
 * something is genuinely dirty and cannot be scrolled away from.
 *
 * The registry is a ref, not state, so a panel re-rendering on every keystroke
 * does not re-render the bar -- only a change in the pending COUNT does.
 */

type Entry = { count: number; save: () => Promise<void>; discard: () => void };

type Ctx = {
  register: (id: string, entry: Entry) => void;
  unregister: (id: string) => void;
};

const SaveCtx = createContext<Ctx | null>(null);
const CountsCtx = createContext<Record<string, number>>({});
const EntriesCtx = createContext<{ current: Record<string, Entry> } | null>(null);

export function SettingsSaveProvider({ children }: { children: ReactNode }) {
  const entries = useRef<Record<string, Entry>>({});
  const [counts, setCounts] = useState<Record<string, number>>({});

  const register = useCallback((id: string, entry: Entry) => {
    entries.current[id] = entry;
    setCounts((prev) => (prev[id] === entry.count ? prev : { ...prev, [id]: entry.count }));
  }, []);

  const unregister = useCallback((id: string) => {
    delete entries.current[id];
    setCounts((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const ctx = useMemo(() => ({ register, unregister }), [register, unregister]);

  return (
    <SaveCtx.Provider value={ctx}>
      <EntriesCtx.Provider value={entries}>
        <CountsCtx.Provider value={counts}>{children}</CountsCtx.Provider>
      </EntriesCtx.Provider>
    </SaveCtx.Provider>
  );
}

/** Report this panel's pending edits to the page's single save bar.
 *
 *  `save` and `discard` are read through a ref at call time, so a panel may
 *  hand over fresh closures on every render without re-registering. */
export function useSettingsSave(
  id: string,
  count: number,
  save: () => Promise<void>,
  discard: () => void,
) {
  const ctx = useContext(SaveCtx);
  const latest = useRef({ save, discard });
  useEffect(() => {
    latest.current = { save, discard };
  });

  useEffect(() => {
    if (!ctx) return;
    ctx.register(id, {
      count,
      save: () => latest.current.save(),
      discard: () => latest.current.discard(),
    });
  }, [ctx, id, count]);

  useEffect(() => () => ctx?.unregister(id), [ctx, id]);
}

export function SettingsSaveBar() {
  const entries = useContext(EntriesCtx);
  const counts = useContext(CountsCtx);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const total = Object.values(counts).reduce((a, b) => a + b, 0);

  useEffect(() => {
    if (!savedAt) return;
    const t = window.setTimeout(() => setSavedAt(0), 4000);
    return () => window.clearTimeout(t);
  }, [savedAt]);

  if (!entries) return null;
  if (total === 0 && !savedAt && !error) return null;

  async function saveAll() {
    if (!entries) return;
    setSaving(true);
    setError(null);
    // Panels are committed one at a time rather than in parallel: each one
    // writes to a different place (the store, a .env file), and a partial
    // failure is far easier to reason about when the order is known.
    try {
      for (const entry of Object.values(entries.current)) {
        if (entry.count > 0) await entry.save();
      }
      setSavedAt(Date.now());
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  }

  function discardAll() {
    if (!entries) return;
    for (const entry of Object.values(entries.current)) entry.discard();
    setError(null);
  }

  return (
    <div className="savebar" role="status">
      {error ? (
        <span className="savebar-error">{error}</span>
      ) : total === 0 ? (
        <span className="savebar-ok">Saved.</span>
      ) : (
        <>
          <span className="savebar-count">
            {total} unsaved change{total === 1 ? "" : "s"}
          </span>
          <button className="savebar-discard" onClick={discardAll} disabled={saving}>
            Discard
          </button>
        </>
      )}
      {total > 0 && (
        <button className="btn btn--primary" onClick={saveAll} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </button>
      )}
    </div>
  );
}
