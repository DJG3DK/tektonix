"""The daily jobs, read and run from the dashboard (agent/jobs.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from agent import audit, auth, jobs
from agent.auth import User, require_full_auth
from agent.routers import audit_store

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    return {"jobs": [jobs.status(name) for name in jobs.JOBS]}


@router.post("/{name}/run", status_code=202)
async def run_job_now(name: str, request: Request, user: User = Depends(require_full_auth)):
    """Start the job now, whether or not it is due, and return at once; the
    status shows it running and then its result. Runs even while a task is
    in flight: the operator asked."""
    auth.require_admin(user)
    if name not in jobs.JOBS:
        raise HTTPException(404, "no such job")
    if jobs._lock(name).locked():
        raise HTTPException(409, f"{jobs.JOBS[name].title} is already running")
    await audit.record(audit_store(request), actor=user.email, action="jobs.run", target=name)
    from agent import live_state

    live_state.spawn_background(jobs.run_job(name, request.app.state, trigger=f"manual by {user.email}"), f"job:{name}")
    return {"ok": True, "started": name}
