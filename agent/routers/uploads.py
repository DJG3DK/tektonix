"""Operator attachments: POST /api/uploads.

A seam out of agent/server.py (agent/routers/), cut on 2026-09-27. The route
is unchanged -- tests/test_route_inventory.py and tests/test_repo_scope.py pin
its path, guard and the check_repo_access call inside it.

The size limits live here with the route that motivated them, and server.py
imports REQUEST_BODY_MAX_BYTES back for its body-size middleware: that
ceiling is sized for the largest legitimate upload batch (audit M-13), so it
is derived from the two upload constants rather than restated beside the
middleware. Nothing here reads app state; the sandbox path comes from
PROJECTS, read off `agent.config` at call time so a test that swaps it is
seen. The files are never served back by a route: they land inside the
sandbox checkout so the agent's own workspace tools reach them, and
agent/workspaces.py carries them into each task's worktree.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from agent import config as agent_config
from agent.auth import User, check_repo_access, require_full_auth

logger = logging.getLogger("tektonix")

router = APIRouter(tags=["uploads"])


UPLOADS_DIRNAME = ".uploads"
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
# audit M-13: bound the number of files per upload and the absolute request
# body. Without these, `files: list[UploadFile]` was unbounded and a Content-
# Length ceiling existed nowhere (so JSON bodies were unbounded too).
UPLOAD_MAX_FILES = 20
REQUEST_BODY_MAX_BYTES = UPLOAD_MAX_FILES * UPLOAD_MAX_BYTES + 8 * 1024 * 1024
UPLOAD_KINDS = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
    ".pdf": "pdf",
    ".csv": "text", ".tsv": "text", ".txt": "text", ".json": "text", ".md": "text",
    ".xlsx": "sheet", ".xls": "sheet",
}

_GIT_EXCLUDES_PATH = Path(__file__).resolve().parent.parent / ".agent-git-excludes"


def _ensure_uploads_ignored(repo_root: str) -> None:
    """Uploads live inside the sandbox repo (so the agent's /workspace tools
    reach them) but must never enter a commit/review. A repo-external git
    excludes file (core.excludesFile) keeps them invisible to git without
    touching the project's own .gitignore -- zero diff, nothing for the
    reviewer to see."""
    import subprocess
    if not _GIT_EXCLUDES_PATH.exists():
        _GIT_EXCLUDES_PATH.write_text(f"{UPLOADS_DIRNAME}/\n")
    subprocess.run(["git", "-C", repo_root, "config", "core.excludesFile", str(_GIT_EXCLUDES_PATH)], check=False)


@router.post("/api/uploads")
async def upload_files(repo: str, files: list[UploadFile] = File(...), user: User = Depends(require_full_auth)):
    """Store operator attachments in the repo's sandbox under .uploads/<batch>/
    and return a manifest for the task goal. PDFs get a sibling .txt with the
    extracted text so the (text-only) coding models can read them directly;
    images are consumed via the agent's describe_image tool."""
    if repo not in agent_config.PROJECTS:
        raise HTTPException(404, f"unknown repo {repo!r}")
    check_repo_access(user, repo)
    # audit M-13: bound the file count before touching any of them -- and
    # before the batch directory exists, so a refused batch leaves nothing.
    if len(files) > UPLOAD_MAX_FILES:
        raise HTTPException(413, f"too many files ({len(files)}); limit is {UPLOAD_MAX_FILES}")
    repo_root = agent_config.PROJECTS[repo]["sandbox"]
    # audit M-34: _ensure_uploads_ignored is synchronous (Path.exists,
    # write_text, subprocess.run) -- run it off the event loop.
    await asyncio.to_thread(_ensure_uploads_ignored, repo_root)
    batch = uuid.uuid4().hex[:8]
    batch_dir = Path(repo_root) / UPLOADS_DIRNAME / batch
    batch_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest = await _store_batch(files, batch, batch_dir)
    except BaseException:
        # A batch is all or nothing: the manifest is what the goal cites,
        # and files from a refused batch had no manifest and no owner
        # (2026-09-29).
        await asyncio.to_thread(shutil.rmtree, batch_dir, True)
        raise
    return {"repo": repo, "files": manifest}


def _unique_name(name: str, taken: set[str]) -> str:
    """`report.csv`, then `report-2.csv`, `report-3.csv`: two attachments with
    one filename used to be one file on disk, the second silently over the
    first, with two manifest entries pointing at it (2026-09-29)."""
    if name not in taken:
        return name
    stem, ext = Path(name).stem, Path(name).suffix
    n = 2
    while f"{stem}-{n}{ext}" in taken:
        n += 1
    return f"{stem}-{n}{ext}"


async def _store_batch(files: list[UploadFile], batch: str, batch_dir: Path) -> list[dict]:
    manifest = []
    taken: set[str] = set()
    for f in files:
        name = Path(f.filename or "file").name  # strip any path components
        ext = Path(name).suffix.lower()
        kind = UPLOAD_KINDS.get(ext)
        if kind is None:
            raise HTTPException(415, f"unsupported file type {ext!r} ({name})")
        name = _unique_name(name, taken)
        taken.update({name, f"{name}.txt"})     # the pdf's sibling text is a name too
        # audit M-13: stream to disk in chunks with a running counter, aborting
        # (and deleting the partial file) the moment it exceeds the cap -- the
        # old `await f.read()` materialized the whole file in memory first.
        dest = batch_dir / name
        written = 0
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_MAX_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"{name} exceeds {UPLOAD_MAX_BYTES // (1024*1024)}MB")
                out.write(chunk)
        rel = f"{UPLOADS_DIRNAME}/{batch}/{name}"
        entry = {"path": rel, "kind": kind, "bytes": written}
        if kind == "pdf":
            # audit M-34: pypdf full-text extraction is CPU-bound for seconds on
            # a large PDF -- run it in a thread so it doesn't stall the loop.
            def _extract_pdf(dest_path: str, out_path: str) -> int:
                import pypdf
                reader = pypdf.PdfReader(dest_path)
                text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
                Path(out_path).write_text(text, encoding="utf-8")
                return len(reader.pages)
            try:
                pages = await asyncio.to_thread(_extract_pdf, str(dest), str(batch_dir / f"{name}.txt"))
                entry["extracted_text"] = f"{rel}.txt"
                entry["pages"] = pages
            except Exception as e:  # noqa: BLE001 -- a scanned/encrypted pdf shouldn't fail the upload
                entry["extracted_text"] = None
                logger.info("uploads: text extraction failed for %s: %s", name, e)
                entry["note"] = "text extraction failed -- possibly scanned; no text layer"
        manifest.append(entry)
    return manifest
