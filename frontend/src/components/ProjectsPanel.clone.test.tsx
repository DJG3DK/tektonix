/**
 * Onboarding a repository that exists only on GitHub.
 *
 * The wizard has always taken a path. A URL is not a path on this machine, so
 * it has to be cloned first -- and the rest of the wizard must not know that
 * happened, which is the whole shape of the feature.
 */
import { describe, expect, it } from "vitest";
import { looksLikeGitHubUrl } from "../api";

describe("what the wizard treats as a repository to clone", () => {
  it("recognises the URLs a person copies out of the address bar", () => {
    for (const url of [
      "https://github.com/owner/repo",
      "https://github.com/owner/repo.git",
      "https://github.com/owner/repo/",
      "https://www.github.com/owner/repo",
      "git@github.com:owner/repo.git",
      "ssh://git@github.com/owner/repo",
    ]) {
      expect(looksLikeGitHubUrl(url), url).toBe(true);
    }
  });

  it("leaves anything path-shaped alone", () => {
    // `owner/repo` is deliberately NOT here: it is the same string as
    // `relative/path`, and switching the wizard to Clone on that guess is how
    // you try to clone a directory name. The API accepts the bare form when
    // the operator asks for it explicitly; the wizard does not infer it.
    for (const text of [
      "/srv/live/thing",
      "./relative",
      "~/code/thing",
      "owner/repo",
      "src/components",
      "https://gitlab.com/owner/repo",
      "",
    ]) {
      expect(looksLikeGitHubUrl(text), text).toBe(false);
    }
  });
});
