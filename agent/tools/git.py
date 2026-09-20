import os
import hashlib
import re
import asyncio

from agent.tools.shell import run_shell


class UntrustedGitDirError(Exception):
    """The worktree's .git pointer no longer names the repo it should."""


# Every git command this module runs executes ON THE HOST, with cwd set to a
# worktree the agent can write to. Two consequences drive everything below.
#
# 1. HOOKS. A worktree's `.git` is a one-line pointer FILE, not a directory,
#    and it sits inside the tree the agent writes to. Rewriting it re-points
#    git at a git dir the agent controls -- hooks included -- so the host-side
#    `git commit` would execute an agent-authored pre-commit hook as the
#    server user (root, under the shipped pm2 config). Writing the pointer
#    matched no approval marker either: the gate looked for ".git/" WITH the
#    slash, and the pointer is the bare file ".git".
#
#    `core.hooksPath=/dev/null` is set on EVERY invocation rather than only on
#    commit, because checkout, merge and rebase run hooks too, and it holds
#    regardless of which git dir the pointer resolves to.
#
# 2. IDENTITY. Disabling hooks stops code execution but not misdirection: a
#    rewritten pointer can still aim commits at a different repository. So
#    when the workspace belongs to a configured project, the pointer is
#    checked against that project's own git dir before anything runs.
_GIT = "git -c core.hooksPath=/dev/null"


def _trusted_git_dir_error(repo_root: str) -> str | None:
    """Return a message if this workspace's .git pointer looks tampered with.

    Returns None when the workspace is not a configured project sandbox (the
    test fixtures and one-off checkouts), since there is no server-owned path
    to compare against. Hook execution is already disabled unconditionally, so
    this is the integrity half, not the code-execution half.
    """
    dotgit = os.path.join(repo_root, ".git")
    if not os.path.isfile(dotgit):
        return None  # a normal repo with a real .git directory
    try:
        from agent.config import PROJECTS
    except Exception:  # noqa: BLE001 -- config unavailable (tests); nothing to compare
        return None

    live = None
    real_root = os.path.realpath(repo_root)
    for cfg in PROJECTS.values():
        if os.path.realpath(cfg.get("sandbox", "")) == real_root:
            live = cfg.get("live")
            break
    if not live:
        return None

    try:
        pointer = open(dotgit).read().strip()
    except OSError as e:
        return f"unreadable .git pointer: {e}"
    gitdir = pointer.removeprefix("gitdir:").strip()
    expected = os.path.realpath(os.path.join(live, ".git"))
    if not os.path.realpath(gitdir).startswith(expected + os.sep):
        return (f"the workspace's .git pointer names {gitdir!r}, which is outside "
                f"{expected!r}. Refusing to run git against it -- this is what a "
                f"rewritten pointer looks like.")
    return None


async def _git(cmd: str, repo_root: str, timeout: int = 30) -> dict:
    """Run one git command with hooks disabled and the pointer verified."""
    problem = _trusted_git_dir_error(repo_root)
    if problem is not None:
        return {"ok": False, "output": f"refusing to run git: {problem}"}
    return await run_shell(f"{_GIT} {cmd}", repo_root, timeout=timeout)


async def git_status(repo_root: str) -> str:
    r = await _git("status --short", repo_root, timeout=30)
    return r["output"]


async def git_diff(repo_root: str, staged: bool = False) -> str:
    """Includes newly created (untracked) files, not just changes to tracked
    ones. Plain `git diff` silently omits untracked files entirely, which
    would make a genuinely new file look like no change happened at all.
    `git add -N` (intent-to-add: stages the path, not the content) makes new
    files show up in `git diff` normally without actually staging their
    content -- this is not intended as a real `git add`.
    """
    await _git("add -A -N", repo_root, timeout=30)
    cmd = "diff --staged" if staged else "diff"
    r = await _git(cmd, repo_root, timeout=30)
    return r["output"]


