"""Running the review service's checks in a sandbox, for the compose bundle.

On a host install the commit reviewer starts its own sandbox containers
(services/commit-reviewer/sandbox.js). In the bundle it cannot: it runs in a
container without the Docker socket, and giving it the socket would make it
host-root equivalent. Until this module existed it ran the checks IN ITS OWN
PROCESS instead -- agent-authored test files, executing in the container that
holds the review-control secret and can reach the merge endpoint. A test that
read /app/data/review_control_secret and POSTed `{"force": true}` merged its
own branch past the gate.

So the reviewer asks the agent, which already holds the socket and already
runs the same commands for its own `run_checks` tool, to start the container
for it. The agent does not take the reviewer's word for anything that decides
what the container can see:

  * the worktree must sit directly under REVIEW_WORKTREE_ROOT, be named for
    the project, and be a git worktree of THAT project's live checkout;
  * every extra mount must come from inside the live checkout, is forced
    read-only, and may only land where the reviewer's own layout puts it;
  * the image is chosen here, from server-owned config;
  * the hardening flags are this module's, never the request's.

The request is authenticated with the review-control secret, which the
reviewer already holds and the checks it starts never see.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from dataclasses import dataclass, field

from agent.tools import sandbox as sb

WORKTREE_ROOT_ENV = "REVIEW_WORKTREE_ROOT"

MAX_TIMEOUT_MS = 30 * 60 * 1000
MAX_OUTPUT_CHARS = 4 * 1024 * 1024
MAX_ARGS = 256

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_BASE_ENV = {"CI": "true", "DEBIAN_FRONTEND": "noninteractive", "LANG": "C.UTF-8"}


class RejectedRequest(ValueError):
    """The request asks for something this module will not start."""


@dataclass
class CheckRequest:
    project: str
    worktree: str
    cmd: str
    args: list[str] = field(default_factory=list)
    rel_dir: str = "."
    env: dict[str, str] = field(default_factory=dict)
    network: str = "none"
    stack: str | None = None
    mounts: list[tuple[str, str]] = field(default_factory=list)
    timeout_ms: int = 300_000


def worktree_root() -> str | None:
    raw = os.environ.get(WORKTREE_ROOT_ENV, "").strip()
    return os.path.realpath(raw) if raw else None


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _no_nul(value: str, what: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise RejectedRequest(f"{what} must be a string without NUL bytes")
    return value


def _project_live(project: str) -> tuple[str, dict]:
    from agent.config import PROJECTS  # noqa: PLC0415 -- the live dict, reloaded in place
    cfg = PROJECTS.get(project)
    if not isinstance(cfg, dict) or not cfg.get("live"):
        raise RejectedRequest(f"unknown project {project!r}")
    return os.path.realpath(cfg["live"]), cfg


def _checked_worktree(req: CheckRequest, live: str) -> str:
    root = worktree_root()
    if not root:
        raise RejectedRequest(f"{WORKTREE_ROOT_ENV} is not set on the agent")
    wt = os.path.realpath(_no_nul(req.worktree, "worktree"))
    if os.path.dirname(wt) != root:
        raise RejectedRequest("worktree is not directly inside the review worktree root")
    if not os.path.basename(wt).startswith(f"{req.project}-"):
        raise RejectedRequest("worktree is not named for this project")

    dotgit = os.path.join(wt, ".git")
    if os.path.islink(dotgit) or not os.path.isfile(dotgit):
        raise RejectedRequest("worktree has no git pointer file")
    try:
        with open(dotgit, encoding="utf-8") as fh:
            gitdir = fh.read(4096).strip().removeprefix("gitdir:").strip()
    except OSError as e:
        raise RejectedRequest(f"worktree git pointer is unreadable: {e}") from e
    worktrees = os.path.join(live, ".git", "worktrees")
    if not gitdir or not _inside(os.path.realpath(gitdir), worktrees) \
            or os.path.realpath(gitdir) == worktrees:
        raise RejectedRequest("worktree is not a git worktree of this project's live checkout")
    return wt


def _checked_rel_dir(rel_dir: str) -> str:
    rel = os.path.normpath(_no_nul(rel_dir or ".", "relDir")).replace(os.sep, "/")
    if rel.startswith("/") or rel == ".." or rel.startswith("../"):
        raise RejectedRequest("relDir must stay inside the worktree")
    return rel


def _checked_mounts(mounts: list[tuple[str, str]], live: str) -> list[tuple[str, str]]:
    """(real source, container target) for every extra mount, or refuse.

    Two shapes, the only two sandbox.js's mountArgs produces:

      * `<live>/<rel>` at `/workspace/<rel>` -- a declared dependency or
        read-only data directory, laid over the worktree's copy;
      * a path at its own absolute path -- live's .git (the worktree pointer
        names it) or a node_modules tree a worktree symlink resolves into.

    Anything else is a mount the reviewer never asks for, so it is refused
    rather than interpreted.
    """
    out: list[tuple[str, str]] = []
    live_git = os.path.join(live, ".git")
    for src, dst in mounts:
        _no_nul(src, "mount source")
        _no_nul(dst, "mount target")
        if not os.path.isabs(src) or not dst.startswith("/"):
            raise RejectedRequest("mount paths must be absolute")
        if any(c in s for s in (src, dst) for c in (":", ",")):
            raise RejectedRequest("mount paths may not contain ':' or ','")
        real = os.path.realpath(src)
        if not os.path.exists(real):
            continue
        if real == live or not _inside(real, live):
            raise RejectedRequest(f"mount source {src!r} is not inside the project's live checkout")
        norm_dst = os.path.normpath(dst)
        if norm_dst.startswith("/workspace/"):
            rel = norm_dst[len("/workspace/"):]
            if not rel or rel.startswith(".."):
                raise RejectedRequest(f"mount target {dst!r} is not inside /workspace")
            if os.path.realpath(os.path.join(live, rel)) != real:
                raise RejectedRequest(f"mount target {dst!r} does not match its source")
        elif norm_dst == real:
            if real != live_git and "node_modules" not in real.split(os.sep):
                raise RejectedRequest(f"same-path mount {src!r} is neither live's .git nor node_modules")
        else:
            raise RejectedRequest(f"mount target {dst!r} is not a location the reviewer uses")
        out.append((real, norm_dst))
    return out


def _image_and_env(req: CheckRequest, cfg: dict) -> tuple[str, dict]:
    """Same order as sandbox.js dockerArgs: the check's own stack, then the
    project's sandbox_image, then the project's stack, then the default."""
    stacks = sb._stack_images().get("stacks") or {}
    if req.stack:
        entry = stacks.get(req.stack) or {}
        return str(entry.get("image") or sb.SANDBOX_IMAGE), dict(entry.get("env") or {})
    entry = stacks.get(cfg.get("stack") or "") or {}
    image = cfg.get("sandbox_image") or entry.get("image") or sb.SANDBOX_IMAGE
    return str(image), dict(entry.get("env") or {})


def build_docker_argv(req: CheckRequest, container_name: str) -> tuple[list[str], str]:
    """The full `docker run` argv for one check, and the image it runs in.

    Pure apart from reading the filesystem to validate paths, so the
    hardening is asserted in tests rather than grepped for."""
    live, cfg = _project_live(_no_nul(req.project, "project"))
    wt = _checked_worktree(req, live)
    rel = _checked_rel_dir(req.rel_dir)

    cmd = _no_nul(req.cmd, "cmd")
    if not cmd or cmd.startswith("-") or len(cmd) > 256:
        raise RejectedRequest("cmd must be a program name")
    if not isinstance(req.args, list) or len(req.args) > MAX_ARGS:
        raise RejectedRequest("args must be a list of at most 256 strings")
    args = [_no_nul(a, "arg") for a in req.args]
    if req.network not in ("none", "bridge"):
        raise RejectedRequest("network must be 'none' or 'bridge'")

    image, toolchain_env = _image_and_env(req, cfg)
    env: dict[str, str] = {**_BASE_ENV, **toolchain_env}
    for k, v in (req.env or {}).items():
        if not isinstance(k, str) or not _ENV_KEY.match(k):
            raise RejectedRequest(f"env name {k!r} is not a plain identifier")
        env[k] = _no_nul(v, f"env {k}")

    mounts = _checked_mounts(req.mounts or [], live)

    argv = [
        "docker", "run", "--rm", "--name", container_name,
        "-v", f"{sb.host_path(wt)}:/workspace",
        *[a for src, dst in mounts for a in ("-v", f"{sb.host_path(src)}:{dst}:ro")],
        "--network", req.network,
        "-w", "/workspace" if rel == "." else f"/workspace/{rel}",
        "--memory", sb.SANDBOX_MEMORY_LIMIT,
        "--memory-swap", sb.SANDBOX_MEMORY_SWAP,
        "--cpus", sb.SANDBOX_CPU_LIMIT,
        "--pids-limit", sb.SANDBOX_PIDS_LIMIT,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--entrypoint", cmd,
        *[a for k, v in env.items() for a in ("-e", f"{k}={v}")],
        image,
        *args,
    ]
    return argv, image


def clamp_timeout_ms(value) -> int:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        ms = 300_000
    return max(1_000, min(ms, MAX_TIMEOUT_MS))


async def run_check(req: CheckRequest) -> dict:
    """Run one check; {ok, code, output, image} in sandbox.js's own shape."""
    name = f"rvw-{uuid.uuid4().hex[:12]}"
    argv, image = build_docker_argv(req, name)
    timeout_s = clamp_timeout_ms(req.timeout_ms) / 1000
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        return {"ok": False, "code": 1, "infrastructure": True, "image": image,
                "output": f"SETUP: the agent could not start docker: {e}"}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        await sb._kill_container(name)
        await proc.wait()
        return {"ok": False, "code": 124, "image": image,
                "output": f"timed out after {int(timeout_s)}s"}
    except asyncio.CancelledError:
        await sb._kill_container(name)
        await proc.wait()
        raise
    text = (out or b"").decode("utf-8", errors="replace")[-MAX_OUTPUT_CHARS:]
    return {"ok": proc.returncode == 0, "code": proc.returncode, "output": text, "image": image}
