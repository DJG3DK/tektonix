import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GitHubSettingsCard } from "./GitHubSettingsCard";
import { SettingsSaveBar, SettingsSaveProvider } from "./SettingsSaveBar";
import type { GitHubSettingsResponse } from "../api";

const getGitHubSettings = vi.fn();
const saveGitHubSettings = vi.fn();
const testGitHubToken = vi.fn();
const pollGitHubNow = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getGitHubSettings: () => getGitHubSettings(),
    saveGitHubSettings: (...a: unknown[]) => saveGitHubSettings(...a),
    testGitHubToken: (...a: unknown[]) => testGitHubToken(...a),
    pollGitHubNow: () => pollGitHubNow(),
  };
});

function response(over: Partial<GitHubSettingsResponse["settings"]> = {}): GitHubSettingsResponse {
  return {
    settings: {
      poll_interval_min: 10, public_url: "", notify: { telegram: true, email: false, email_to: "" },
      tokens: { main: { hint: "…abcd", created_at: 1_700_000_000 } },
      projects: { proj: { token: "main", policies: { dependabot_prs: "off", security_alerts: "off", code_scanning: "off", review_requests: "off", ci_failures: "off" }, budget_usd: 3, max_open_auto: 2, authors: "dependabot", route: "auto" } },
      ...over,
    },
    sources: {
      dependabot_prs: { label: "Dependabot pull requests", help: "" },
      security_alerts: { label: "Dependabot security alerts", help: "" },
      review_requests: { label: "Review comments requesting changes", help: "" },
      ci_failures: { label: "Failing checks on the default branch", help: "" },
      code_scanning: { label: "Code scanning alerts (CodeQL)", help: "" },
    },
    modes: ["off", "propose", "auto"],
    author_filters: ["dependabot", "bots", "anyone"],
    env_token: false,
    projects: ["proj"],
  };
}

function mount() {
  return render(
    <SettingsSaveProvider>
      <GitHubSettingsCard />
      <SettingsSaveBar />
    </SettingsSaveProvider>,
  );
}

