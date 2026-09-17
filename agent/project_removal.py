"""Taking a project back off the agent, and putting it back later.

The rule this module is built around, and the reason it is not just three
lines of rmtree: **the live repository is never touched.** Removing a project
from Tektonix means the agent forgets it -- the worktree it built in, the
deploy key it minted, the memory it accumulated -- and means nothing at all
about the operator's own checkout, their branches, their remote, or their
code. A "remove" that deleted someone's repository because they wanted the
agent to stop watching it would be unrecoverable and entirely our fault.

The one exception is deliberate and is the opposite of destructive:
`core.sshCommand` is unset on the live repo, because the agent set it when it
minted the deploy key. Leaving it would point their git at a key file that no
longer exists and break every push they make by hand.

Archive or delete
-----------------
The agent's knowledge of a project is spread over six store namespaces: its
memory, its episodes, the skills it generated, its planning sessions, their
transcripts, and its task history. `archive` serialises all of it to one JSON
file and then removes the rows; `delete` removes them without the file.

The archive exists so that re-adding the same project later is a continuation
rather than a fresh start -- the codebase map, what the agent learned about
the project's conventions, and the history of what it already tried. It is a
plain file so an operator can see it, copy it, and delete it; the onboarding
wizard offers to restore it when a project of the same name is added again.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from agent import paths

logger = logging.getLogger("agent.project_removal")

ARCHIVE_DIR: Path = Path(os.environ.get("AGENT_ARCHIVE_DIR") or (paths.REPO_ROOT / "archives"))

# Every namespace a project owns. Keyed by a label that survives into the
# archive file, so a restore does not depend on this tuple's order.
#
# (repo,) is the agent's memory for the project; the rest are named. If a new
# per-project namespace is ever added, it belongs here too -- tests/
# test_project_removal.py pins the list against a real provisioned project so
# a forgotten one shows up as leftovers rather than as a mystery months later.
def namespaces(repo: str) -> dict[str, tuple[str, ...]]:
    return {
        "memory": (repo,),
        "episodes": ("episodes", repo),
        "skills": ("skills", repo),
        "planning": ("planning", repo),
        "planning_log": ("planning_log", repo),
        "tasks": ("tasks", repo),
    }


class RemovalError(Exception):
    """Something the operator should see and can act on."""


def _safe_name(name: str) -> str:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise RemovalError(f"invalid project name {name!r}")
    return name


# ---------------------------------------------------------------------------
# archives
# ---------------------------------------------------------------------------

def archive_path(repo: str, when: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when if when is not None else time.time()))
    return ARCHIVE_DIR / f"{_safe_name(repo)}-{stamp}.json"


async def collect(store, repo: str) -> dict[str, Any]:
    """Read every namespace this project owns into one document."""
    out: dict[str, Any] = {
        "project": repo,
        "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1,
        "namespaces": {},
    }
    for label, ns in namespaces(repo).items():
        rows = []
        try:
            for item in await store.asearch(ns, limit=1000):
                rows.append({"key": item.key, "value": item.value})
        except Exception:  # noqa: BLE001 -- a namespace that cannot be read is empty, not fatal
            logger.exception("archive: could not read %s for %s", label, repo)
        out["namespaces"][label] = rows
    out["item_count"] = sum(len(v) for v in out["namespaces"].values())
    return out


def write_archive(doc: dict[str, Any], path: Path | None = None) -> Path:
    target = path or archive_path(doc["project"])
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic, like every other file this codebase replaces: a half-written
    # archive that a restore later reads as truth is worse than none.
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=1, default=str))
    os.replace(tmp, target)
    return target


def list_archives(repo: str | None = None) -> list[dict[str, Any]]:
    """Every archive on disk, newest first. Cheap: the header is read, not the
    rows, so a panel listing them does not load megabytes of memory."""
    if not ARCHIVE_DIR.is_dir():
        return []
    out = []
    for f in sorted(ARCHIVE_DIR.glob("*.json"), reverse=True):
        try:
            doc = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if repo is not None and doc.get("project") != repo:
            continue
        out.append({
            "file": f.name,
            "project": doc.get("project"),
            "archived_at": doc.get("archived_at"),
            "item_count": doc.get("item_count", 0),
        })
    return out


def read_archive(filename: str) -> dict[str, Any]:
    """Load one archive by FILENAME, never by a caller-supplied path: the name
    has to resolve to a file directly inside ARCHIVE_DIR, so a request cannot
    walk out of it."""
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise RemovalError(f"invalid archive name {filename!r}")
    target = (ARCHIVE_DIR / filename).resolve()
    if target.parent != ARCHIVE_DIR.resolve() or not target.is_file():
        raise RemovalError(f"no archive named {filename!r}")
    try:
        return json.loads(target.read_text())
    except (OSError, ValueError) as e:
        raise RemovalError(f"{filename} is unreadable: {e}") from e


def delete_archive(filename: str) -> None:
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise RemovalError(f"invalid archive name {filename!r}")
    target = (ARCHIVE_DIR / filename).resolve()
    if target.parent != ARCHIVE_DIR.resolve() or not target.is_file():
        raise RemovalError(f"no archive named {filename!r}")
    target.unlink()


async def restore(store, repo: str, doc: dict[str, Any]) -> int:
    """Write an archive's rows back, under `repo`.

    Restores under the CURRENT project name rather than the archived one, so
    re-adding a project at a different name still carries its memory across --
    and so a hand-edited archive cannot write into a namespace the operator
    did not ask for.
    """
    written = 0
    for label, ns in namespaces(repo).items():
        for row in doc.get("namespaces", {}).get(label) or []:
            key, value = row.get("key"), row.get("value")
            if not key:
                continue
            await store.aput(ns, key, value)
            written += 1
    return written


async def purge(store, repo: str) -> int:
    """Remove every row this project owns."""
    removed = 0
    for ns in namespaces(repo).values():
        try:
            for item in await store.asearch(ns, limit=1000):
                await store.adelete(ns, item.key)
                removed += 1
        except Exception:  # noqa: BLE001 -- keep going; a stuck namespace must not strand the rest
            logger.exception("purge: could not clear %s for %s", ns, repo)
    return removed


# ---------------------------------------------------------------------------
# the on-disk half
# ---------------------------------------------------------------------------

def remove_worktree(live: str, sandbox: str) -> tuple[bool, str]:
    """Unregister the agent's workspace through git, then make sure it is gone.

    `git worktree remove` rather than rmtree because the live repo holds
    administrative files for every worktree of it; deleting the directory
    alone leaves the registration behind, and the next `worktree add` at the
    same path fails with "already registered".
    """
    if not os.path.isdir(sandbox):
        return True, "no workspace to remove"
    if os.path.isdir(live):
        r = subprocess.run(["git", "worktree", "remove", "--force", sandbox],  # noqa: S603,S607
                           cwd=live, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, f"removed {sandbox}"
        detail = (r.stderr or r.stdout).strip()[:300]
    else:
        detail = "the live repo is gone, so git cannot unregister it"
    # The live repo may itself have been moved or deleted by the operator.
    # The workspace is ours either way, so take it.
    try:
        shutil.rmtree(sandbox)
    except OSError as e:
        return False, f"{detail}; and the directory could not be removed: {e}"
    return True, f"removed {sandbox} directly ({detail})"


def clear_reviewer_state(state_path: Path, repo: str) -> bool:
    """Drop the project's row from the commit reviewer's state file.

    Left behind it is harmless but confusing: the reviewer's /projects would
    stop listing it while its last verdict sat in the file forever.
    """
    if not state_path.is_file():
        return False
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return False
    if repo not in data:
        return False
    data.pop(repo)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, state_path)
    return True


def remove_project_entry(projects_path: Path, repo: str) -> bool:
    """Take the project out of projects.json, atomically.

    The counterpart to provisioning.write_project_entry, and the step that
    actually makes the project gone: the two Node services re-read this file
    on every poll, so they stop reviewing and deploying it with no restart.
    """
    try:
        data = json.loads(projects_path.read_text())
    except (OSError, ValueError) as e:
        raise RemovalError(f"could not read {projects_path}: {e}") from e
    projects = data.get("projects") or {}
    if repo not in projects:
        return False
    projects.pop(repo)
    # test_env is keyed by project too (see projects.json's own shape).
    (data.get("test_env") or {}).pop(repo, None)
    tmp = projects_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, projects_path)
    return True
