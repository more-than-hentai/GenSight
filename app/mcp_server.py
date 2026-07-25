"""MCP (Model Context Protocol) server for GenSight.

Exposes the image library to AI clients (Claude Code, Claude Desktop,
etc.) over stdio. Register with:

    claude mcp add gensight -- /path/to/GenSight/.venv/bin/python -m app.mcp_server

The server reads the same SQLite library the web UI writes, so scans
performed in the browser are immediately searchable from the AI client.

Authentication: when web auth is enabled in settings, MCP tools are
locked too. Unlock either by calling the `login` tool with the same
credentials, or by setting the GENSIGHT_MCP_PASSWORD environment
variable in the MCP server configuration (username defaults to the
configured one).
"""
from __future__ import annotations

import os
import sys

from . import auth as auth_mod

# Per-process unlock state (an MCP stdio server serves one client).
_unlocked = False


def _verify(username: str, password: str) -> bool:
    cfg = auth_mod.auth_config()
    return username == cfg.get("username") and auth_mod.verify_password(
        password, cfg.get("salt", ""), cfg.get("password_hash", "")
    )


def authorize(username: str, password: str) -> bool:
    """Validate credentials against the web auth config and unlock."""
    global _unlocked
    if _verify(username, password):
        _unlocked = True
    return _unlocked


def require_auth() -> dict | None:
    """Return an error payload when locked, None when access is OK."""
    global _unlocked
    if not auth_mod.enabled() or _unlocked:
        return None
    env_pw = os.environ.get("GENSIGHT_MCP_PASSWORD")
    if env_pw and _verify(
        os.environ.get("GENSIGHT_MCP_USERNAME",
                       auth_mod.auth_config().get("username", "")),
        env_pw,
    ):
        _unlocked = True
        return None
    return {
        "error": "authentication required: call the login tool with the "
                 "GenSight username/password, or set GENSIGHT_MCP_PASSWORD "
                 "in the MCP server environment"
    }


def _build():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print(
            "The 'mcp' package is required: pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from . import db, stats

    mcp = FastMCP("GenSight")

    def _slim(item: dict) -> dict:
        return {
            k: item.get(k)
            for k in (
                "file", "filename", "tool", "prompt", "negative_prompt",
                "params", "rating", "favorite", "group_name", "tags",
            )
        }

    @mcp.tool()
    def login(username: str, password: str) -> dict:
        """Authenticate against GenSight when web auth is enabled.
        Required before other tools work if the library is protected."""
        if not auth_mod.enabled():
            return {"ok": True, "note": "authentication is not enabled"}
        if authorize(username, password):
            return {"ok": True}
        return {"ok": False, "error": "invalid credentials"}

    @mcp.tool()
    def search_images(
        query: str = "", tool: str = "", favorite_only: bool = False,
        min_rating: int = 0, limit: int = 20,
    ) -> dict:
        """Search the AI image library by prompt/filename/model/tag text.
        `tool` filters by generator: a1111, comfyui, novelai, unknown."""
        if (err := require_auth()):
            return err
        total, items = db.query_images(
            q=query, tool=tool, favorite=favorite_only or None,
            min_rating=min_rating, limit=min(limit, 100),
        )
        return {"total": total, "items": [_slim(i) for i in items]}

    @mcp.tool()
    def get_image_metadata(path: str) -> dict:
        """Full generation metadata (prompt, sampler, seed, model...)
        for one image by absolute path."""
        if (err := require_auth()):
            return err
        item = db.get_image(path)
        if not item:
            return {"error": f"not in library: {path}"}
        return _slim(item)

    @mcp.tool()
    def get_prompt_stats(top: int = 30) -> dict:
        """Most used positive/negative prompt tokens, models and samplers
        across the whole library."""
        if (err := require_auth()):
            return err
        return stats.collect(top=min(top, 200))

    @mcp.tool()
    def find_similar_images(path: str, max_distance: int = 10, limit: int = 20):
        """Find visually similar images by perceptual hash.
        distance 0 = identical, <=10 = similar."""
        if (err := require_auth()):
            return err
        return [
            {**_slim(i), "distance": i["distance"]}
            for i in db.similar_images(path, max_distance, limit)
        ]

    @mcp.tool()
    def find_duplicates(limit: int = 50):
        """Groups of images with identical perceptual hashes (exact
        visual duplicates)."""
        if (err := require_auth()):
            return err
        return [
            {"count": g["count"], "files": [i["file"] for i in g["items"]]}
            for g in db.duplicate_groups(limit)
        ]

    @mcp.tool()
    def library_summary() -> dict:
        """Library overview: total images, per-tool counts, favorites,
        tagged count."""
        if (err := require_auth()):
            return err
        return db.summary()

    return mcp


def main() -> None:
    _build().run()


if __name__ == "__main__":
    main()
