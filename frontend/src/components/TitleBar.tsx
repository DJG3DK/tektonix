import { useEffect, useState } from "react";
import "./TitleBar.css";

/* Inside the desktop app the dashboard window is frameless, so this strip is
   its title bar: a drag handle and the three window controls. Outside the
   app (a browser tab) it renders nothing. The app injects window.__TAURI__
   into this page through its dashboard capability (app/src-tauri/
   capabilities/dashboard.json). */
type TauriWindow = {
  minimize: () => Promise<void>;
  toggleMaximize: () => Promise<void>;
  close: () => Promise<void>;
};

function currentWindow(): TauriWindow | null {
  const w = (window as unknown as { __TAURI__?: { window?: { getCurrentWindow?: () => TauriWindow } } }).__TAURI__;
  try {
    return w?.window?.getCurrentWindow?.() ?? null;
  } catch {
    return null;
  }
}

export function insideDesktopApp(): boolean {
  return currentWindow() !== null;
}

export function TitleBar() {
  const [win, setWin] = useState<TauriWindow | null>(null);
  useEffect(() => {
    const w = currentWindow();
    setWin(w);
    if (w) document.documentElement.classList.add("in-desktop-app");
    return () => document.documentElement.classList.remove("in-desktop-app");
  }, []);
  if (!win) return null;
  return (
    <div className="titlebar" data-tauri-drag-region data-testid="titlebar">
      <span className="titlebar-name" data-tauri-drag-region>Tektonix</span>
      <span className="titlebar-controls">
        <button type="button" className="titlebar-btn" aria-label="Minimise" onClick={() => void win.minimize()}>&#x2500;</button>
        <button type="button" className="titlebar-btn" aria-label="Maximise" onClick={() => void win.toggleMaximize()}>&#x25A1;</button>
        <button type="button" className="titlebar-btn titlebar-btn--close" aria-label="Close" onClick={() => void win.close()}>&#x2715;</button>
      </span>
    </div>
  );
}