async def sync_workspace_to_base(repo_root: str, base_ref: str = "main") -> dict:
    """Move an IDLE workspace to the current live tip before a fresh task edits.

    Why this exists: the workspace worktree only ever sat where the previous
    task left it, while direct pushes land on live main continuously. A fresh
    task then edited a stale tree, and ensure_task_branch -- which runs at
    COMMIT time, when the agent's own uncommitted edits have already dirtied
    the tree -- took its dirty-tree branch-from-HEAD path every single time,
    silently pinning the task branch to the stale base. First observed live
    2026-08-26: a task branched 9 commits behind main, its (review-approved,
    operator-approved) merge then failed --ff-only with "diverging branches",
    and the agent had spent the whole task editing week-old code.

    Only acts on a CLEAN tree -- a dirty tree means a resumed or concurrent
    task owns the workspace, and moving the base under real work is exactly
    the kind of silent damage this module exists to prevent. Detached
    checkout, because `main` itself is checked out in the live worktree and
    git (correctly) refuses to check a branch out twice.
    """
    status = await _git("status --porcelain", repo_root, timeout=15)
    if not status["ok"]:
        return {"ok": False, "synced": False, "reason": f"status failed: {status['output'][:200]}"}
    if status["output"].strip():
        return {"ok": True, "synced": False, "reason": "tree dirty -- workspace belongs to in-flight work"}
    r = await _git(f"checkout --detach {base_ref}", repo_root, timeout=30)
    if not r["ok"]:
        return {"ok": False, "synced": False, "reason": r["output"][:300]}
    sha = await _git("rev-parse --short HEAD", repo_root, timeout=15)
    return {"ok": True, "synced": True, "base": sha["output"].strip() if sha["ok"] else base_ref}


async def ensure_task_branch(repo_root: str, task_id: str, base_ref: str = "main") -> dict:
    """Put the agent's workspace on a per-task branch before committing.

    The workspace is a git worktree of the live repo, so this branch is created
    directly in live's own object store -- there is no clone to keep in sync and
    no remote to fetch through. The reviewer and the merge endpoint read the
    branch as a plain local ref.

    Two things this protects against, both observed:
      * committing to `main` made one ref serve two writers (the agent, and the
        refresh cron that fast-forwarded it), and
      * without a branch there was no stable review unit, so the reviewer
        inferred one by comparing HEADs -- which produced inverted diffs and two
        false `blocking` findings on live trading safety settings.

    Idempotent: on resume the branch already exists and is checked out, and this
    returns without touching the tree.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(task_id)).strip("-.") or "task"
    branch = f"agent/{safe}"

    cur = await _git("rev-parse --abbrev-ref HEAD", repo_root, timeout=15)
    if cur["ok"] and cur["output"].strip() == branch:
        return {"ok": True, "branch": branch, "switched": False}

    # A fresh task should start from the current live tip rather than from
    # wherever the previous task left the workspace. Only safe when the tree is
    # clean -- if there is uncommitted work, branch from HEAD so it survives,
    # and say so rather than discarding it.
    status = await _git("status --porcelain", repo_root, timeout=15)
    dirty = bool(status["ok"] and status["output"].strip())

    if dirty:
        cmd = f"checkout -B {branch}"
    else:
        cmd = f"checkout -B {branch} {base_ref}"

    r = await _git(cmd, repo_root, timeout=30)
    if not r["ok"] and not dirty:
        # base_ref may not exist (unusual default branch name); fall back to HEAD.
        r = await _git(f"checkout -B {branch}", repo_root, timeout=30)

    return {
        "ok": r["ok"],
        "branch": branch,
        "switched": True,
        "from_base": not dirty,
        "output": r["output"],
    }


# audit M-12: git_commit used `git add -A`, and the sandboxed bash can create
# arbitrary files in the worktree -- every one landed in the commit that goes to
# review and, on approval, into the live repo (core.excludesFile only covers
# .uploads/). These directories/suffixes are never legitimate commit content;
# their presence in the pending set is a build/dep artifact that must be cleaned
# (or gitignored) before committing, not silently shipped.
_COMMIT_DENY_DIRS = (
    "node_modules/", "dist/", "build/", ".next/", ".venv/", "venv/",
    "__pycache__/", ".pytest_cache/", "coverage/", ".mypy_cache/",
    "target/", ".turbo/", ".cache/",
)
_COMMIT_DENY_SUFFIXES = (".pyc", ".log", ".tmp")
_MAX_COMMIT_FILES = 500


def _porcelain_paths(porcelain: str) -> list[str]:
    paths = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        # rename entries are "old -> new"; take the destination
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip().strip('"'))
    return paths


async def git_commit(repo_root: str, message: str, files: list[str] | None = None) -> dict:
    if not files:
        # audit M-12: guard the blanket `git add -A`. Inspect what would be
        # staged and refuse obvious artifacts / a runaway file count, so the
        # agent cleans up rather than committing junk into the live repo.
        status = await _git("status --porcelain", repo_root, timeout=30)
        if status["ok"]:
            pending = _porcelain_paths(status["output"])
            denied = [pth for pth in pending
                      if any(d in pth for d in _COMMIT_DENY_DIRS)
                      or pth.endswith(_COMMIT_DENY_SUFFIXES)]
            if denied:
                sample = ", ".join(denied[:10])
                return {"ok": False, "output": (
                    f"refusing to commit {len(denied)} build/dependency artifact(s) that "
                    f"should be gitignored or removed first: {sample}"
                    + (" ..." if len(denied) > 10 else ""))}
            if len(pending) > _MAX_COMMIT_FILES:
                return {"ok": False, "output": (
                    f"refusing to commit {len(pending)} files at once (limit {_MAX_COMMIT_FILES}) -- "
                    "this usually means an un-ignored directory got created; clean it up or "
                    "pass an explicit file list.")}
    add_cmd = f"add {' '.join(files)}" if files else "add -A"
    add = await _git(add_cmd, repo_root, timeout=30)
    if not add["ok"]:
        return {"ok": False, "output": add["output"]}
    # Message via a temp file, not -m "...", so multi-line messages with
    # quotes/special chars can never break the shell invocation.
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(message)
        msg_path = f.name
    try:
        commit = await _git(f"commit --no-verify -F {msg_path}", repo_root, timeout=30)
    finally:
        Path(msg_path).unlink(missing_ok=True)
    return {"ok": commit["ok"], "output": commit["output"]}


async def current_sha(repo_root: str) -> str:
    r = await _git("rev-parse HEAD", repo_root, timeout=15)
    return r["output"].strip()


async def sha_in_repo(repo_root: str, sha: str) -> bool:
    """True iff `sha` is an ancestor of (or equal to) `repo_root`'s HEAD --
    used by verify_and_ship to detect that the review service's
    auto-merge-on-READY already merged the pending commit to the live repo
    (the two are separate paths to the same merge and can race, which would
    otherwise nudge an already-finished task for a verdict that auto-merge
    had already consumed).
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_root, "merge-base", "--is-ancestor", sha, "HEAD",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