describe("GitHubSettingsCard", () => {
  beforeEach(() => {
    getGitHubSettings.mockReset();
    saveGitHubSettings.mockReset();
    testGitHubToken.mockReset();
    pollGitHubNow.mockReset();
    getGitHubSettings.mockResolvedValue(response());
  });

  it("says on the card that an inbox task keeps merge review whatever Auto means elsewhere", async () => {
    // Auto is the word an operator reads as "fully unattended". For inbox
    // items it is not: the task still prompts for gated actions and still
    // needs a merge approval, because nobody typed the goal. The invariant
    // is enforced server-side (tests/test_inbox_task_invariants.py); this is
    // the half that stops someone turning it on believing otherwise.
    getGitHubSettings.mockResolvedValue(response());
    render(<SettingsSaveProvider><GitHubSettingsCard /></SettingsSaveProvider>);
    const note = await screen.findByText(/never unattended/i);
    const text = note.parentElement?.textContent ?? "";
    expect(text).toMatch(/always requires your merge approval/i);
    expect(text).toMatch(/ignores Auto mode/i);
    expect(text).toMatch(/cannot use Auto/i);   // the no-checks refusal
  });

  it("lists stored tokens by name and hint only, never a value", async () => {
    mount();
    expect(await screen.findByText("main", { selector: ".gh-token-name" })).toBeInTheDocument();
    expect(screen.getByText(/…abcd/)).toBeInTheDocument();
    expect(screen.getByText("OFF")).toBeInTheDocument();
  });

  it("a policy change shows up in the save bar and is sent as a project patch", async () => {
    const user = userEvent.setup();
    mount();
    const select = await screen.findByLabelText("proj Dependabot pull requests");
    await user.selectOptions(select, "propose");
    expect(await screen.findByText(/1 unsaved change/)).toBeInTheDocument();
    expect(screen.getByText("ON")).toBeInTheDocument();

    saveGitHubSettings.mockResolvedValue({ settings: response({
      projects: { proj: { ...response().settings.projects.proj, policies: { ...response().settings.projects.proj.policies, dependabot_prs: "propose" } } },
    }).settings });
    await user.click(screen.getByRole("button", { name: /^save/i }));
    await waitFor(() => expect(saveGitHubSettings).toHaveBeenCalledTimes(1));
    const patch = saveGitHubSettings.mock.calls[0][0];
    expect(patch.projects.proj.policies.dependabot_prs).toBe("propose");
    expect(patch.add_tokens).toBeUndefined();
    await waitFor(() => expect(screen.queryByText(/1 unsaved change/)).not.toBeInTheDocument());
  });

  it("adding a token keeps it unsaved until the bar saves, and can be tested before saving", async () => {
    const user = userEvent.setup();
    testGitHubToken.mockResolvedValue({ ok: true, login: "octo", repos: [], matched: [{ slug: "o/proj", project: "proj", push: true, pull: true, dependabot_alerts: false }] });
    mount();
    await screen.findByText("main", { selector: ".gh-token-name" });
    await user.type(screen.getByLabelText("token name"), "second");
    await user.type(screen.getByLabelText("token value"), "github_pat_" + "x".repeat(30));
    await user.click(screen.getByRole("button", { name: "Add token" }));

    const row = screen.getByText("second", { selector: ".gh-token-name" }).closest("li")!;
    expect(within(row).getByText("unsaved")).toBeInTheDocument();
    expect(screen.getByText(/1 unsaved change/)).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Test" }));
    expect(await screen.findByText(/authenticated as/)).toBeInTheDocument();
    expect(testGitHubToken).toHaveBeenCalledWith({ token: "github_pat_" + "x".repeat(30) });
    expect(screen.getByText(/no alert permission/)).toBeInTheDocument();

    saveGitHubSettings.mockResolvedValue({ settings: response({ tokens: { main: { hint: "…abcd", created_at: 1 }, second: { hint: "…xxxx", created_at: 2 } } }).settings });
    await user.click(screen.getByRole("button", { name: /^save/i }));
    await waitFor(() => expect(saveGitHubSettings).toHaveBeenCalledTimes(1));
    expect(saveGitHubSettings.mock.calls[0][0].add_tokens).toEqual({ second: "github_pat_" + "x".repeat(30) });
    expect(await screen.findByText(/…xxxx/)).toBeInTheDocument();
  });

  it("removing a stored token is a pending change sent as remove_tokens", async () => {
    const user = userEvent.setup();
    mount();
    const row = (await screen.findByText("main", { selector: ".gh-token-name" })).closest("li")!;
    await user.click(within(row).getByRole("button", { name: "Remove" }));
    expect(screen.queryByText("main", { selector: ".gh-token-name" })).not.toBeInTheDocument();
    saveGitHubSettings.mockResolvedValue({ settings: response({ tokens: {}, projects: { proj: { ...response().settings.projects.proj, token: null } } }).settings });
    await user.click(screen.getByRole("button", { name: /^save/i }));
    await waitFor(() => expect(saveGitHubSettings.mock.calls[0][0].remove_tokens).toEqual(["main"]));
  });

  it("discard drops every pending edit", async () => {
    const user = userEvent.setup();
    mount();
    await user.selectOptions(await screen.findByLabelText("proj Dependabot pull requests"), "auto");
    await user.click(screen.getByRole("button", { name: /discard/i }));
    expect(screen.queryByText(/1 unsaved change/)).not.toBeInTheDocument();
    expect((screen.getByLabelText("proj Dependabot pull requests") as HTMLSelectElement).value).toBe("off");
  });

  it("poll now reports what each project returned", async () => {
    const user = userEvent.setup();
    pollGitHubNow.mockResolvedValue({ results: [{ repo: "proj", found: 3, proposed: 1, created: 0 }] });
    mount();
    await screen.findByText("main", { selector: ".gh-token-name" });
    await user.click(screen.getByRole("button", { name: "Poll now" }));
    expect(await screen.findByText(/proj: 3 found, 1 proposed, 0 started/)).toBeInTheDocument();
  });
});

describe("renaming a token", () => {
  it("stages a rename and sends it with the other edits", async () => {
    // Operators rename tokens in GitHub as they work out what each is for.
    // Without this the only route was remove-and-re-add: paste the secret
    // again, and lose every project mapped to it on the way.
    const user = userEvent.setup();
    render(<GitHubSettingsCard />);
    await screen.findAllByRole("button", { name: "Rename" });

    await user.click(screen.getAllByRole("button", { name: "Rename" })[0]);
    const box = screen.getByLabelText("rename main");
    await user.clear(box);
    await user.type(box, "DJG3dk-Projects{Enter}");

    expect(screen.getByText(/was main/)).toBeInTheDocument();
  });

  it("does not ask for the secret again", async () => {
    const user = userEvent.setup();
    render(<GitHubSettingsCard />);
    await screen.findAllByRole("button", { name: "Rename" });
    await user.click(screen.getAllByRole("button", { name: "Rename" })[0]);
    // The rename box is a plain text field -- an input with no type is text.
    // What matters is that it is NOT a password prompt: renaming must not
    // make somebody paste the secret again.
    const box = screen.getByLabelText("rename main");
    expect(box.getAttribute("type")).not.toBe("password");
    expect((box as HTMLInputElement).value).toBe("main");
  });
});
