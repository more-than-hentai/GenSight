"""Authentication endpoints (session cookie based, optional roles)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import audit, auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class SetupBody(BaseModel):
    username: str
    password: str


class DisableBody(BaseModel):
    password: str


class UserBody(BaseModel):
    username: str
    password: str
    role: str = "user"


def _session(request: Request) -> tuple[str, str] | None:
    return auth.session_info(request.cookies.get(auth.COOKIE_NAME))


def _login_response(username: str, password: str) -> JSONResponse:
    token = auth.login(username, password)
    resp = JSONResponse({"ok": True})
    if token:
        resp.set_cookie(
            auth.COOKIE_NAME, token, httponly=True, samesite="lax",
            max_age=auth.SESSION_TTL,
        )
    return resp


@router.get("/status")
def status(request: Request):
    info = _session(request)
    return {
        "enabled": auth.enabled(),
        "authenticated": (not auth.enabled()) or info is not None,
        "username": info[0] if info else "",
        "role": info[1] if info else ("admin" if not auth.enabled() else ""),
    }


@router.post("/login")
def login(body: LoginBody, request: Request):
    token = auth.login(body.username, body.password)
    client = request.client.host if request.client else ""
    if not token:
        audit.record("auth.login", actor=body.username, target=client,
                     detail={"result": "invalid credentials"}, ok=False)
        raise HTTPException(401, "invalid credentials")
    audit.record("auth.login", actor=body.username, target=client)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=auth.SESSION_TTL,
    )
    return resp


@router.post("/logout")
def logout(request: Request):
    info = _session(request)
    auth.logout(request.cookies.get(auth.COOKIE_NAME))
    audit.record("auth.logout", actor=info[0] if info else "")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@router.post("/setup")
def setup(body: SetupBody, request: Request):
    """Enable auth with an admin account. Once enabled, only an admin
    session may change credentials (enforced by the middleware)."""
    username = body.username.strip()
    if not username or len(body.password) < 4:
        raise HTTPException(400, "username required, password min 4 chars")
    auth.set_credentials(username, body.password)
    audit.record("auth.enable", actor=username)
    return _login_response(username, body.password)


@router.post("/disable")
def disable(body: DisableBody):
    """Disable auth. Requires any admin's password; accounts are kept."""
    if not auth.enabled():
        return {"ok": True, "enabled": False}
    admins = [u for u in auth.get_users() if u.get("role") == "admin"]
    if not any(
        auth.verify_password(body.password, u.get("salt", ""),
                             u.get("password_hash", ""))
        for u in admins
    ):
        raise HTTPException(401, "invalid password")
    auth.disable()
    audit.record("auth.disable")
    return {"ok": True, "enabled": False}


# -------- user management (admin-only via middleware) --------


@router.get("/users")
def list_users():
    return {"users": [
        {"username": u["username"], "role": u.get("role", "admin")}
        for u in auth.get_users()
    ]}


@router.post("/users")
def create_user(body: UserBody, request: Request):
    username = body.username.strip()
    if not username or len(body.password) < 4:
        raise HTTPException(400, "username required, password min 4 chars")
    existing = auth.find_user(username)
    if existing and existing.get("role") == "admin" and body.role != "admin":
        admins = [u for u in auth.get_users() if u.get("role") == "admin"]
        if len(admins) == 1:
            raise HTTPException(400, "cannot demote the last admin account")
    # Read the caller's identity before add_user revokes their session.
    caller = _session(request)
    try:
        auth.add_user(username, body.password, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record("auth.user_upsert", actor=caller[0] if caller else "",
                 target=username,
                 detail={"role": body.role, "existed": existing is not None})
    # Re-issue a cookie when an admin just changed their own credentials
    # so the revocation does not sign them out mid-session.
    if caller and caller[0] == username:
        return _login_response(username, body.password)
    return {"ok": True}


@router.delete("/users/{username}")
def remove_user(username: str, request: Request):
    info = _session(request)
    if info and info[0] == username:
        raise HTTPException(400, "cannot delete your own account")
    try:
        auth.delete_user(username)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record("auth.user_delete", actor=info[0] if info else "",
                 target=username)
    return {"ok": True}
