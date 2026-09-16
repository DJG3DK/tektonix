import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiKeysPanel } from "./ApiKeysPanel";
import { SettingsSaveBar, SettingsSaveProvider } from "./SettingsSaveBar";
import { DeployKeyCard } from "./DeployKeyCard";
import type { EnvKey } from "../api";

const getEnvConfig = vi.fn();
const saveEnvConfig = vi.fn();
const getDeployKey = vi.fn();
const setDeployKey = vi.fn();
const checkDeployKeyRemote = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getEnvConfig: () => getEnvConfig(),
    saveEnvConfig: (...a: unknown[]) => saveEnvConfig(...a),
    getDeployKey: (...a: unknown[]) => getDeployKey(...a),
    setDeployKey: (...a: unknown[]) => setDeployKey(...a),
    testDeployKey: (...a: unknown[]) => checkDeployKeyRemote(...a),
    deleteDeployKey: vi.fn(async () => {}),
    restartServices: vi.fn(async () => {}),
  };
});

/* The panel has no Save button of its own any more -- the settings page owns a
   single sticky bar and every panel feeds it. Rendering the pair together is
   what a user actually gets. */
const renderPanel = () =>
  render(
    <SettingsSaveProvider>
      <ApiKeysPanel />
      <SettingsSaveBar />
    </SettingsSaveProvider>,
  );

const envKey = (over: Partial<EnvKey> = {}): EnvKey =>
  ({
    key: "OPENROUTER_API_KEY",
    label: "OpenRouter API key",
    help: "used for every model call",
    group: "Model routing",
    secret: true,
    is_set: true,
    display: "…a1b2",
    ...over,
  }) as EnvKey;

beforeEach(() => {
  getEnvConfig.mockReset();
  saveEnvConfig.mockReset();
  getDeployKey.mockReset();
  setDeployKey.mockReset();
  checkDeployKeyRemote.mockReset();
});

describe("ApiKeysPanel — a secret's real value never reaches the browser", () => {
  it("shows the masked hint as a placeholder, never as the field's value", async () => {
    // Placeholder, not value: a prefilled mask would be written back as the
    // real secret the first time the form was saved.
    getEnvConfig.mockResolvedValue({ keys: [envKey({ display: "…a1b2" })] });
    renderPanel();
    const input = (await screen.findByLabelText("OpenRouter API key")) as HTMLInputElement;
    expect(input.placeholder).toMatch(/…a1b2/);
    expect(input.value).toBe("");
  });

  it("says outright that a blank field keeps the existing value", async () => {
    getEnvConfig.mockResolvedValue({ keys: [envKey()] });
    renderPanel();
    const input = (await screen.findByLabelText("OpenRouter API key")) as HTMLInputElement;
    expect(input.placeholder).toMatch(/blank keeps it/i);
  });

  it("renders a secret as a password field until revealed", async () => {
    getEnvConfig.mockResolvedValue({ keys: [envKey({ secret: true })] });
    renderPanel();
    const input = (await screen.findByLabelText("OpenRouter API key")) as HTMLInputElement;
    expect(input.type).toBe("password");
    await userEvent.click(screen.getByRole("button", { name: /show what you typed/i }));
    expect((screen.getByLabelText("OpenRouter API key") as HTMLInputElement).type).toBe("text");
  });

  it("marks an unset key as not set", async () => {
    getEnvConfig.mockResolvedValue({ keys: [envKey({ is_set: false, display: "" })] });
    renderPanel();
    expect(await screen.findByText("not set")).toBeInTheDocument();
  });

  it("sends only the fields that were actually edited", async () => {
    getEnvConfig.mockResolvedValue({
      keys: [envKey({ key: "A" }), envKey({ key: "B" })],
    });
    saveEnvConfig.mockResolvedValue({ restart_required: [] });
    renderPanel();
    await screen.findAllByLabelText("OpenRouter API key");

    const first = document.querySelector("input") as HTMLInputElement;
    await userEvent.type(first, "new-secret");
    await userEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => expect(saveEnvConfig).toHaveBeenCalled());
    const sent = saveEnvConfig.mock.calls[0][0] as Record<string, string>;
    expect(Object.keys(sent)).toHaveLength(1);
    expect(Object.values(sent)[0]).toBe("new-secret");
  });

  it("offers a restart rather than performing one", async () => {
    // Restarting the router interrupts every in-flight model call, so it must
    // never be a side effect of saving a form.
    getEnvConfig.mockResolvedValue({ keys: [envKey()] });
    saveEnvConfig.mockResolvedValue({ restart_required: ["model-router"] });
    renderPanel();
    await screen.findByLabelText("OpenRouter API key");

    await userEvent.type(document.querySelector("input") as HTMLInputElement, "x");
    await userEvent.click(screen.getByRole("button", { name: /save/i }));

    expect(await screen.findByText(/model-router/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /restart/i })).toBeInTheDocument();
  });

  it("reports a save failure instead of implying success", async () => {
    getEnvConfig.mockResolvedValue({ keys: [envKey()] });
    saveEnvConfig.mockRejectedValue(new Error("write failed"));
    renderPanel();
    await screen.findByLabelText("OpenRouter API key");
    await userEvent.type(document.querySelector("input") as HTMLInputElement, "x");
    await userEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(await screen.findByText(/write failed/i)).toBeInTheDocument();
  });
});

describe("DeployKeyCard", () => {
  const status = (over: Record<string, unknown> = {}) => ({
    project: "3d-bot",
    installed: false,
    fingerprint: null,
    public_key: null,
    remote: "git@github.com:DJG3DK/3d-bot.git",
    remote_kind: "ssh",
    configured: false,
    detail: null,
    ...over,
  });

  it("reports when no key is installed", async () => {
    getDeployKey.mockResolvedValue(status());
    render(<DeployKeyCard project="3d-bot" />);
    await waitFor(() => expect(getDeployKey).toHaveBeenCalledWith("3d-bot"));
    expect(document.body.textContent).toMatch(/3d-bot/);
  });

  it("shows a fingerprint but never a private key once one is installed", async () => {
    getDeployKey.mockResolvedValue(
      status({ installed: true, configured: true, fingerprint: "SHA256:abc123", public_key: "ssh-ed25519 AAAA…" }),
    );
    render(<DeployKeyCard project="3d-bot" />);
    expect(await screen.findByText(/SHA256:abc123/)).toBeInTheDocument();
    // The public half is fine to show; there is no endpoint that returns the
    // private half, and nothing here should ever render one.
    expect(document.body.textContent).not.toMatch(/BEGIN OPENSSH PRIVATE KEY/);
  });

  it("surfaces a remote check result", async () => {
    getDeployKey.mockResolvedValue(status({ installed: true, configured: true, fingerprint: "SHA256:abc" }));
    checkDeployKeyRemote.mockResolvedValue({ ok: false, detail: "permission denied" });
    render(<DeployKeyCard project="3d-bot" />);
    await screen.findByText(/SHA256:abc/);

    await userEvent.click(screen.getByRole("button", { name: /test connection/i }));
    expect(await screen.findByText(/permission denied/i)).toBeInTheDocument();
  });

  it("does not crash when the status call fails", async () => {
    getDeployKey.mockRejectedValue(new Error("nope"));
    render(<DeployKeyCard project="3d-bot" />);
    await waitFor(() => expect(getDeployKey).toHaveBeenCalled());
    expect(document.body).toBeTruthy();
  });
});
