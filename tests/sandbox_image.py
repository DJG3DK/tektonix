"""Whether the local sandbox image is usable by the Docker-backed tests.

Two suites (test_sandbox_gh.py, test_review_db_check_e2e.py) skip without a
built `tektonix-sandbox` image. They used to trust whatever image was
present, and a stale one -- built before `gh` was added to the Dockerfile --
failed them with errors that read as product bugs (2026-09-29 audit, T4).

The image carries no label to compare, so staleness is judged by time: an
image created before the last change under docker/agent-sandbox/ was built
from an older Dockerfile. The last change is the newest commit touching that
directory, or the files' mtime on a checkout without history. Either way the
answer is a skip with a reason that says to rebuild, never a failure.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent import paths

IMAGE = "tektonix-sandbox:latest"
SANDBOX_DIR = paths.REPO_ROOT / "docker" / "agent-sandbox"


def _image_created() -> datetime | None:
    """When the local image was built, or None when there is no image (or no
    docker). `image inspect` never contacts a registry."""
    if shutil.which("docker") is None or not Path("/var/run/docker.sock").exists():
        return None
    try:
        out = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
        return _parse_created(json.loads(out.stdout)[0]["Created"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        return None


def _parse_created(created: str) -> datetime:
    # "2026-09-28T14:23:19.398793542Z": nine fractional digits, of which
    # fromisoformat takes at most six.
    head, _, frac = created.rstrip("Z").partition(".")
    return datetime.fromisoformat(head + ("." + frac[:6] if frac else "")).replace(tzinfo=UTC)


def _dockerfile_changed() -> datetime | None:
    """When docker/agent-sandbox/ last changed."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct", "--", str(SANDBOX_DIR)],
                             cwd=str(paths.REPO_ROOT), capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return datetime.fromtimestamp(int(out.stdout.strip()), tz=UTC)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        newest = max(p.stat().st_mtime for p in SANDBOX_DIR.rglob("*") if p.is_file())
    except (OSError, ValueError):
        return None
    return datetime.fromtimestamp(newest, tz=UTC)


def skip_reason() -> str | None:
    """None when the image is built and current; otherwise why to skip."""
    created = _image_created()
    if created is None:
        return "the sandbox image is not built on this machine"
    changed = _dockerfile_changed()
    if changed is not None and created < changed:
        return (f"the local {IMAGE} is stale (built {created:%Y-%m-%d %H:%M}, docker/agent-sandbox changed "
                f"{changed:%Y-%m-%d %H:%M}); rebuild it")
    return None


def is_stale(created: datetime | None, changed: datetime | None) -> bool:
    """The rule on its own, for the test that pins it."""
    return created is not None and changed is not None and created < changed
