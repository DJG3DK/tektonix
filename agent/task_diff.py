"""Live diff of a project workspace against its base branch.

Feeds the dashboard's diff panel two ways:
  * WHILE a task runs -- polled every few seconds, so the operator can watch
    the agent's edits land file by file (committed or not).
  * WHEN a task parks on pending_merge_approval -- the final-look review
    before the operator lets it merge.

Everything here shells out to git inside the workspace root and parses the
output into per-file entries the frontend can shade. No path from the request
ever reaches a command line: the only inputs are the repo name (resolved
through PROJECTS) and git's own output.
"""

from __future__ import annotations

import asyncio
import os

from agent.config import PROJECTS

# A single file's patch beyond this is elided (the header row still shows the
# file and its +/- counts). Keeps one generated lockfile from turning the
# panel into a 2MB payload.
MAX_PATCH_CHARS = 60_000
MAX_UNTRACKED_BYTES = 200_000


# audit M-31: this endpoint is polled every few seconds while a task runs, so a
# hung git process would pile up. Every other subprocess spawn in the codebase
# wraps a wait_for; this one didn't.
_GIT_TIMEOUT_S = 20
# Cap how many untracked files get an individual `git diff --no-index` spawn --
# a stray node_modules / build dir otherwise means thousands of process spawns
# and an unbounded response. Files past the cap are reported, not diffed.
MAX_UNTRACKED_FILES = 200


async def _git(repo_root: str, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_root, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_S)
    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return 1, f"(git {' '.join(args)[:60]} timed out after {_GIT_TIMEOUT_S}s)"
    return proc.returncode or 0, out.decode(errors="replace")


def split_patch(patch_text: str) -> dict[str, str]:
    """Splits one combined `git diff` output into {path: file_patch}.

    Keyed by the NEW path (b/...) so renames land under the name the reviewer
    will see going forward.
    """
    files: dict[str, str] = {}
    current: list[str] | None = None
    current_path: str | None = None
    for line in patch_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                files[current_path] = "".join(current or [])
            # `diff --git a/x b/x` -- take the b/ side, handling spaces by
            # splitting on ' b/' from the right.
            try:
                current_path = line.rstrip("\n").rsplit(" b/", 1)[1]
            except IndexError:
                current_path = line.rstrip("\n")
            current = [line]
        elif current is not None:
            current.append(line)
    if current_path is not None:
        files[current_path] = "".join(current or [])
    return files


def parse_numstat(numstat_text: str) -> dict[str, tuple[int | None, int | None]]:
    """{path: (additions, deletions)}; None for binary files (git prints '-')."""
    out: dict[str, tuple[int | None, int | None]] = {}
    for line in numstat_text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s, path = parts[0], parts[1], parts[-1]
        # rename lines look like "old => new" or "{a => b}/rest" -- keep the
        # resolved new path form git already prints in the last field.
        adds = None if add_s == "-" else int(add_s)
        dels = None if del_s == "-" else int(del_s)
        out[path] = (adds, dels)
    return out


def _file_entries(numstat: str, patch: str) -> list[dict]:
    counts = parse_numstat(numstat)
    patches = split_patch(patch)
    files = []
    for path, (adds, dels) in counts.items():
        text = patches.get(path, "")
        truncated = len(text) > MAX_PATCH_CHARS
        files.append({
            "path": path,
            "additions": adds,
            "deletions": dels,
            "binary": adds is None,
            "untracked": False,
            "patch": "" if truncated else text,
            "truncated": truncated,
        })
    return files


async def _branch_diff(root: str, repo: str, branch: str, tip: str, base_ref: str) -> dict:
    """A task branch's committed work, read from git rather than the tree.

    For a task whose workspace another task has since used: the shared
    worktree then holds someone else's checkout, and diffing it showed the
    operator the wrong task's changes on the very screen where they approve a
    merge.
    """
    rc_mb, merge_base = await _git(root, "merge-base", tip, base_ref)
    base = merge_base.strip() if rc_mb == 0 and merge_base.strip() else base_ref
    _, numstat = await _git(root, "diff", "--numstat", base, tip)
    _, patch = await _git(root, "diff", "--patch", "--no-color", base, tip)
    files = sorted(_file_entries(numstat, patch), key=lambda f: f["path"])
    return {
        "repo": repo, "base": base, "head": tip, "branch": branch, "files": files,
        "total_additions": sum(f["additions"] or 0 for f in files),
        "total_deletions": sum(f["deletions"] or 0 for f in files),
    }


