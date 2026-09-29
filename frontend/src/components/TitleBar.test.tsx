import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TitleBar, insideDesktopApp } from "./TitleBar";

// Frameless desktop window: the dashboard draws its own strip, and only there.
describe("TitleBar", () => {
  afterEach(() => {
    cleanup();
    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
    document.documentElement.classList.remove("in-desktop-app");
  });

  it("renders nothing in a browser tab", () => {
    render(<TitleBar />);
    expect(screen.queryByTestId("titlebar")).not.toBeInTheDocument();
    expect(insideDesktopApp()).toBe(false);
  });

  it("inside the app it is a drag handle with the three window controls", async () => {
    const win = { minimize: vi.fn(async () => {}), toggleMaximize: vi.fn(async () => {}), close: vi.fn(async () => {}) };
    const invoke = vi.fn(async () => undefined);
    (window as unknown as { __TAURI__: unknown }).__TAURI__ = { window: { getCurrentWindow: () => win }, core: { invoke } };
    render(<TitleBar />);
    const bar = await screen.findByTestId("titlebar");
    expect(bar).toHaveAttribute("data-tauri-drag-region");
    expect(document.documentElement.classList.contains("in-desktop-app")).toBe(true);
    screen.getByRole("button", { name: "Minimise" }).click();
    screen.getByRole("button", { name: "Maximise" }).click();
    screen.getByRole("button", { name: "Close" }).click();
    expect(win.minimize).toHaveBeenCalled();
    expect(win.toggleMaximize).toHaveBeenCalled();
    expect(win.close).toHaveBeenCalled();
    screen.getByRole("button", { name: "Control panel" }).click();
    expect(invoke).toHaveBeenCalledWith("open_panel");
  });
});
