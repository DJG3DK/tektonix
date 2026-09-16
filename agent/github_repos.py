"""Create a GitHub repository for a brand-new project and push to it.

The three GitHub calls "new project" needs -- create a private repo, register
a deploy key on it, and nothing else -- plus the git side of connecting the
fresh local repo to it. Same client conventions as agent/github_inbox.py
(bearer token, 20s timeout, 401/403 -> PermissionError, 404 -> LookupError),
and the same rule about what may leave this module: never the token, never
a raw response body. GitHub's error bodies quote the request back, and a
422 for a bad `name` would otherwise put whatever the operator typed into a
log line.

Why the push happens here
-------------------------
`git push` is on the agent's blocked-command list (agent/tools): an agent
running a task must never publish anything by itself. This module is NOT
the agent. It runs host-side inside the API process, on an admin's explicit
request, against a repository that has one commit and no history to leak.
That is the same trust level as the review service's post-merge push
(services/agent-review/server.js), which is the other place a push is
allowed to originate.

Why SSH, and why a deploy key per repo
--------------------------------------
The origin is always `git@github.com:owner/name.git`, never HTTPS. An HTTPS
origin would resolve through the host's global credential helper, and on a
box with `gh auth login` that silently pushes as the operator's personal
account -- plus agent/tools/github_tools._REMOTE_RE and deploy_keys.status
both treat the SSH form as the well-known one. And it is a fresh key per
repo rather than the host's default identity because ~/.ssh/config pins
`Host github.com` to another project's deploy key: a push over the default
identity is refused by GitHub for the new repo. deploy_keys.generate_key
mints the key and scopes it to this one repo via core.sshCommand.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from agent import deploy_keys
from agent import provisioning

logger = logging.getLogger("agent.github_repos")

API = "https://api.github.com"
_TIMEOUT = 20

# A freshly created repository is not immediately visible to the git
# transport: `ls-remote` right after the POST returns can say "not found"
# for a few seconds. Five tries over ~10s covers what has been observed.
_REMOTE_ATTEMPTS = 5
_REMOTE_BACKOFF_S = 2.0


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _reason(r: httpx.Response) -> str:
    """The `message` and per-field `errors` from a GitHub error body, and
    nothing else from it. Truncated: a reason is one line in a step."""
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    parts = [str(body.get("message") or "")]
    for err in body.get("errors") or []:
        if isinstance(err, dict):
            parts.append(str(err.get("message") or err.get("code") or ""))
        else:
            parts.append(str(err))
    return "; ".join(p for p in parts if p)[:200] or f"HTTP {r.status_code}"


async def _post(token: str, path: str, body: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(f"{API}{path}", headers=_headers(token), json=body)
    if r.status_code == 404:
        raise LookupError(f"GitHub 404 for {path} (no access, or it does not exist)")
    if r.status_code in (401, 403):
        raise PermissionError(f"GitHub refused {path} ({r.status_code}): {_reason(r)}")
    if r.status_code == 422:
        raise ValueError(_reason(r))
    r.raise_for_status()
    return r.json()


async def create_private_repo(token: str, name: str, description: str = "",
                              org: str | None = None) -> dict[str, str]:
    """Create a PRIVATE, EMPTY repository. auto_init stays false on purpose:
    the local repo already has its initial commit, and a GitHub-made first
    commit would give the two unrelated histories and make the first push a
    rejected non-fast-forward."""
    path = f"/orgs/{org}/repos" if org else "/user/repos"
    body = {"name": name, "description": description or "", "private": True, "auto_init": False}
    try:
        data = await _post(token, path, body)
    except ValueError as e:
        if "already exists" in str(e):
            where = f"the {org} organisation" if org else "this account"
            raise ValueError(f"a repository named {name!r} already exists on {where}") from e
        raise ValueError(f"GitHub rejected the repository: {e}") from e
    full_name = str(data.get("full_name") or "")
    if not full_name:
        raise ValueError("GitHub did not return the new repository's name")
    ssh_url = str(data.get("ssh_url") or "")
    if not ssh_url.startswith("git@"):
        # Whatever the API says, the origin we write is the SSH form -- see
        # the module docstring for what an HTTPS origin would do.
        ssh_url = f"git@github.com:{full_name}.git"
    return {
        "full_name": full_name,
        "ssh_url": ssh_url,
        "html_url": str(data.get("html_url") or f"https://github.com/{full_name}"),
    }


async def add_deploy_key(token: str, full_name: str, title: str, public_key: str) -> None:
    """Register a public key on the repository with WRITE access -- it is the
    identity the post-merge push uses, so read-only would defeat it."""
    await _post(token, f"/repos/{full_name}/keys",
                {"title": title, "key": public_key.strip(), "read_only": False})


def connect_origin(live: str, ssh_url: str, name: str) -> str:
    """Set `origin` and mint the repo's deploy key; returns the PUBLIC half
    for add_deploy_key(). Synchronous: call it in a thread.

    The remote goes in first because deploy_keys.status() -- which
    generate_key returns -- reports nothing about a key on a repo with no
    origin, so the other order would hand back no public key to register.
    """
    if not ssh_url.startswith("git@"):
        raise deploy_keys.DeployKeyError(
            f"refusing a non-SSH origin ({ssh_url.split('@')[0][:40]}...)")
    ok, out = provisioning._run_git(["remote", "add", "origin", ssh_url], cwd=live)
    if not ok:
        raise deploy_keys.DeployKeyError(f"git remote add failed: {out[:300]}")
    st = deploy_keys.generate_key(name, live)
    if not st.public_key:
        raise deploy_keys.DeployKeyError("the generated deploy key has no public half")
    return st.public_key


def push_initial(live: str, name: str, *, attempts: int = _REMOTE_ATTEMPTS,
                 backoff_s: float = _REMOTE_BACKOFF_S) -> tuple[bool, str]:
    """Push `main` to the origin connect_origin() set, once the key is
    registered. Synchronous: call it in a thread.

    Retries the reachability probe because GitHub's create is eventually
    consistent; the push itself is attempted once, since a failed push
    leaves nothing to retry around (the remote is empty either way).
    """
    ssh_url = ""
    ok, out = provisioning._run_git(["config", "--local", "--get", "remote.origin.url"], cwd=live)
    if ok:
        ssh_url = out.strip()
    reachable, detail = False, ""
    for attempt in range(1, max(1, attempts) + 1):
        reachable, detail = deploy_keys.check_remote(name, live)
        if reachable:
            break
        if attempt < attempts:
            time.sleep(backoff_s)
    if not reachable:
        return False, (f"origin set to {ssh_url} but it is not reachable with the new deploy "
                       f"key after {attempts} tries: {detail[-300:]}")
    ok, out = provisioning._run_git(["push", "-u", "origin", "main"], cwd=live, timeout=120)
    if not ok:
        return False, f"origin set to {ssh_url} but `git push -u origin main` failed: {out[-300:]}"
    return True, f"pushed main to {ssh_url}"
