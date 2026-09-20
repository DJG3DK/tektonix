/**
 * A task that shipped as a pull request.
 *
 * Nothing merged and nothing deployed: the work is sitting on GitHub waiting
 * for a person. The link is the outcome, so it has to be on the task rather
 * than inside a step log entry somebody has to go looking through.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

function PullRequestResult({ url }: { url?: string | null }) {
  if (!url) return null;
  return (
    <div className="task-pr">
      <span className="task-pr-label">Opened a pull request</span>
      <a className="task-pr-link" href={url} target="_blank" rel="noopener noreferrer">
        {url.replace(/^https:\/\/github\.com\//, "")}
      </a>
      <span className="task-pr-note">Nothing was merged — review and merge it on GitHub.</span>
    </div>
  );
}

describe("the pull request a shipped task leaves behind", () => {
  it("shows the link, and says plainly that nothing merged", () => {
    render(<PullRequestResult url="https://github.com/owner/repo/pull/12" />);
    const link = screen.getByRole("link", { name: "owner/repo/pull/12" });
    expect(link).toHaveAttribute("href", "https://github.com/owner/repo/pull/12");
    expect(screen.getByText(/nothing was merged/i)).toBeInTheDocument();
  });

  it("opens in a new tab without handing that tab a reference back", () => {
    render(<PullRequestResult url="https://github.com/owner/repo/pull/12" />);
    const link = screen.getByRole("link", { name: /pull\/12/ });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toMatch(/noopener/);
  });

  it("renders nothing at all for a task that merged normally", () => {
    const { container } = render(<PullRequestResult url={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
