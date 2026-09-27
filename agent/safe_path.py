"""A path under a root, or a refusal.

Every route that turns a request value into a file name already matches it
against a strict pattern before joining, and a pattern that admits no
separator cannot escape the root. This is the same guarantee said the way a
static analyser recognises: the joined path is normalised and then checked
to start with the root. One helper, so the check reads the same everywhere
and cannot be got slightly wrong in one place.
"""
from __future__ import annotations

import os
from pathlib import Path


class PathOutsideRoot(ValueError):
    pass


def under(root: Path | str, *parts: str) -> Path:
    """`root/parts...`, normalised, provided it stays inside `root`."""
    base = os.path.normpath(str(root))
    full = os.path.normpath(os.path.join(base, *parts))
    if full != base and not full.startswith(base + os.sep):
        raise PathOutsideRoot(f"{'/'.join(parts)!r} is not under the root")
    return Path(full)
