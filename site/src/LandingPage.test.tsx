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

  it("says who the newsletter comes from, and what is done with an address", () => {
    render(<LandingPage />);
    expect(screen.getAllByRole("link", { name: /danny@tektonix\.io/ }).length).toBeGreaterThan(0);
    // The signup form no longer promises an unsubscribe. Nothing has been sent
    // yet, so there is nothing to leave -- the promise belongs in the first
    // issue, which carries the link the stored token is minted for.
    expect(document.body.textContent).toMatch(/never sold, never shared/i);
  });

  it("links off-site only ever into that one repo", () => {
    // The point is that this page never sends anyone somewhere else, not that
    // every link is the repo root -- the download goes to /releases/latest.
    // Anchored with the trailing slash so a lookalike host cannot satisfy it.
    render(<LandingPage />);
    const external = screen
      .getAllByRole("link")
      .filter((a) => a.getAttribute("href")?.startsWith("http"))
      .filter((a) => !/^sign in$/i.test(a.textContent ?? ""));
    expect(external.length).toBeGreaterThan(0);
    external.forEach((a) => {
      const href = a.getAttribute("href") ?? "";
      expect(
        href === "https://github.com/DJG3DK/tektonix" ||
          href.startsWith("https://github.com/DJG3DK/tektonix/"),
      ).toBe(true);
    });
  });

  it("offers the release, not a version that will go stale here", () => {
    // A direct asset URL has to name the file, the file names its version, and
    // this page would then offer an old one from the next release onwards.
    render(<LandingPage />);
    const download = screen.getByRole("link", { name: /download the latest release/i });
    expect(download).toHaveAttribute("href", "https://github.com/DJG3DK/tektonix/releases/latest");
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

  it("does not claim the Docker bundle includes the review services", () => {
    // docker-compose.yml ships postgres, router and agent. Saying the review
    // services are "as one stack" is the headline differentiator sold on a
    // path that does not run them.
    render(<LandingPage />);
    expect(document.body.textContent).not.toMatch(/review services as one stack/i);
    expect(document.body.textContent).toMatch(/review services are not in this bundle yet/i);
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
