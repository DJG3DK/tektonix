"""The one internal route: the commit reviewer's checks, run in the sandbox.

See agent/review_sandbox.py for why it exists. It is not a user-facing route
and has no session: the caller is the review service, authenticated with the
review-control secret it already holds for merge and deploy.

Disabled (503) unless REVIEW_WORKTREE_ROOT is set, which only the compose
bundle does. A host install's reviewer starts its own containers and never
calls this.
"""
from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from agent import review_sandbox as rs

router = APIRouter(tags=["internal"])


def require_review_secret(x_review_secret: str | None = Header(default=None)) -> None:
    expected = os.environ.get("REVIEW_CONTROL_SECRET") or ""
    if not expected or not rs.worktree_root():
        raise HTTPException(503, "review sandbox is not enabled on this deployment")
    if not x_review_secret or not hmac.compare_digest(
            x_review_secret.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "invalid or missing X-Review-Secret")


class _Mount(BaseModel):
    src: str
    dst: str


class _RunBody(BaseModel):
    project: str
    worktree: str
    cmd: str
    args: list[str] = Field(default_factory=list)
    relDir: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    network: str = "none"
    stack: str | None = None
    mounts: list[_Mount] = Field(default_factory=list)
    timeoutMs: int = 300_000


@router.post("/api/internal/review-sandbox/run")
async def run_review_check(body: _RunBody, _auth: None = Depends(require_review_secret)):
    req = rs.CheckRequest(
        project=body.project, worktree=body.worktree, cmd=body.cmd, args=body.args,
        rel_dir=body.relDir, env=body.env, network=body.network, stack=body.stack,
        mounts=[(m.src, m.dst) for m in body.mounts], timeout_ms=body.timeoutMs,
    )
    try:
        return await rs.run_check(req)
    except rs.RejectedRequest as e:
        raise HTTPException(400, f"refused: {e}") from e
