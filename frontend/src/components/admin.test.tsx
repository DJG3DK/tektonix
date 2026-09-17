import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChangePasswordPage } from "./ChangePasswordPage";
import { SetupTotpPage } from "./SetupTotpPage";
import { UsersPanel } from "./UsersPanel";
import { user } from "../test/fixtures";

const listUsers = vi.fn();
const createUser = vi.fn();
const deleteUser = vi.fn();
const updateUserAccess = vi.fn();
const changePassword = vi.fn();
const setup2FA = vi.fn();
const confirm2FA = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listUsers: () => listUsers(),
    createUser: (...a: unknown[]) => createUser(...a),
    deleteUser: (...a: unknown[]) => deleteUser(...a),
    updateUserAccess: (...a: unknown[]) => updateUserAccess(...a),
    changePassword: (...a: unknown[]) => changePassword(...a),
    setup2FA: () => setup2FA(),
    confirm2FA: (...a: unknown[]) => confirm2FA(...a),
  };
});

beforeEach(() => {
  [listUsers, createUser, deleteUser, updateUserAccess, changePassword, setup2FA, confirm2FA].forEach(
    (m) => m.mockReset(),
  );
  listUsers.mockResolvedValue([]);
});

describe("UsersPanel", () => {
  it("lists the accounts that exist", async () => {
    listUsers.mockResolvedValue([user({ email: "a@x.test" }), user({ id: 2, email: "b@x.test" })]);
    render(<UsersPanel repos={["webapp"]} />);
    expect(await screen.findByText("a@x.test")).toBeInTheDocument();
    expect(screen.getByText("b@x.test")).toBeInTheDocument();
  });

  async function fillNewUser(email = "new@x.test", password = "Passw0rdPassw0rd") {
    await userEvent.type(document.querySelector('input[type="email"]') as HTMLInputElement, email);
    await userEvent.type(
      screen.getByPlaceholderText(/at least 12 characters/i) as HTMLInputElement,
      password,
    );
    await userEvent.click(document.querySelector('input[type="checkbox"]') as HTMLInputElement);
  }

  it("will not create a user with access to nothing", async () => {
    // Least privilege runs both ways: an account scoped to no repo at all is
    // not a safe default, it is a broken one.
    createUser.mockResolvedValue(undefined);
    render(<UsersPanel repos={["webapp"]} />);
    await waitFor(() => expect(listUsers).toHaveBeenCalled());

    await userEvent.type(document.querySelector('input[type="email"]') as HTMLInputElement, "new@x.test");
    await userEvent.type(screen.getByPlaceholderText(/at least 12 characters/i), "Passw0rdPassw0rd");
    await userEvent.click(screen.getByRole("button", { name: /create user/i }));
    expect(createUser).not.toHaveBeenCalled();
  });

  it("creates a user scoped to the repos that were ticked", async () => {
    createUser.mockResolvedValue(undefined);
    render(<UsersPanel repos={["webapp"]} />);
    await waitFor(() => expect(listUsers).toHaveBeenCalled());

    await fillNewUser();
    await userEvent.click(screen.getByRole("button", { name: /create user/i }));
    await waitFor(() =>
      expect(createUser).toHaveBeenCalledWith("new@x.test", "Passw0rdPassw0rd", "user", ["webapp"]),
    );
  });

  it("confirms creation without echoing the password back into the page", async () => {
    // The temporary password is used once and then forgotten by the UI; the
    // new account is forced to set its own on first login, so re-displaying
    // it would leave a live credential sitting on screen for no benefit.
    createUser.mockResolvedValue(undefined);
    render(<UsersPanel repos={["webapp"]} />);
    await waitFor(() => expect(listUsers).toHaveBeenCalled());
    await fillNewUser("new@x.test", "Passw0rdPassw0rd");
    await userEvent.click(screen.getByRole("button", { name: /create user/i }));

    expect(await screen.findByText(/user created/i)).toBeInTheDocument();
    expect(screen.getByText(/set their own password on first login/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("Passw0rdPassw0rd");
  });

  it("clears the form after a successful create", async () => {
    // Leaving a password in a field after submit is a credential left lying
    // around in the DOM.
    createUser.mockResolvedValue(undefined);
    render(<UsersPanel repos={["webapp"]} />);
    await waitFor(() => expect(listUsers).toHaveBeenCalled());
    await fillNewUser();
    await userEvent.click(screen.getByRole("button", { name: /create user/i }));

    await screen.findByText(/user created/i);
    expect((document.querySelector('input[type="email"]') as HTMLInputElement).value).toBe("");
    expect((screen.getByPlaceholderText(/at least 12 characters/i) as HTMLInputElement).value).toBe("");
  });

  it("reports a creation failure rather than a silent no-op", async () => {
    createUser.mockRejectedValue(new Error("email already exists"));
    render(<UsersPanel repos={["webapp"]} />);
    await waitFor(() => expect(listUsers).toHaveBeenCalled());
    await fillNewUser("dupe@x.test");
    await userEvent.click(screen.getByRole("button", { name: /create user/i }));
    expect(await screen.findByText(/email already exists/i)).toBeInTheDocument();
  });

  it("offers per-repo scoping so an account need not see everything", async () => {
    listUsers.mockResolvedValue([]);
    render(<UsersPanel repos={["webapp", "storefront"]} />);
    await waitFor(() => expect(listUsers).toHaveBeenCalled());
    expect(screen.getAllByText(/webapp/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/storefront/).length).toBeGreaterThan(0);
  });
});

describe("ChangePasswordPage", () => {
  it("refuses a mismatch without calling the server", async () => {
    render(<ChangePasswordPage onDone={vi.fn()} />);
    const boxes = document.querySelectorAll('input[type="password"]');
    await userEvent.type(boxes[boxes.length - 2] as HTMLInputElement, "Passw0rdPassw0rd");
    await userEvent.type(boxes[boxes.length - 1] as HTMLInputElement, "somethingElse1X");
    await userEvent.click(screen.getByRole("button", { name: /set password/i }));

    expect(changePassword).not.toHaveBeenCalled();
  });

  it("hands control back once the password is accepted", async () => {
    changePassword.mockResolvedValue(undefined);
    const onDone = vi.fn();
    render(<ChangePasswordPage onDone={onDone} />);
    const boxes = document.querySelectorAll('input[type="password"]');
    for (const b of boxes) await userEvent.type(b as HTMLInputElement, "Passw0rdPassw0rd");
    await userEvent.click(screen.getByRole("button", { name: /set password/i }));

    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("surfaces a rejection from the server", async () => {
    changePassword.mockRejectedValue(new Error("password too common"));
    render(<ChangePasswordPage onDone={vi.fn()} />);
    const boxes = document.querySelectorAll('input[type="password"]');
    for (const b of boxes) await userEvent.type(b as HTMLInputElement, "Passw0rdPassw0rd");
    await userEvent.click(screen.getByRole("button", { name: /set password/i }));

    expect(await screen.findByText(/password too common/i)).toBeInTheDocument();
  });
});

describe("SetupTotpPage", () => {
  it("fetches the enrolment secret on mount", async () => {
    setup2FA.mockResolvedValue({ secret: "JBSWY3DPEHPK3PXP", uri: "otpauth://totp/x" });
    render(<SetupTotpPage onDone={vi.fn()} />);
    await waitFor(() => expect(setup2FA).toHaveBeenCalled());
  });

  it("shows the secret so it can be entered by hand", async () => {
    setup2FA.mockResolvedValue({ secret: "JBSWY3DPEHPK3PXP", uri: "otpauth://totp/x" });
    render(<SetupTotpPage onDone={vi.fn()} />);
    expect(await screen.findByText(/JBSWY3DPEHPK3PXP/)).toBeInTheDocument();
  });

  it("only completes once a code verifies", async () => {
    setup2FA.mockResolvedValue({ secret: "JBSWY3DPEHPK3PXP", uri: "otpauth://totp/x" });
    confirm2FA.mockRejectedValue(new Error("invalid code"));
    const onDone = vi.fn();
    render(<SetupTotpPage onDone={onDone} />);
    await screen.findByText(/JBSWY3DPEHPK3PXP/);

    const code = document.querySelector("input") as HTMLInputElement;
    await userEvent.type(code, "000000");
    await userEvent.click(screen.getByRole("button", { name: /verify|confirm|enable/i }));

    expect(await screen.findByText(/invalid code/i)).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
  });
});
