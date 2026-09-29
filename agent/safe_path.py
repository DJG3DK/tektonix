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


def under(root: Path | str, *parts: str, resolve: bool = False) -> Path:
    """`root/parts...`, normalised, provided it stays inside `root`.

    The check is lexical: a symlink inside the root that points outside it
    passes. With `resolve=True` both sides are followed to their real paths
    and compared again, and the real path is returned -- for a root whose
    contents something else writes (the SWE-bench harness's run
    directories), where the path must exist to be of any use anyway
    (2026-09-29 audit, A12).
    """
    base = os.path.normpath(str(root))
    full = os.path.normpath(os.path.join(base, *parts))
    if full != base and not full.startswith(base + os.sep):
        raise PathOutsideRoot(f"{'/'.join(parts)!r} is not under the root")
    if not resolve:
        return Path(full)
    real_base = os.path.realpath(base)
    real_full = os.path.realpath(full)
    if real_full != real_base and not real_full.startswith(real_base + os.sep):
        raise PathOutsideRoot(f"{'/'.join(parts)!r} leaves the root through a link")
    return Path(real_full)
