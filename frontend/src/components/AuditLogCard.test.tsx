import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuditLogCard } from "./AuditLogCard";
import type { AuditEntry } from "../api";

const getAuditLog = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getAuditLog: (...a: unknown[]) => getAuditLog(...a) };
});

function entry(over: Partial<AuditEntry> = {}): AuditEntry {
  return {
    ts: Date.now() / 1000 - 120,
    actor: "admin@example.com",
    action: "settings.auto_approve",
    label: "changed auto-approve of commands",
    target: "operator@example.com",
    detail: "on for storefront",
    ...over,
  };
}

describe("AuditLogCard", () => {
  beforeEach(() => {
    getAuditLog.mockReset();
  });

  it("shows who did what, where, and when", async () => {
    getAuditLog.mockResolvedValue([entry()]);
    render(<AuditLogCard />);
    expect(await screen.findByText("admin@example.com")).toBeTruthy();
    expect(screen.getByText("operator@example.com")).toBeTruthy();  // who it was done to
    expect(screen.getByText(/changed auto-approve of commands/)).toBeTruthy();
    expect(screen.getByText(/on for storefront/)).toBeTruthy();
    expect(screen.getByText("2 min ago")).toBeTruthy();
  });

  it("an empty log says so rather than looking broken", async () => {
    getAuditLog.mockResolvedValue([]);
    render(<AuditLogCard />);
    expect(await screen.findByText(/Nothing recorded yet/i)).toBeTruthy();
  });

  it("reports a failure instead of rendering an empty table", async () => {
    getAuditLog.mockRejectedValue(new Error("admin only"));
    render(<AuditLogCard />);
    expect(await screen.findByText("admin only")).toBeTruthy();
  });

  it("shows the last ten and expands to the rest", async () => {
    getAuditLog.mockResolvedValue(
      Array.from({ length: 14 }, (_, i) => entry({ ts: Date.now() / 1000 - i * 60, actor: `u${i}@x` })),
    );
    render(<AuditLogCard />);
    expect(await screen.findByText("u0@x")).toBeTruthy();
    expect(screen.queryByText("u12@x")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Show all 14/ }));
    expect(screen.getByText("u12@x")).toBeTruthy();
  });

  it("never renders a raw timestamp as the visible cell", async () => {
    const ts = Date.now() / 1000 - 3600;
    getAuditLog.mockResolvedValue([entry({ ts })]);
    render(<AuditLogCard />);
    expect(await screen.findByText("1h ago")).toBeTruthy();
    expect(screen.queryByText(String(ts))).toBeNull();
  });
});
