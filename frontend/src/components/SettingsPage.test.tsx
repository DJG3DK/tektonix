import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "./SettingsPage";
import type { CurrentUser } from "../types";

const setAutoApprove = vi.fn();
const listProjectsConfig = vi.fn();
const getTelegramSettings = vi.fn();
const disable2FA = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    setAutoApprove: (...a: unknown[]) => setAutoApprove(...a),
    listProjectsConfig: () => listProjectsConfig(),
    getTelegramSettings: () => getTelegramSettings(),
    disable2FA: (...a: unknown[]) => disable2FA(...a),
  };
});

function user(over: Partial<CurrentUser> = {}): CurrentUser {
  return {
    id: 1, email: "dev@example.com", role: "user", allowed_repos: ["sandbox", "production"],
    totp_enabled: true, must_change_password: false, require_totp_setup: false,
    auto_approve_commands: false, auto_approve_repos: [], require_merge_review: true, theme: "drafting",
    ...over,
  };
}

/** Render the page and open the section the auto-mode controls live in.
 *
 *  The page shows one section at a time now (2026-09-13), so these controls
 *  are behind a nav click rather than somewhere in a 4300px scroll. Also
 *  clears the remembered section, which would otherwise leak between tests
 *  through localStorage. */
async function openAgentBehavior(u: CurrentUser, onUserChanged = () => {}) {
  // Guarded the same way the component guards it: this environment has no
  // localStorage at all, which is precisely the case rememberedSection() has
  // to survive.
  try {
    localStorage.clear();
  } catch {
    /* no storage here; the page falls back to the first section */
  }
  render(<SettingsPage user={u} onUserChanged={onUserChanged} />);
  await userEvent.click(screen.getByRole("button", { name: "Agent behavior" }));
}

describe("auto mode is chosen per project", () => {
  beforeEach(() => {
    setAutoApprove.mockReset();
    setAutoApprove.mockResolvedValue({ auto_approve_repos: ["sandbox"] });
    listProjectsConfig.mockReset();
    listProjectsConfig.mockResolvedValue({ projects: { sandbox: {}, production: {} } });
    getTelegramSettings.mockReset();
    getTelegramSettings.mockResolvedValue({ configured: false, chat_id: "", bot_token_hint: "" });
  });

  it("will not turn on until a project is ticked", async () => {
    await openAgentBehavior(user(), () => {});
    await userEvent.click(screen.getByRole("button", { name: /Turn auto mode on/ }));
    await waitFor(() => expect(screen.getByLabelText("sandbox")).toBeTruthy());

    const confirm = screen.getByRole("button", { name: /Turn it on for no projects/ });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    expect(setAutoApprove).not.toHaveBeenCalled();
  });

  it("sends exactly the projects that were ticked", async () => {
    const onUserChanged = vi.fn();
    await openAgentBehavior(user(), onUserChanged);
    await userEvent.click(screen.getByRole("button", { name: /Turn auto mode on/ }));
    await waitFor(() => expect(screen.getByLabelText("sandbox")).toBeTruthy());

    await userEvent.click(screen.getByLabelText("sandbox"));
    await userEvent.click(screen.getByRole("button", { name: /Turn it on for 1 project$/ }));

    expect(setAutoApprove).toHaveBeenCalledWith(true, ["sandbox"]);
    expect(onUserChanged).toHaveBeenCalledWith(
      expect.objectContaining({ auto_approve_commands: true, auto_approve_repos: ["sandbox"] }),
    );
  });

  it("names the projects it covers once it is on", async () => {
    await openAgentBehavior(user({ auto_approve_commands: true, auto_approve_repos: ["sandbox"] }));
    const covers = await screen.findByText(/Covers:/);
    expect(covers.parentElement?.textContent).toMatch(/Covers:\s*sandbox/);
  });

  it("says plainly when it is on but covers nothing", async () => {
    await openAgentBehavior(user({ auto_approve_commands: true, auto_approve_repos: [] }));
    expect(await screen.findByText(/auto mode is on but applies nowhere/i)).toBeTruthy();
  });

  it("turning it off does not resend the project list", async () => {
    await openAgentBehavior(user({ auto_approve_commands: true, auto_approve_repos: ["sandbox"] }));
    await userEvent.click(await screen.findByRole("button", { name: /Turn auto mode off/ }));
    expect(setAutoApprove).toHaveBeenCalledWith(false, undefined);
  });
});

describe("two-factor in Settings", () => {
  function openAccount(u: CurrentUser, onUserChanged = vi.fn()) {
    try {
      localStorage.clear();
    } catch {
      /* no storage in this environment */
    }
    render(<SettingsPage user={u} onUserChanged={onUserChanged} />);
    return onUserChanged;
  }

  beforeEach(() => {
    disable2FA.mockReset();
    getTelegramSettings.mockReset();
    getTelegramSettings.mockResolvedValue({ configured: false, chat_id: "", bot_token_hint: "" });
    listProjectsConfig.mockReset();
    listProjectsConfig.mockResolvedValue({ projects: {} });
  });

  it("a user can turn it off, with their password", async () => {
    disable2FA.mockResolvedValue(undefined);
    const changed = openAccount(user({ totp_enabled: true }));
    const card = screen.getByRole("heading", { name: /two-factor authentication/i }).closest("section")!;
    const turnOff = within(card).getByRole("button", { name: /turn off 2fa/i });
    expect(turnOff).toBeDisabled();                          // not without the password
    await userEvent.type(within(card).getByLabelText(/current password/i), "hunter2hunter2");
    await userEvent.click(turnOff);
    expect(disable2FA).toHaveBeenCalledWith("hunter2hunter2");
    await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ totp_enabled: false })));
  });

  it("says why when the server refuses", async () => {
    disable2FA.mockRejectedValue(new Error("current password required to disable 2FA"));
    openAccount(user({ totp_enabled: true }));
    const card = screen.getByRole("heading", { name: /two-factor authentication/i }).closest("section")!;
    await userEvent.type(within(card).getByLabelText(/current password/i), "wrong");
    await userEvent.click(within(card).getByRole("button", { name: /turn off 2fa/i }));
    expect(await within(card).findByText(/current password required/)).toBeInTheDocument();
  });

  it("a user without it is offered to turn it on", () => {
    openAccount(user({ totp_enabled: false }));
    const card = screen.getByRole("heading", { name: /two-factor authentication/i }).closest("section")!;
    expect(within(card).getByRole("button", { name: /turn on 2fa/i })).toBeInTheDocument();
  });

  it("an admin cannot turn it off, and is told where recovery lives", () => {
    openAccount(user({ role: "admin", allowed_repos: null, totp_enabled: true }));
    const card = screen.getByRole("heading", { name: /two-factor authentication/i }).closest("section")!;
    expect(within(card).queryByRole("button", { name: /turn off/i })).toBeNull();
    expect(within(card).getByText(/recovery codes/i)).toBeInTheDocument();
  });
});
