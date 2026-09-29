import { useEffect, useState } from "react";
import { getTaskDefaults } from "./api";

/** The budget a new task starts with, from Settings → Runtime limits
 *  ("Default task budget"). The Build Now popup and the New Task form used to
 *  hard-code $2 regardless of that setting (operator report, 2026-09-09).
 *  Fetched once per page load and shared; the fallback only shows until the
 *  fetch lands, or if it fails (the server applies the real default anyway). */
const FALLBACK_TASK_BUDGET_USD = 2.0;

let cached: number | null = null;
let inflight: Promise<number> | null = null;

function load(): Promise<number> {
  if (cached !== null) return Promise.resolve(cached);
  if (!inflight) {
    inflight = getTaskDefaults()
      .then((r) => {
        const v = r.default_task_budget_usd;
        cached = typeof v === "number" && v > 0 ? v : FALLBACK_TASK_BUDGET_USD;
        return cached;
      })
      .catch(() => FALLBACK_TASK_BUDGET_USD)
      .finally(() => { inflight = null; });
  }
  return inflight;
}

/** For tests: forget the cached value. */
export function _resetDefaultTaskBudgetCache() { cached = null; inflight = null; }

export function useDefaultTaskBudget(): number {
  const [value, setValue] = useState<number>(cached ?? FALLBACK_TASK_BUDGET_USD);
  useEffect(() => {
    let alive = true;
    load().then((v) => { if (alive) setValue(v); });
    return () => { alive = false; };
  }, []);
  return value;
}

/** A budget input's state: follows the configured default until the user
 *  types a value of their own, then keeps what they typed. */
export function useBudgetInput(): [number, (v: number) => void] {
  const defaultBudget = useDefaultTaskBudget();
  const [budget, setBudget] = useState<number>(defaultBudget);
  const [touched, setTouched] = useState(false);
  // Follows the default until touched. Adjusted during render, not in an
  // effect: the input never shows the old default for a frame.
  const [seenDefault, setSeenDefault] = useState(defaultBudget);
  if (seenDefault !== defaultBudget) {
    setSeenDefault(defaultBudget);
    if (!touched) setBudget(defaultBudget);
  }
  return [budget, (v: number) => { setTouched(true); setBudget(v); }];
}
