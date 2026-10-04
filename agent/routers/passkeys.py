"""Passkey routes: adding one to your account, listing and removing them, and
signing in with one. agent/passkeys.py holds the WebAuthn side.

Adding a passkey asks for the account password again: a session someone
else got hold of must not be able to plant a way back in. Signing in with
one is rate-limited like a password guess and answers every failure the
same way.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from agent import audit, auth, passkeys, rate_limit
from agent.auth import User, require_full_auth
from agent.routers import audit_store
from agent.routers.auth import _set_session_cookie, _user_public

router = APIRouter(tags=["passkeys"])


class RegisterOptionsRequest(BaseModel):
    password: str = ""


class RegisterVerifyRequest(BaseModel):
    challenge_id: str = Field(max_length=200)
    credential: dict
    name: str = Field(default="", max_length=200)


class LoginVerifyRequest(BaseModel):
    challenge_id: str = Field(max_length=200)
    credential: dict


class RenameRequest(BaseModel):
    name: str = Field(max_length=200)


def _rp(request: Request) -> passkeys.RelyingParty:
    try:
        return passkeys.relying_party(request.headers.get("origin"))
    except passkeys.PasskeyError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/api/auth/passkeys")
async def list_passkeys(request: Request, user: User = Depends(require_full_auth)):
    return {"passkeys": await passkeys.list_for(request.app.state.auth_pool, user.id)}


@router.post("/api/auth/passkeys/register/options")
async def register_options(request: Request, req: RegisterOptionsRequest, user: User = Depends(require_full_auth)):
    rate_limit.check_rate_limit(request, "password-recheck")
    row = await auth.get_user_by_id(request.app.state.auth_pool, user.id)
    if not req.password or not auth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(403, "current password required to add a passkey")
    rate_limit.clear_rate_limit(request, "password-recheck")
    try:
        return await passkeys.registration_options(request.app.state.auth_pool, _rp(request), user.id, user.email)
    except passkeys.PasskeyError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/api/auth/passkeys/register/verify", status_code=201)
async def register_verify(request: Request, req: RegisterVerifyRequest, user: User = Depends(require_full_auth)):
    try:
        saved = await passkeys.register(request.app.state.auth_pool, _rp(request), user.id,
                                        req.challenge_id, req.credential, req.name)
    except passkeys.PasskeyError as e:
        raise HTTPException(400, str(e)) from e
    await audit.record(audit_store(request), actor=user.email, action="auth.passkey_add",
                       target=user.email, detail=saved["name"])
    return saved


@router.patch("/api/auth/passkeys/{passkey_id}")
async def rename_passkey(request: Request, passkey_id: int, req: RenameRequest, user: User = Depends(require_full_auth)):
    name = await passkeys.rename(request.app.state.auth_pool, user.id, passkey_id, req.name)
    if name is None:
        raise HTTPException(404, "no such passkey on this account")
    return {"id": passkey_id, "name": name}


@router.delete("/api/auth/passkeys/{passkey_id}")
async def delete_passkey(request: Request, passkey_id: int, user: User = Depends(require_full_auth)):
    name = await passkeys.remove(request.app.state.auth_pool, user.id, passkey_id)
    if name is None:
        raise HTTPException(404, "no such passkey on this account")
    await audit.record(audit_store(request), actor=user.email, action="auth.passkey_remove",
                       target=user.email, detail=name)
    return {"ok": True}


@router.post("/api/auth/passkeys/login/options")
async def login_options(request: Request):
    rate_limit.check_rate_limit(request, "passkey-login")
    try:
        return await passkeys.login_options(request.app.state.auth_pool, _rp(request))
    except passkeys.PasskeyError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/api/auth/passkeys/login/verify")
async def login_verify(request: Request, req: LoginVerifyRequest, response: Response):
    rate_limit.check_rate_limit(request, "passkey-login")
    pool = request.app.state.auth_pool
    try:
        user_id = await passkeys.login(pool, _rp(request), req.challenge_id, req.credential)
    except passkeys.PasskeyError as e:
        raise HTTPException(401, str(e)) from e
    rate_limit.clear_rate_limit(request, "passkey-login")
    token = await auth.create_session(pool, user_id)
    _set_session_cookie(response, token, request)
    row = await auth.get_user_by_id(pool, user_id)
    return {"requires_2fa": False, "user": _user_public(auth._row_to_user(row))}
