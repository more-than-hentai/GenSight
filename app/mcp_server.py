"""MCP (Model Context Protocol) server for GenSight.

Exposes the image library to AI clients (Claude Code, Claude Desktop,
etc.) over stdio. Register with:

    claude mcp add gensight -- /path/to/GenSight/.venv/bin/python -m app.mcp_server

The server reads the same SQLite library the web UI writes, so scans
performed in the browser are immediately searchable from the AI client.
"""
from __future__ import annotations

import sys


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
    def search_images(
        query: str = "", tool: str = "", favorite_only: bool = False,
        min_rating: int = 0, limit: int = 20,
    ) -> dict:
        """Search the AI image library by prompt/filename/model/tag text.
        `tool` filters by generator: a1111, comfyui, novelai, unknown."""
        total, items = db.query_images(
            q=query, tool=tool, favorite=favorite_only or None,
            min_rating=min_rating, limit=min(limit, 100),
        )
        return {"total": total, "items": [_slim(i) for i in items]}

    @mcp.tool()
    def get_image_metadata(path: str) -> dict:
        """Full generation metadata (prompt, sampler, seed, model...)
        for one image by absolute path."""
        item = db.get_image(path)
        if not item:
            return {"error": f"not in library: {path}"}
        return _slim(item)

    @mcp.tool()
    def get_prompt_stats(top: int = 30) -> dict:
        """Most used positive/negative prompt tokens, models and samplers
        across the whole library."""
        return stats.collect(top=min(top, 200))

    @mcp.tool()
    def find_similar_images(path: str, max_distance: int = 10, limit: int = 20) -> list:
        """Find visually similar images by perceptual hash.
        distance 0 = identical, <=10 = similar."""
        return [
            {**_slim(i), "distance": i["distance"]}
            for i in db.similar_images(path, max_distance, limit)
        ]

    @mcp.tool()
    def find_duplicates(limit: int = 50) -> list:
        """Groups of images with identical perceptual hashes (exact
        visual duplicates)."""
        return [
            {"count": g["count"], "files": [i["file"] for i in g["items"]]}
            for g in db.duplicate_groups(limit)
        ]

    @mcp.tool()
    def library_summary() -> dict:
        """Library overview: total images, per-tool counts, favorites,
        tagged count."""
        return db.summary()

    return mcp


def main() -> None:
    _build().run()


if __name__ == "__main__":
    main()
