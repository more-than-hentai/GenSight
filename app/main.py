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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """When auth is enabled, every /api route except /api/auth/*
    requires a valid session cookie. Static assets stay public so the
    login screen can render."""
    if auth.enabled():
        path = request.url.path
        if path.startswith("/api") and not path.startswith("/api/auth/"):
            if not auth.check(request.cookies.get(auth.COOKIE_NAME)):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "authentication required"},
                )
    return await call_next(request)


for router in routers.ALL:
    app.include_router(router)

app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
