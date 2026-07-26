"""GenSight — AI-generated image metadata extractor web UI.

Application assembly only: routers live in app/routers/, domain logic
in the sibling modules (db, scanner, watcher, quality, tagger, files,
auth). Run with:  uvicorn app.main:app
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, config, routers
from .watcher import watch_manager

logger = logging.getLogger("gensight")

app = FastAPI(title="GenSight", version=__version__)

WEB_DIR = config.BASE_DIR / "web"


@app.on_event("startup")
def _startup() -> None:
    watch_manager.start()


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return JSON instead of a bare 500 so the UI can show a message."""
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"}
    )


# Endpoints a restricted "user" role may reach. Everything else under
# /api is admin-only: anything accepting filesystem paths as input or
# mutating system state (settings, scans, watches, groups, trash,
# organize, tagger/quality jobs, user management) stays locked down
# when the instance is exposed beyond localhost.
USER_ALLOWED_PREFIXES = (
    "/api/library",      # browse/search/rate (cleanup excluded below)
    "/api/stats",
    "/api/analyze",      # single-image upload analysis
    "/api/image",        # path-validated serving
    "/api/i18n",
)
USER_DENIED_PREFIXES = (
    "/api/library/cleanup",
)
# Reachable without a session so the login screen can work
PUBLIC_PREFIXES = (
    "/api/auth/status", "/api/auth/login", "/api/auth/logout", "/api/i18n",
)


def _user_allowed(path: str) -> bool:
    if any(path.startswith(p) for p in USER_DENIED_PREFIXES):
        return False
    return any(path.startswith(p) for p in USER_ALLOWED_PREFIXES)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """When auth is enabled: every /api route needs a valid session
    (except the public auth/i18n endpoints), and non-admin sessions are
    limited to the read/analyze surface. Static assets stay public so
    the login screen can render.

    Also publishes the effective role on request.state so endpoints
    that take a path (media) can tighten their own checks."""
    # Auth off = single-operator localhost mode: treat as admin.
    request.state.auth_role = "admin"
    request.state.auth_user = ""
    if auth.enabled():
        path = request.url.path
        info = auth.session_info(request.cookies.get(auth.COOKIE_NAME))
        request.state.auth_role = info[1] if info else None
        request.state.auth_user = info[0] if info else ""
        if path.startswith("/api") and not any(
            path.startswith(p) for p in PUBLIC_PREFIXES
        ):
            if info is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "authentication required"},
                )
            if info[1] != "admin" and not _user_allowed(path):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "admin privileges required"},
                )
    return await call_next(request)


for router in routers.ALL:
    app.include_router(router)

app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
