"""Settings, and the audit log that records changing them.

A seam out of agent/server.py (agent/routers/). These two are one module
rather than two because they are interleaved in the original and belong
together in use: every write here records an audit entry, and the audit route
is how an operator reads them back.

The routes are unchanged -- tests/test_route_inventory.py pins every path,
method and guard, so a move that altered any of them fails the snapshot
rather than landing quietly.

State comes off `request.app.state`; importing `app` back from server.py,
which includes this router, would be a cycle.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from agent.auth import User, require_full_auth

from agent import audit
from agent import auth
from agent import github_inbox
from agent import github_settings
from agent import runtime_settings
from agent.config import PROJECTS
from agent.routers import audit_store
from agent.tools import review_gate
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
import logging

logger = logging.getLogger("tektonix")

router = APIRouter(tags=["settings"])


class GitHubSettingsPatch(BaseModel):
    poll_interval_min: int | None = None
    public_url: str | None = None
    notify: dict | None = None
    add_tokens: dict[str, str] | None = None
    remove_tokens: list[str] | None = None
    rename_tokens: dict[str, str] | None = None
    projects: dict[str, dict] | None = None


class GitHubTokenTestRequest(BaseModel):
    name: str | None = None      # a stored token
    token: str | None = None     # or a pasted one, before saving


class RuntimeSettingsRequest(BaseModel):
    values: dict[str, float]


@router.get("/api/settings/runtime")
async def get_runtime_settings(request: Request, user: User = Depends(require_full_auth)):
    """Admin-only: these are deployment-wide, not per-user preferences. Ships
    the spec alongside the values so the UI renders labels, help, units and
    bounds from one source instead of duplicating them."""
    return {"knobs": runtime_settings.KNOBS, "values": runtime_settings.all_values()}


@router.get("/api/audit")
async def read_audit_log(request: Request, limit: int = 100, user: User = Depends(require_full_auth)):
    """Who moved a control, newest first. Admin-only: it names accounts, and
    the point of the page is that a second operator's actions are visible to
    the person responsible for the deployment -- not to everyone with a
    login. See agent/audit.py for what is recorded and what is not."""
    auth.require_admin(user)
    return {"entries": await audit.recent(audit_store(request), limit=min(max(limit, 1), 500)),
            "actions": audit.ACTIONS}


@router.post("/api/settings/runtime")
async def set_runtime_settings(request: Request, req: RuntimeSettingsRequest, user: User = Depends(require_full_auth)):
    """Values are clamped to each knob's bounds rather than rejected, so a
    fat-fingered zero becomes the minimum instead of an error the operator has
    to decode. Unknown names ARE rejected -- a typo must not sit in the
    database looking like configuration."""
    auth.require_admin(user)
    try:
        values = await runtime_settings.save(request.app.state.store, req.values)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    logger.info("runtime settings updated by user %s: %s", user.id, sorted(req.values))
    return {"ok": True, "values": values}


@router.get("/api/settings/github")
async def get_github_settings(request: Request, user: User = Depends(require_full_auth)):
    """Admin-only. Tokens come back as name + hint + date, never the value."""
    auth.require_admin(user)
    settings = github_settings.current()
    return {
        "settings": github_settings.public_view(settings),
        "sources": github_settings.SOURCES,
        "modes": list(github_settings.MODES),
        "author_filters": list(github_settings.AUTHOR_FILTERS),
        "env_token": bool(getattr(request.app.state.config, "github_token", None)),
        "projects": [name for name in PROJECTS],
    }


@router.post("/api/settings/github")
async def set_github_settings(request: Request, req: GitHubSettingsPatch, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    # Auto on a project whose review gate runs nothing mechanical means a
    # model's opinion is the only thing between a GitHub alert and a diff.
    # Refused here so the operator finds out while setting it, rather than
    # from a reason line on an item three days later. The poller enforces the
    # same rule independently -- a project's checks can be emptied after the
    # policy was set (see github_inbox.decide).
    for repo, project_patch in (patch.get("projects") or {}).items():
        modes = (project_patch or {}).get("policies") or {}
        if "auto" not in modes.values():
            continue
        has_checks = await review_gate.project_has_checks(repo)
        if has_checks is False:
            raise HTTPException(400, (
                f"{repo} has no checks configured, so its review gate runs nothing mechanical -- "
                "Auto would start work that nothing verifies. Add checks for the project "
                "(Settings -> Projects, or projects.json) and try again, or use Propose."))
        if has_checks is None:
            raise HTTPException(400, (
                f"could not confirm {repo}'s checks with the review service, so Auto is refused. "
                "Start commit-reviewer and try again, or use Propose."))
    try:
        saved = await github_settings.save(request.app.state.store, request.app.state.config, patch)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    logger.info("github settings updated by user %s: %s", user.id, sorted(patch))
    # One record per project whose source policy actually moved. Auto is the
    # one that matters -- it lets the poller create work without anyone
    # clicking -- so it is named explicitly rather than folded into "settings
    # changed".
    for repo, project_patch in (patch.get("projects") or {}).items():
        modes = (project_patch or {}).get("policies") or {}
        if not modes:
            continue
        await audit.record(
            request.app.state.store, actor=user.email, action="github.source_policy", target=repo,
            detail=", ".join(f"{name}={mode}" for name, mode in sorted(modes.items())),
        )
    request.app.state.github_poll_wake.set()
    return {"ok": True, "settings": github_settings.public_view(saved)}


@router.post("/api/settings/github/test")
async def test_github_token(request: Request, req: GitHubTokenTestRequest, user: User = Depends(require_full_auth)):
    """Who the token is and which projects it reaches. Works on a pasted
    token before it is saved, or on a stored one by name."""
    auth.require_admin(user)
    raw = (req.token or "").strip()
    if not raw and req.name:
        entry = github_settings.current()["tokens"].get(req.name)
        if not entry:
            raise HTTPException(404, f"no stored token named {req.name!r}")
        raw = github_settings.decrypt_token(request.app.state.config, entry["enc"])
    if not raw and getattr(request.app.state.config, "github_token", None):
        raw = request.app.state.config.github_token
    if not raw:
        raise HTTPException(400, "no token to test")
    return await github_inbox.probe_token(raw, PROJECTS)
