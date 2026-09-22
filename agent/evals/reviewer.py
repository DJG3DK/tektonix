"""A second, isolated reviewer pair, for the duration of a run.

The eval measures first-pass review rate, so the real reviewer has to run --
a stub would be measuring the stub. But the live pair cannot be used: it reads
the live projects.json (which must never learn about a fixture), writes the
verdict state the dashboard reads, and appends to the usage log the reviewer
spend figure is summed from. Pointing an eval at it would corrupt all three.

So the run starts its own. Same code, same model, different ports and
different files, every one of them through an environment variable that
already existed or that agent/evals added for exactly this:

    AGENT_PROJECTS_JSON       only the fixtures are configured
    REVIEW_ONLY_PROJECTS_JSON only the fixtures are VISIBLE -- see below
    REVIEW_STATE_DIR          its own state.json / history.jsonl
    REVIEW_USAGE_LOG          its spend is not the dashboard's spend
    REVIEW_WORKTREE_ROOT      its checkouts are not beside the live ones
    REVIEW_SERVICE_PORT       }  a free pair, so a run can happen while the
    REVIEW_CONTROL_PORT       }  real reviewer is mid-review

REVIEW_ONLY_PROJECTS_JSON is the one that is not obvious, and the first smoke
run of this module is why it exists. Pointing AGENT_PROJECTS_JSON at the
fixtures is not enough on its own: the reviewer also merges in
builtin-projects.local.js, and a built-in-only project appears whether or not
projects.json mentions it. So the eval instance came up polling the
operator's real repositories -- and the moment one of those has a live task
branch, two reviewers are racing on the same repo, writing verdicts into
different state files, each unaware of the other's worktree.

It shares the model router and the review credentials, because those are the
point: an eval that reviewed with a different model would not be measuring
this agent.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from agent import paths

REVIEWER_JS = paths.REPO_ROOT / "services" / "commit-reviewer" / "reviewer.js"
DASHBOARD_JS = paths.REPO_ROOT / "services" / "agent-review" / "server.js"

# How long to wait for each service to answer its health route. The reviewer
# reads a handful of files and opens two listeners; if it is not up in this
# long it is not coming up, and the run should say so rather than spend an
# hour timing out one review at a time.
STARTUP_TIMEOUT_S = 45


class ReviewerError(RuntimeError):
    pass


def free_port() -> int:
    """A port nothing is listening on, chosen by the kernel.

    Racy in principle -- something else could take it between the close and
    the child's bind. Accepted: the alternative is a fixed second pair of
    ports, which collides with a concurrent run rather than with a hypothesis.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class EvalReviewer:
    service_port: int
    control_port: int
    state_dir: Path
    procs: list[subprocess.Popen]
    log_path: Path

    @property
    def env_overrides(self) -> dict[str, str]:
        """What the agent process must have set to talk to THIS pair.

        Returned rather than applied, because the agent side reads these at
        import time (agent/tools/review_gate.py) -- the runner sets them
        before importing, and getting that ordering wrong would silently
        review against the live service.
        """
        return {
            "REVIEW_SERVICE_PORT": str(self.service_port),
            "REVIEW_CONTROL_PORT": str(self.control_port),
        }


def _child_env(projects_json: Path, state_dir: Path,
               service_port: int, control_port: int) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "AGENT_PROJECTS_JSON": str(projects_json),
        "REVIEW_ONLY_PROJECTS_JSON": "1",
        "REVIEW_STATE_DIR": str(state_dir),
        "REVIEW_USAGE_LOG": str(state_dir / "usage.jsonl"),
        "REVIEW_WORKTREE_ROOT": str(state_dir / "worktrees"),
        "REVIEW_SERVICE_PORT": str(service_port),
        "REVIEW_CONTROL_PORT": str(control_port),
        # Loopback only. This pair exists for one process on this box to talk
        # to; there is no nginx in front of it and no reason for anything else
        # to reach it.
        "REVIEW_BIND_ADDRESS": "127.0.0.1",
    })
    return env


async def _wait_healthy(url: str, deadline: float, what: str, proc: subprocess.Popen,
                        log_path: Path) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        while asyncio.get_running_loop().time() < deadline:
            if proc.poll() is not None:
                raise ReviewerError(
                    f"the eval {what} exited immediately (code {proc.returncode}). "
                    f"Its output is in {log_path}")
            with contextlib.suppress(httpx.HTTPError):
                r = await client.get(url)
                # 503 is a real answer from a service that is listening -- the
                # reviewer reports unhealthy before its first poll completes,
                # and waiting for a 200 would time out on a working process.
                if r.status_code in (200, 503):
                    return
            await asyncio.sleep(0.4)
    raise ReviewerError(f"the eval {what} never answered {url} within "
                        f"{STARTUP_TIMEOUT_S}s. Its output is in {log_path}")


async def start(projects_json: Path, state_dir: Path) -> EvalReviewer:
    """Bring up the pair, or raise having cleaned up whatever did start."""
    if not REVIEWER_JS.is_file() or not DASHBOARD_JS.is_file():
        raise ReviewerError(f"cannot find the reviewer services under {paths.REPO_ROOT / 'services'}")
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "worktrees").mkdir(exist_ok=True)

    service_port, control_port = free_port(), free_port()
    env = _child_env(projects_json, state_dir, service_port, control_port)
    log_path = state_dir / "reviewer.log"
    procs: list[subprocess.Popen] = []
    try:
        with open(log_path, "ab", buffering=0) as log:
            for script in (DASHBOARD_JS, REVIEWER_JS):
                procs.append(subprocess.Popen(
                    ["node", str(script)], cwd=str(script.parent), env=env,
                    stdout=log, stderr=subprocess.STDOUT,
                    # Its own process group, so the run's own Ctrl-C reaches
                    # the harness and lets it tear these down in order rather
                    # than killing them out from under it.
                    start_new_session=True,
                ))
        deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_S
        await _wait_healthy(f"http://127.0.0.1:{service_port}/health",
                            deadline, "review dashboard", procs[0], log_path)
        await _wait_healthy(f"http://127.0.0.1:{control_port}/health",
                            deadline, "commit reviewer", procs[1], log_path)
    except BaseException:
        await stop(EvalReviewer(service_port, control_port, state_dir, procs, log_path))
        raise
    return EvalReviewer(service_port, control_port, state_dir, procs, log_path)


async def stop(reviewer: EvalReviewer) -> None:
    """Terminate both, then make sure. Never raises -- a run that has just
    produced a report must not lose it to a stubborn child process."""
    for proc in reviewer.procs:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
    for proc in reviewer.procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=5)
        except OSError:  # pragma: no cover - already gone
            pass