async def rebase_onto_base(repo_root: str, base_ref: str = "main") -> dict:
    """Move the task branch onto the live tip when the tip has moved under it.

    The merge into live is `--ff-only`, deliberately: it is what guarantees the
    thing that merges is the thing that was reviewed. The cost is that a branch
    whose base moved cannot land at all -- a reviewed, approved commit with
    nowhere to go, at the end of a task somebody already paid for. The trigger
    is ordinary: a push to main, or another merge, while a task is running.

    So the branch moves instead of the rule. Called after the commit and before
    the review, which is the one moment the tree is clean AND the agent is still
    running to fix a conflict if there is one.

    `patch_identical` is the interesting part. A rebase rewrites every sha, and
    the reviewer discards its history when the sha it reviewed is no longer an
    ancestor -- so a naive rebase costs a full review round every time someone
    else pushes. Comparing the branch's own diff before and after answers
    whether that round would learn anything: if the patch is byte-identical,
    the review that exists still describes this change. Context lines shifting
    because main edited nearby is enough to make it differ, and re-reviewing
    then is the right, conservative answer.

    Returns, and never raises:
      moved=False                 the base is still where the branch forked. The
                                  common case, and it costs two rev-parses.
      moved, rebased, ok=True     moved, and the branch now sits on the new tip.
      conflicts=[...], ok=False   moved, and the two changes disagree. The
                                  rebase is ABORTED before returning, so the
                                  branch is exactly as it was and the caller can
                                  hand the file list to whoever can resolve it.
    """
    base_tip = await _git(f"rev-parse {base_ref}", repo_root, timeout=15)
    if not base_tip["ok"]:
        # No such ref. Not this function's problem to diagnose, and not a
        # reason to fail a task: the branch is fine where it is.
        return {"ok": True, "moved": False, "reason": f"{base_ref} does not resolve"}
    tip = base_tip["output"].strip()

    mb = await _git(f"merge-base HEAD {base_ref}", repo_root, timeout=15)
    if not mb["ok"]:
        return {"ok": True, "moved": False, "reason": "no merge base"}
    fork = mb["output"].strip()

    if fork == tip:
        return {"ok": True, "moved": False, "base": tip}

    before = await _git(f"diff {fork}..HEAD", repo_root, timeout=60)
    patch_before = _digest(before["output"] if before["ok"] else "")

    r = await _git(f"rebase {base_ref}", repo_root, timeout=120)
    if not r["ok"]:
        # Collect the file list BEFORE aborting -- afterwards there is nothing
        # left to ask.
        conflicted = await _git("diff --name-only --diff-filter=U", repo_root, timeout=15)
        files = [f for f in (conflicted["output"] or "").splitlines() if f.strip()]
        await _git("rebase --abort", repo_root, timeout=60)
        return {
            "ok": False,
            "moved": True,
            "rebased": False,
            "conflicts": files,
            "base": tip,
            "output": r["output"][:1000],
        }

    after = await _git(f"diff {tip}..HEAD", repo_root, timeout=60)
    patch_after = _digest(after["output"] if after["ok"] else "")

    return {
        "ok": True,
        "moved": True,
        "rebased": True,
        "patch_identical": patch_before == patch_after and bool(patch_before),
        "base": tip,
        "previous_base": fork,
    }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