async def collect_task_diff(repo: str, base_ref: str = "main", task_branch: str | None = None) -> dict:
    """Everything the workspace holds that `base_ref` does not -- committed
    AND uncommitted, plus untracked files -- as one structured payload.

    With `task_branch`, and the workspace NOT on that branch, the branch's
    committed work instead (see _branch_diff)."""
    project = PROJECTS.get(repo)
    if not project:
        raise KeyError(f"unknown repo {repo!r}")
    root = project["sandbox"]

    rc, head = await _git(root, "rev-parse", "HEAD")
    rc_b, branch = await _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if task_branch and (rc_b != 0 or branch.strip() != task_branch):
        rc_tb, tip = await _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{task_branch}")
        if rc_tb == 0 and tip.strip():
            return await _branch_diff(root, repo, task_branch, tip.strip(), base_ref)

    # Diff against the BRANCH POINT, not the base branch's tip. The workspace
    # worktree can sit behind main between tasks (it's only fast-forwarded when
    # a task starts), and diffing a behind-HEAD worktree against `main` shows
    # main's own recent history in reverse -- 133 phantom files on an idle
    # workspace when this was first probed. merge-base(HEAD, main) is where
    # this line of work actually forked, so an idle workspace reads as zero
    # and a task branch shows exactly the agent's changes.
    rc_mb, merge_base = await _git(root, "merge-base", "HEAD", base_ref)
    if rc_mb == 0 and merge_base.strip():
        base_ref = merge_base.strip()

    # Working tree (committed + uncommitted) vs base, in one pass each for
    # numstat and patches.
    _, numstat = await _git(root, "diff", "--numstat", base_ref)
    _, patch = await _git(root, "diff", "--patch", "--no-color", base_ref)
    files = _file_entries(numstat, patch)

    # Untracked files are invisible to `git diff <ref>` but are real work the
    # agent produced -- synthesize an all-additions patch for each.
    _, untracked = await _git(root, "ls-files", "--others", "--exclude-standard")
    untracked_paths = untracked.splitlines()
    untracked_overflow = max(0, len(untracked_paths) - MAX_UNTRACKED_FILES)
    for path in untracked_paths[:MAX_UNTRACKED_FILES]:
        full = os.path.join(root, path)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > MAX_UNTRACKED_BYTES:
            files.append({"path": path, "additions": None, "deletions": None,
                          "binary": False, "untracked": True, "patch": "",
                          "truncated": True})
            continue
        # --no-index exits 1 when the files differ; that's success here.
        _, ptext = await _git(root, "diff", "--no-color", "--no-index", "/dev/null", full)
        adds = sum(1 for line in ptext.splitlines()
                   if line.startswith("+") and not line.startswith("+++"))
        files.append({
            "path": path,
            "additions": adds,
            "deletions": 0,
            "binary": "\x00" in ptext,
            "untracked": True,
            "patch": ptext if len(ptext) <= MAX_PATCH_CHARS else "",
            "truncated": len(ptext) > MAX_PATCH_CHARS,
        })

    files.sort(key=lambda f: f["path"])
    # audit M-31: if untracked files were capped, add one marker entry so the UI
    # can show the diff is incomplete rather than silently dropping them.
    if untracked_overflow:
        files.append({"path": f"({untracked_overflow} more untracked files not shown)",
                      "additions": None, "deletions": None, "binary": False,
                      "untracked": True, "patch": "", "truncated": True})
    return {
        "repo": repo,
        "base": base_ref,
        "head": head.strip() if rc == 0 else None,
        "branch": branch.strip() if rc_b == 0 else None,
        "files": files,
        "total_additions": sum(f["additions"] or 0 for f in files),
        "total_deletions": sum(f["deletions"] or 0 for f in files),
    }


# ── Operator edits ──────────────────────────────────────────────────────────
# The final-look panel lets the operator fix something by hand. These are the
# read half and the path rule; verify_and_ship applies the write, inside the
# task's own run and so under its project lock.

MAX_EDIT_FILE_BYTES = 1_000_000
MAX_EDIT_FILES = 50


def valid_edit_path(path: str) -> str | None:
    """The normalized repo-relative path, or None if it may not be edited.

    Checked on the way in (endpoint) and again on the way to disk
    (verify_and_ship, which also resolves symlinks against the real tree).
    """
    if not isinstance(path, str) or not path or len(path) > 1024 or "\x00" in path or "\\" in path:
        return None
    if path.startswith("/"):
        return None
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or parts[0] == ".git" or ".git" in parts:
        return None
    return "/".join(parts)


async def read_task_file(repo: str, task_branch: str, path: str, base_ref: str = "main") -> dict:
    """One file of a task, as its branch has it and as it was before the task.

    Read from git objects, never the working tree: the tree may belong to
    another task by now (see _branch_diff), and git takes the path as an
    object name, so nothing here touches the filesystem by it.
    """
    project = PROJECTS.get(repo)
    if not project:
        raise KeyError(f"unknown repo {repo!r}")
    clean = valid_edit_path(path)
    if clean is None:
        raise ValueError("that path cannot be edited")
    root = project["sandbox"]
    rc, tip = await _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{task_branch}")
    if rc != 0 or not tip.strip():
        raise LookupError("this task has no committed branch to edit")
    tip = tip.strip()
    rc_mb, mb = await _git(root, "merge-base", tip, base_ref)
    base = mb.strip() if rc_mb == 0 and mb.strip() else base_ref

    async def blob(ref: str) -> str | None:
        rc_s, size = await _git(root, "cat-file", "-s", f"{ref}:{clean}")
        if rc_s != 0:
            return None
        if int(size.strip() or 0) > MAX_EDIT_FILE_BYTES:
            raise ValueError(f"{clean} is larger than {MAX_EDIT_FILE_BYTES // 1000} KB -- edit it in the repo")
        rc_c, text = await _git(root, "show", f"{ref}:{clean}")
        if rc_c != 0:
            return None
        if "\x00" in text:
            raise ValueError(f"{clean} is binary")
        return text

    modified = await blob(tip)
    if modified is None:
        raise LookupError(f"{clean} does not exist on this task's branch")
    return {"path": clean, "original": await blob(base) or "", "modified": modified,
            "sha": tip, "base": base}
