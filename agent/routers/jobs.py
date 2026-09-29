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
    # start_job claims the slot before returning, so a second click in the
    # same loop turn is a 409 here rather than a JobBusy in the background.
    try:
        jobs.start_job(name, request.app.state, trigger=f"manual by {user.email}")
    except jobs.JobBusy as e:
        raise HTTPException(409, str(e))
    await audit.record(audit_store(request), actor=user.email, action="jobs.run", target=name)
    return {"ok": True, "started": name}
