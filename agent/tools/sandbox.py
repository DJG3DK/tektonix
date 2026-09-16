"""Sandboxed shell execution for the agent's own bash/write/edit/read
tools -- not for the deterministic checks/git operations (agent/tools/
checks.py, agent/tools/git.py), which stay on the host unchanged since
those are fixed commands our code runs, never LLM-chosen, and already have
no isolation concern (nothing an LLM decided goes into them).

Running bash/write/edit directly on the same host as the agent server,
against real repo checkouts, is a real risk: a context-injected or
badly-confused agent could otherwise read or touch anything outside its
intended repo -- another project's live secrets on the same host, this
project's own .env, other tooling config directories, etc. Docker
containers, bind-mounting only the target repo's sandbox checkout, close
that specific risk. Local
containers were chosen over a remote sandbox provider specifically to avoid
a new third-party account/credentials. Network access stays enabled
(package installs still need it) -- the isolation boundary here is
filesystem, not network, which is still a meaningful improvement over no
boundary at all, not a claim of complete sandboxing.

Same {"ok", "exit_code", "output"} / ShellTimeout contract as
agent/tools/shell.py's run_shell, so agent_tools.py's tool functions barely
change to use this instead.
"""

import asyncio
from agent import runtime_settings as _rs
import logging
import os
import uuid

from agent.tools.shell import ShellTimeout

logger = logging.getLogger("tektonix")

# Every `-v` source below is resolved by the DOCKER DAEMON, not by this
# process -- which is the same thing while the agent runs directly on the
# host, and stops being the same thing the moment it runs in a container with
# the host's Docker socket mounted (docs/roadmap-packaging.md, M1). There the
# sandbox containers are SIBLINGS created by the host daemon: a `-v` naming a
# path that exists only inside the agent container silently mounts an empty
# directory, and the task reads an empty repo with no error anywhere.
#
# So the mapping is explicit. AGENT_HOST_PATH_MAP is a colon-free,
# comma-separated list of `container_prefix=host_prefix` pairs; unset (the
# normal single-host install) means the identity mapping, which is exactly
# today's behaviour.
#
#   AGENT_HOST_PATH_MAP=/projects=C:\dev,/workspaces=D:\work
_HOST_PATH_MAP_ENV = "AGENT_HOST_PATH_MAP"


def _host_path_map() -> list[tuple[str, str]]:
    """Parsed prefix pairs, longest container prefix first so a nested mapping
    wins over the parent it sits under."""
    raw = os.environ.get(_HOST_PATH_MAP_ENV, "").strip()
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        container, host = entry.split("=", 1)
        container, host = container.strip().rstrip("/"), host.strip()
        if container and host:
            pairs.append((container, host.rstrip("/\\")))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def host_path(path: str) -> str:
    """The path the Docker daemon should be given for a bind mount.

    Identity unless AGENT_HOST_PATH_MAP says otherwise. Only whole path
    SEGMENTS match, so a prefix of "/projects" never rewrites "/projects-old".
    """
    if not path:
        return path
    for container, host in _host_path_map():
        if path == container:
            return host
        if path.startswith(container + "/"):
            rest = path[len(container) + 1:]
            sep = "\\" if ("\\" in host or ":" in host[:2]) else "/"
            return f"{host}{sep}{rest.replace('/', sep)}"
    return path


SANDBOX_IMAGE = "tektonix-sandbox:latest"  # built from docker/agent-sandbox/Dockerfile
SANDBOX_MEMORY_LIMIT = "2g"
SANDBOX_CPU_LIMIT = "2"
# audit M-10: cap process count to blunt a fork bomb, drop all Linux
# capabilities (git/npm/node need none), and forbid privilege escalation.
SANDBOX_PIDS_LIMIT = "512"


async def _kill_container(name: str) -> None:
    # `docker run --rm`'s own cleanup only fires on a normal exit -- killing
    # the `docker run` CLI process (the asyncio subprocess we actually hold
    # a handle to) does not reliably stop the container itself; without this,
    # a timed-out or cancelled command would leave the container (and
    # whatever it's running) orphaned, running detached on the host --
    # exactly the same class of bug run_shell's own _kill_process_group
    # exists to prevent, just one layer up (container instead of process
    # group). `docker kill` + `--rm` together tear it down completely.
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:  # noqa: BLE001 -- best-effort cleanup, never worth failing the caller over
        pass


