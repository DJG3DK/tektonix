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

  it("offers the newsletter, not a sign-in", () => {
    // A visitor has no account on somebody else's private console, so "sign
    // in" was never an action this page could offer them. The thing it can
    // offer is to tell them when something ships.
    render(<LandingPage />);
    expect(screen.queryByRole("link", { name: /^sign in$/i })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/agent\.tektonix\.io/);
    expect(screen.getByRole("link", { name: /get updates/i })).toHaveAttribute("href", "#newsletter");
  });

  it("collects a name and an address, and posts them as a plain form", () => {
    const { container } = render(<LandingPage />);
    const form = container.querySelector("form.lp-news-form") as HTMLFormElement;
    expect(form).toBeTruthy();
    // method and action, not a handler: there is no JavaScript on this page to
    // run a fetch, and a same-origin POST needs no CORS either.
    expect(form.getAttribute("method")).toBe("post");
    expect(form.getAttribute("action")).toBe("/newsletter/subscribe");
    expect(form.querySelector('input[name="name"][required]')).toBeTruthy();
    expect(form.querySelector('input[name="email"][type="email"][required]')).toBeTruthy();
  });

  it("has no control that would need JavaScript to do anything", () => {
    // A submit button inside a form is HTML doing its own job. Anything else
    // -- type=button, an onClick -- would render fine and then do nothing,
    // because scripts/render.mjs strips the only runtime that could handle it.
    const { container } = render(<LandingPage />);
    const buttons = [...container.querySelectorAll("button")];
    expect(buttons).toHaveLength(1);
    expect(buttons[0].getAttribute("type")).toBe("submit");
    expect(buttons[0].closest("form")).toBeTruthy();
  });

  it("says who the newsletter comes from, and how to leave it", () => {
    render(<LandingPage />);
    expect(screen.getAllByRole("link", { name: /danny@tektonix\.io/ }).length).toBeGreaterThan(0);
    expect(document.body.textContent).toMatch(/unsubscribe/i);
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
