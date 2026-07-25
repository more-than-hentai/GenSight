"""Authentication endpoints (session cookie based, optional)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(BaseModel):
    username: str
    password: str


class DisableBody(BaseModel):
    password: str


def _authenticated(request: Request) -> bool:
    return auth.check(request.cookies.get(auth.COOKIE_NAME))


@router.get("/status")
def status(request: Request):
    return {
        "enabled": auth.enabled(),
        "authenticated": (not auth.enabled()) or _authenticated(request),
        "username": auth.auth_config().get("username", ""),
    }


@router.post("/login")
def login(body: LoginBody):
    token = auth.login(body.username, body.password)
    if not token:
        raise HTTPException(401, "invalid credentials")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=auth.SESSION_TTL,
    )
    return resp


@router.post("/logout")
def logout(request: Request):
    auth.logout(request.cookies.get(auth.COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@router.post("/setup")
def setup(body: SetupBody, request: Request):
    """Enable auth / change credentials. Once enabled, changing
    credentials requires a valid session."""
    if auth.enabled() and not _authenticated(request):
        raise HTTPException(401, "authentication required")
    username = body.username.strip()
    if not username or len(body.password) < 4:
        raise HTTPException(400, "username required, password min 4 chars")
    auth.set_credentials(username, body.password)
    token = auth.login(username, body.password)
    resp = JSONResponse({"ok": True, "enabled": True})
    resp.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=auth.SESSION_TTL,
    )
    return resp


@router.post("/disable")
def disable(body: DisableBody):
    cfg = auth.auth_config()
    if not cfg.get("enabled"):
        return {"ok": True, "enabled": False}
    if not auth.verify_password(body.password, cfg.get("salt", ""),
                                cfg.get("password_hash", "")):
        raise HTTPException(401, "invalid password")
    auth.disable()
    return {"ok": True, "enabled": False}
