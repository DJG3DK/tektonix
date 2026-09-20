/**
 * What a merge will actually do, said out loud during onboarding.
 *
 * The pm2 path only ever fitted one way of running things, and a project that
 * is not a pm2 app got a merge and nothing else -- with no sentence anywhere
 * telling anybody that. The silent case is the one that needs saying.
 */
import { describe, expect, it } from "vitest";

/* Mirrors the helper in ProjectsPanel. */
function deployOutcome(apps: Record<string, boolean>, restarts: Record<string, boolean>): string {
  const pm2 = Object.entries(apps).filter(([, on]) => on).map(([v]) => v);
  const cmds = Object.entries(restarts).filter(([, on]) => on).map(([v]) => v);
  if (!pm2.length && !cmds.length) {
    return "merge only — nothing on this machine is restarted";
  }
  const parts = [];
  if (pm2.length) parts.push(`restart ${pm2.join(", ")}`);
  if (cmds.length) parts.push(cmds.join(", "));
  return `merge, then ${parts.join(" and ")}`;
}

describe("the outcome onboarding promises", () => {
  it("says merge only when this machine runs nothing", () => {
    // The common case for a repository somebody is only sending pull requests
    // to, and the one that used to be silent.
    expect(deployOutcome({}, {})).toMatch(/merge only/);
  });

  it("does not count a candidate that was offered and left unticked", () => {
    expect(deployOutcome({ "some-app": false }, { "docker compose up -d": false }))
      .toMatch(/merge only/);
  });

  it("names the pm2 apps it will restart", () => {
    expect(deployOutcome({ api: true, worker: true }, {})).toBe("merge, then restart api, worker");
  });

  it("names a compose or systemd command in full", () => {
    expect(deployOutcome({}, { "docker compose up -d --build": true }))
      .toBe("merge, then docker compose up -d --build");
  });

  it("reads correctly when a project has both", () => {
    expect(deployOutcome({ api: true }, { "systemctl restart worker": true }))
      .toBe("merge, then restart api and systemctl restart worker");
  });
});