def _mount_allow_roots(cwd: str) -> list[str]:
    """Host paths a bind mount for this workspace may point at.

    Both mount computations below read state OUT of the worktree -- a
    node_modules symlink, and the .git pointer file -- and the worktree is
    bind-mounted read-write for the agent. Left unchecked that lets the agent
    choose what the host mounts into its own container: `ln -s /home
    <ws>/node_modules` yields `-v /home:/home:ro`, and a rewritten .git
    pointer yields `-v /root:/root:ro`. That is read access to every other
    project's .env, this agent's own secrets and ~/.ssh -- exactly what the
    module docstring claims to prevent.

    So a target is only accepted if it resolves under the workspace itself or
    under the LIVE checkout of the project this workspace belongs to, both of
    which come from server-owned config rather than from the worktree.

    An unconfigured workspace (a test fixture, a one-off checkout) gets the
    workspace alone -- fail closed, since there is no trusted second path to
    compare against.
    """
    roots = [os.path.realpath(cwd)]
    try:
        from agent.config import PROJECTS
    except Exception:  # noqa: BLE001 -- config unavailable; workspace-only
        return roots
    real_cwd = os.path.realpath(cwd)
    for cfg in PROJECTS.values():
        if os.path.realpath(cfg.get("sandbox", "")) == real_cwd:
            live = cfg.get("live")
            if live:
                roots.append(os.path.realpath(live))
            break
    return roots


def _mount_target_allowed(target: str, allow_roots: list[str]) -> bool:
    real = os.path.realpath(target)
    return any(real == r or real.startswith(r + os.sep) for r in allow_roots)


def _node_modules_mounts(cwd: str) -> list[str]:
    """audit C-2: some project worktrees symlink their `node_modules` (and, in a
    monorepo, nested ones) to the LIVE checkout's node_modules OUTSIDE the
    worktree -- e.g. a Next.js project'
    `<worktree>/node_modules -> /home/a Next.js project/node_modules`. Mounting only
    the worktree leaves that symlink dangling inside the container, so `npm run
    lint` fails with "eslint: not found" even though it passes on the host. For
    each such symlink whose target lives outside `cwd`, bind-mount the target
    read-only at its own absolute path so the symlink resolves. Read-only: the
    check phase must never mutate the live install. Real (non-symlink)
    node_modules dirs are already covered by the /workspace mount and need
    nothing here.
    """
    mounts: list[str] = []
    seen: set[str] = set()
    real_cwd = os.path.realpath(cwd)
    allow_roots = _mount_allow_roots(cwd)
    for root, dirs, _files in os.walk(cwd):
        # don't descend into the store internals; one level of nesting is enough
        # for the monorepo app/package node_modules layout.
        depth = root[len(cwd):].count(os.sep)
        if depth >= 3:
            dirs[:] = []
        if "node_modules" in dirs:
            nm = os.path.join(root, "node_modules")
            if os.path.islink(nm):
                target = os.path.realpath(nm)
                # only mount if it points OUTSIDE the worktree and actually exists
                if target and target not in seen \
                        and os.path.isdir(target) \
                        and not target.startswith(real_cwd + os.sep):
                    # audit C2: the symlink is agent-writable, so its target is
                    # an agent-chosen mount unless it is checked against
                    # server-owned paths.
                    if not _mount_target_allowed(target, allow_roots):
                        logger.warning(
                            "refusing to mount %s for %s: outside this project's "
                            "workspace and live checkout", target, cwd)
                        continue
                    seen.add(target)
                    mounts += ["-v", f"{host_path(target)}:{target}:ro"]
            # never walk into a node_modules tree
            dirs.remove("node_modules")
    return mounts