async def fetch_base_from_origin(live_root: str, base_ref: str = "main") -> dict:
    """Bring the live checkout's base branch up to date with `origin`.

    The local copy is a cache of GitHub, not the source of truth. Without this
    a task branches from whatever the machine last saw, does good work against
    it, and finds out at merge time -- which is the expensive end. The rebase
    path handles that when it happens; this makes it happen less.

    Fast-forward only, and deliberately: if local `main` has commits that are
    not on the remote, somebody committed here directly and this function has
    no business rewriting or merging that. It says so and leaves it, and the
    task proceeds from the local tip exactly as before.

    Called against the LIVE repo rather than the workspace, because the
    workspace is a worktree of it and shares its refs -- fetching once updates
    both. Never raises: no remote, no network and an unreachable host are all
    ordinary, and none of them is a reason to fail a task that could run
    offline perfectly well.
    """
    remotes = await _git("remote", live_root, timeout=15)
    if not remotes["ok"] or "origin" not in remotes["output"].split():
        return {"ok": True, "fetched": False, "reason": "no origin remote"}

    f = await _git(f"fetch --quiet origin {base_ref}", live_root, timeout=120)
    if not f["ok"]:
        return {"ok": True, "fetched": False,
                "reason": f"fetch failed: {f['output'][:200]}"}

    local = await _git(f"rev-parse {base_ref}", live_root, timeout=15)
    remote = await _git("rev-parse FETCH_HEAD", live_root, timeout=15)
    if not (local["ok"] and remote["ok"]):
        return {"ok": True, "fetched": True, "advanced": False, "reason": "could not compare"}
    local_sha, remote_sha = local["output"].strip(), remote["output"].strip()
    if local_sha == remote_sha:
        return {"ok": True, "fetched": True, "advanced": False, "base": local_sha}

    # Only when the local branch is strictly behind. `--is-ancestor` answers
    # exactly that, and answers it about the commit graph rather than about
    # timestamps or counts.
    anc = await _git(f"merge-base --is-ancestor {base_ref} FETCH_HEAD", live_root, timeout=15)
    if not anc["ok"]:
        return {"ok": True, "fetched": True, "advanced": False,
                "diverged": True,
                "reason": f"local {base_ref} has commits origin does not; leaving it alone"}

    # Fast-forward the branch ref without touching the working tree: `main` is
    # checked out in the live worktree, and a task may be running in another
    # worktree of the same repo. `update-ref` moves the pointer only.
    head = await _git("rev-parse --abbrev-ref HEAD", live_root, timeout=15)
    if head["ok"] and head["output"].strip() == base_ref:
        up = await _git("merge --ff-only FETCH_HEAD", live_root, timeout=60)
    else:
        up = await _git(f"update-ref refs/heads/{base_ref} {remote_sha}", live_root, timeout=15)
    if not up["ok"]:
        return {"ok": True, "fetched": True, "advanced": False,
                "reason": f"could not fast-forward: {up['output'][:200]}"}
    return {"ok": True, "fetched": True, "advanced": True,
            "base": remote_sha, "previous": local_sha}
