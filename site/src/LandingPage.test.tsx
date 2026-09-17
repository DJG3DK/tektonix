import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { LandingPage } from "./LandingPage";

/* The public page. Moved here from the console's test suite when the two
 * became separate builds (2026-09-17); the sign-in assertion changed with it,
 * because signing in is now a link to another host rather than a callback
 * into an app that shared this page's bundle. */

describe("LandingPage", () => {
  it("leads with what the product does", () => {
    render(<LandingPage />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/autonomous coding agent/i);
  });

  it("sends sign-in to the console's own host", () => {
    // The whole point of the split: this page carries no application code,
    // so the only way to the console is a link to where it now lives.
    render(<LandingPage />);
    const signIn = screen.getByRole("link", { name: /^sign in$/i });
    expect(signIn).toHaveAttribute("href", "https://agent.tektonix.io");
  });

  it("has exactly one sign-in control", () => {
    // The closing section used to carry a second one, splitting the call to
    // action between signing in and reading the source.
    render(<LandingPage />);
    const signIns = screen.getAllByRole("link").filter((a) => /^sign in$/i.test(a.textContent ?? ""));
    expect(signIns).toHaveLength(1);
  });

  it("ships no button that would need JavaScript", () => {
    // If a control ever comes back, scripts/render.mjs is stripping the only
    // runtime that could have handled it -- so the page would look fine and
    // do nothing.
    render(<LandingPage />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("links to the source, and only ever to that repo", () => {
    render(<LandingPage />);
    const external = screen
      .getAllByRole("link")
      .filter((a) => a.getAttribute("href")?.startsWith("http"))
      .filter((a) => !/^sign in$/i.test(a.textContent ?? ""));
    expect(external.length).toBeGreaterThan(0);
    external.forEach((a) =>
      expect(a.getAttribute("href")).toBe("https://github.com/DJG3DK/tektonix"),
    );
  });

  it("opens external links safely", () => {
    // target=_blank without rel=noopener hands the opened page a reference
    // back to this one.
    render(<LandingPage />);
    screen
      .getAllByRole("link")
      .filter((a) => a.getAttribute("target") === "_blank")
      .forEach((a) => expect(a.getAttribute("rel")).toMatch(/noopener/));
  });

  it("states the Node floor as 24, not a retired LTS", () => {
    // Node 20 left maintenance in April 2026; install.sh and CI pin 24.
    render(<LandingPage />);
    expect(document.body.textContent).toMatch(/Node 24\+/);
    expect(document.body.textContent).not.toMatch(/Node 20\+/);
  });

  it("describes the licence as source-available, never as open source", () => {
    // PolyForm Noncommercial is not an OSI licence, and the README says so.
    render(<LandingPage />);
    expect(screen.getByText(/source available under polyform noncommercial/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/open source/i);
  });

  it("marks the review gate as the stage that can send work back", () => {
    const { container } = render(<LandingPage />);
    expect(container.querySelector(".lp-pipeline .is-gate")).toBeTruthy();
  });

  it("gives every screenshot alt text", () => {
    render(<LandingPage />);
    screen.getAllByRole("img").forEach((img) => {
      expect(img.getAttribute("alt")).toBeTruthy();
    });
  });

  it("lazy-loads the images below the fold", () => {
    // Eight screenshots eagerly loaded would compete with the hero.
    const { container } = render(<LandingPage />);
    expect(container.querySelectorAll('img[loading="lazy"]').length).toBeGreaterThan(0);
  });
});
