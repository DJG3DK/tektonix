"""Serving the images the agent shows the operator (agent/artifacts.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from agent import artifacts
from agent.auth import User, check_repo_access, require_full_auth

router = APIRouter(tags=["artifacts"])


@router.get("/api/artifacts/{repo}/{artifact_id}")
async def get_artifact(repo: str, artifact_id: str, user: User = Depends(require_full_auth)):
    check_repo_access(user, repo)
    found = artifacts.load(repo, artifact_id)
    if found is None:
        raise HTTPException(404, "no such image")
    data, ctype = found
    return Response(content=data, media_type=ctype, headers={
        # An image and nothing else, whatever a browser would like to guess.
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        # Immutable: an id is never reused, so it can be cached -- privately.
        "Cache-Control": "private, max-age=31536000, immutable",
    })