async def run_shell_sandboxed(
    cmd: str,
    cwd: str,
    # None means "use the operator-set default"; an explicit value from the
    # call site still wins, so the long-running checks keep their own caps.
    timeout: int | None = None,
    extra_env: dict | None = None,
    network: str | None = None,
) -> dict:
    """Runs `cmd` inside an ephemeral Docker container with only `cwd`
    (the repo sandbox checkout) bind-mounted, read-write, at /workspace --
    no other host path is visible inside the container. No TTY, CI=true,
    hard timeout -- same non-interactive-by-construction discipline as
    run_shell (see that module's own docstring on why: a pty-based shell tool
    can hang forever on an unanswered interactive prompt).

    `network` maps to docker's --network (e.g. "none" for full network
    isolation -- used by the deterministic check runner, whose deps are already
    installed so it needs no egress; audit C-2/M-10). None keeps the default
    bridge, which is what the interactive `bash` tool uses.

    Returns {"ok": bool, "exit_code": int, "output": str} -- same shape as
    run_shell, so callers don't need to branch on which one they used.
    """
    container_name = f"lga-{uuid.uuid4().hex[:12]}"
    env_args = []
    for k, v in {"CI": "true", "DEBIAN_FRONTEND": "noninteractive", **(extra_env or {})}.items():
        env_args += ["-e", f"{k}={v}"]

    # The workspace is a git WORKTREE of the live repo (see the 2026-08-25
    # workspace migration), and a worktree's .git is not a directory but a one-
    # line file: `gitdir: <live>/.git/worktrees/<name>`. Mounting only the
    # worktree therefore broke every git command inside the container — status,
    # diff, log all died with "fatal: not a git repository" because the pointer
    # target didn't exist in the mount namespace. It went unnoticed because the
    # deterministic git operations (agent/tools/git.py) run on the HOST; only
    # LLM-issued `git ...` in the bash tool was affected.
    #
    # Fix: when cwd is a worktree, also mount the live repo's .git at the SAME
    # absolute path, read-only. Same path, because that's what the pointer file
    # names; read-only, because the sandbox runs unreviewed model commands and
    # must not be able to rewrite the live repository's history — commits happen
    # on the host through verify_and_ship, never in here. Read operations work;
    # an in-sandbox `git commit` fails on the read-only mount, which is correct.
    git_mount_args: list[str] = []
    dotgit = os.path.join(cwd, ".git")
    if os.path.isfile(dotgit):
        try:
            with open(dotgit) as fh:
                gitdir = fh.read().strip().removeprefix("gitdir:").strip()
            # <live>/.git/worktrees/<name>  ->  <live>/.git
            marker = f"{os.sep}worktrees{os.sep}"
            if marker in gitdir:
                main_git = gitdir[: gitdir.index(marker)]
                # audit C2: this path comes from a file inside the worktree,
                # which the agent can rewrite -- `gitdir: /root/x` would mount
                # /root read-only into the container. Same allow-root check the
                # node_modules mounts get.
                if os.path.isdir(main_git) and _mount_target_allowed(
                        main_git, _mount_allow_roots(cwd)):
                    git_mount_args = ["-v", f"{host_path(main_git)}:{main_git}:ro"]
                elif os.path.isdir(main_git):
                    logger.warning(
                        "refusing to mount git dir %s for %s: outside this "
                        "project's workspace and live checkout", main_git, cwd)
        except OSError:
            pass  # unreadable pointer file: run without git rather than not at all

    nm_mount_args = _node_modules_mounts(cwd)
    net_args = ["--network", network] if network else []

    docker_args = [
        "docker", "run", "--rm", "--name", container_name,
        "-v", f"{host_path(cwd)}:/workspace",
        *git_mount_args,
        *nm_mount_args,
        *net_args,
        "-w", "/workspace",
        "--memory", SANDBOX_MEMORY_LIMIT,
        "--cpus", SANDBOX_CPU_LIMIT,
        # audit M-10 hardening. --user is deliberately NOT set: the bind-mounted
        # worktree is root-owned on the host (pm2 runs as root), so a non-root
        # container user could not write to it. Network stays on the default
        # bridge (a dedicated network blocking link-local/RFC1918 and binding
        # llm-router to 127.0.0.1 is tracked as residual infra) -- but
        # MODEL_ROUTER_KEY is not passed in, so the reachable router surface is
        # authenticated, not open.
        "--pids-limit", SANDBOX_PIDS_LIMIT,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        *env_args,
        SANDBOX_IMAGE,
        "bash", "-c", cmd,
    ]
    proc = await asyncio.create_subprocess_exec(
        *docker_args,
        stdin=asyncio.subprocess.DEVNULL,  # no stdin at all -- matches run_shell's own reasoning
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        _timeout = timeout if timeout is not None else _rs.as_int("sandbox_command_timeout_s")
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_timeout)
    except TimeoutError:
        await _kill_container(container_name)
        await proc.wait()
        # _timeout, not timeout: the latter is None when the caller took the
        # operator default, which would report "timed out after None seconds".
        raise ShellTimeout(cmd, _timeout)
    except asyncio.CancelledError:
        # Same reasoning as run_shell's own CancelledError handler: the
        # operator's Stop button cancels the asyncio task driving the whole
        # run, and without explicit cleanup here the container (and
        # anything running inside it) would keep running orphaned.
        await _kill_container(container_name)
        await proc.wait()
        raise

    output = stdout.decode("utf-8", errors="replace")
    return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": output[-20_000:]}
