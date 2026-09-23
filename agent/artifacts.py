"""Images the agent shows the operator.

The agent could always LOOK at an image -- a render of a logo it drew, a
screenshot of a page -- by sending it to a vision model and reading the
description back. It could never SHOW one: nothing carried the pixels to the
dashboard. Found 2026-09-23: asked to "show me examples of the logo before we
build anything", a planning session rendered sixteen versions, read sixteen
descriptions, approved its own design, and the operator saw none of them.

So an image the agent means to show is stored here, per project, and served
by an authenticated route (agent/routers/artifacts.py) that checks the viewer
may see that project. The dashboard renders a reference to it as a picture.

Only raster images, and only ones whose bytes say so. An SVG is rasterised
before it gets here (agent/tools/show_tools.py): served from this origin, an
SVG is a document that can run script, and a design the agent wrote is not
something to execute in the operator's session.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from agent import paths

ROOT = paths.REPO_ROOT / "logs" / "artifacts"
MAX_BYTES = 8 * 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{32}$")
# What each accepted format's first bytes are, and what it is served as.
_FORMATS = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


class ArtifactError(ValueError):
    pass


def sniff(data: bytes) -> tuple[str, str] | None:
    """(extension, content type) from the bytes themselves, or None."""
    for magic, ext, ctype in _FORMATS:
        if data.startswith(magic):
            return ext, ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def _dir(repo: str) -> Path:
    from agent.config import PROJECTS  # noqa: PLC0415 -- config reads env at import
    if repo not in PROJECTS:
        raise ArtifactError(f"unknown project {repo!r}")
    return ROOT / repo


def save(repo: str, data: bytes, caption: str = "", source: str = "") -> str:
    """Store an image for `repo`; returns the URL the dashboard loads it from."""
    if len(data) > MAX_BYTES:
        raise ArtifactError(f"image is {len(data)} bytes; the limit is {MAX_BYTES}")
    kind = sniff(data)
    if kind is None:
        raise ArtifactError("not a PNG, JPEG, GIF or WebP image")
    ext, _ = kind
    folder = _dir(repo)
    folder.mkdir(parents=True, exist_ok=True)
    artifact_id = uuid.uuid4().hex
    (folder / f"{artifact_id}.{ext}").write_bytes(data)
    (folder / f"{artifact_id}.json").write_text(json.dumps(
        {"caption": caption[:300], "source": source[:300], "created_at": time.time()}))
    return url_for(repo, artifact_id)


def url_for(repo: str, artifact_id: str) -> str:
    return f"/api/artifacts/{repo}/{artifact_id}"


def load(repo: str, artifact_id: str) -> tuple[bytes, str] | None:
    """(bytes, content type), or None when there is no such image."""
    if not ID_RE.match(artifact_id or ""):
        return None
    try:
        folder = _dir(repo)
    except ArtifactError:
        return None
    for path in folder.glob(f"{artifact_id}.*"):
        if path.suffix == ".json":
            continue
        data = path.read_bytes()
        kind = sniff(data)
        if kind:
            return data, kind[1]
    return None
