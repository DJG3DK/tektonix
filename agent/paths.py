"""Where this installation lives on disk.

Every path here used to be the literal string "/home/3d-agent", which is
where the original deployment happened to sit. That works exactly once: a
second install anywhere else silently reads and writes the first one's files,
or fails on a directory that isn't there.

REPO_ROOT is derived from this file's own location (agent/paths.py -> agent/
-> repo root), so it is correct wherever the checkout is, including a git
worktree of it. AGENT_HOME overrides it for the unusual case of running the
code from one place while its data lives in another.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT: Path = Path(
    os.environ.get("AGENT_HOME") or Path(__file__).resolve().parent.parent
).resolve()

# Runtime data the app writes (gitignored). Kept together so a deployment can
# point them somewhere else in one move.
DATA_DIR: Path = REPO_ROOT / "data"
SERVICES_DIR: Path = REPO_ROOT / "services"

# The review services' own on-disk state.
REVIEWER_DIR: Path = SERVICES_DIR / "commit-reviewer"
REVIEWER_USAGE_LOG: Path = REVIEWER_DIR / "usage.jsonl"


def repo_path(*parts: str) -> Path:
    """A path inside this installation."""
    return REPO_ROOT.joinpath(*parts)


class UnsafePath(ValueError):
    """A name that does not resolve to a file directly inside its directory."""


def contained_file(base: Path | str, name: str) -> Path:
    """`base/name`, proven to sit directly inside `base`.

    One implementation because there were several, each slightly different,
    and "slightly different" is how one of them ends up wrong. Callers that
    take a file name from a request or a project name from a config route it
    through here before opening, writing or deleting anything.

    Three checks, deliberately in this order:

    1. The name must be a single component. A separator, a dot-segment or an
       empty string is rejected outright rather than normalised away, because
       a caller asking for `a/b` wanted something this function cannot give.
    2. `normpath` then `startswith(base + sep)`. This is the textual
       containment check, and it is the shape static analysis recognises as a
       barrier (CodeQL py/path-injection) -- `Path.parent == base` is
       equivalent to a reader and invisible to the checker, which is why the
       earlier spelling was flagged ten times over.
    3. `realpath` on both sides, and the same containment again. This is the
       one that actually matters: step 2 is textual and a symlink inside
       `base` pointing out of it would pass it.

    Raises UnsafePath rather than returning None so a caller cannot forget to
    look at the result.
    """
    if not name or "/" in name or "\\" in name or os.sep in name or name in (".", ".."):
        raise UnsafePath(f"{name!r} is not a single path component")

    base_dir = os.path.normpath(str(base))
    joined = os.path.normpath(os.path.join(base_dir, name))
    if not joined.startswith(base_dir + os.sep):
        raise UnsafePath(f"{name!r} escapes {base_dir}")

    # Symlinks: the textual check above cannot see them, and `base` is a
    # directory other processes and the operator can write to.
    real_base = os.path.realpath(base_dir)
    real_target = os.path.realpath(joined)
    if not real_target.startswith(real_base + os.sep):
        raise UnsafePath(f"{name!r} resolves outside {base_dir}")
    if os.path.dirname(real_target) != real_base:
        raise UnsafePath(f"{name!r} is not directly inside {base_dir}")

    return Path(joined)
