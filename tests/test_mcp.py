"""MCP server tests: prompt extraction plus the hardening from the
Codex security review of the MCP surface."""
import sys
from pathlib import Path

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, config, db, mcp_server  # noqa: E402


def _use_tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "data" / "settings.json")
    monkeypatch.setattr(config, "THUMB_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "data" / "uploads")
    monkeypatch.setattr(mcp_server, "_unlocked", None)


def _img(path: Path, prompt="a cat, masterpiece", negative="lowres"):
    path.parent.mkdir(parents=True, exist_ok=True)
    info = PngImagePlugin.PngInfo()
    info.add_text(
        "parameters",
        f"{prompt}\nNegative prompt: {negative}\n"
        "Steps: 28, Sampler: Euler a, CFG scale: 7.0, Seed: 4242, "
        "Size: 512x512, Model: demoModel_v1",
    )
    Image.new("RGB", (64, 64), "navy").save(path, pnginfo=info)
    return path


def _tools(tmp_path, monkeypatch):
    """Call the tool functions directly (FastMCP wraps the same fns)."""
    _use_tmp_data(tmp_path, monkeypatch)
    import inspect

    from mcp.server.fastmcp import FastMCP

    captured = {}
    real_tool = FastMCP.tool

    def capture(self, *a, **kw):
        deco = real_tool(self, *a, **kw)

        def wrapper(fn):
            captured[fn.__name__] = fn
            return deco(fn)

        return wrapper

    monkeypatch.setattr(FastMCP, "tool", capture)
    mcp_server._build()
    assert inspect.isfunction(captured["extract_prompt"])
    return captured


# ------------------------------------------------- prompt extraction


def test_extract_prompt_from_unindexed_file(tmp_path, monkeypatch):
    """The gap this feature closes: a loose file that was never scanned."""
    t = _tools(tmp_path, monkeypatch)
    loose = _img(tmp_path / "imgs" / "loose.png", prompt="a loose prompt")

    r = t["extract_prompt"](str(loose))
    data = r["data"]
    assert data["prompt"] == "a loose prompt"
    assert data["negative_prompt"] == "lowres"
    assert data["params"]["Sampler"] == "Euler a"
    assert data["params"]["Seed"] == "4242"
    assert data["params"]["Model"] == "demoModel_v1"
    assert data["tool"] == "a1111"
    assert data["in_library"] is False
    assert data["source"] == "file"
    assert "untrusted" in r["note"].lower()


def test_extract_prompt_marks_indexed_files(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    p = _img(tmp_path / "imgs" / "indexed.png")
    from app.scanner import process_and_store

    process_and_store(p)
    assert t["extract_prompt"](str(p))["data"]["in_library"] is True


def test_extract_prompt_falls_back_to_library_when_file_gone(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    p = _img(tmp_path / "imgs" / "gone.png", prompt="remembered prompt")
    from app.scanner import process_and_store

    process_and_store(p)
    p.unlink()
    data = t["extract_prompt"](str(p))["data"]
    assert data["prompt"] == "remembered prompt"
    assert data["source"] == "library"


def test_extract_prompt_rejects_non_image_and_missing(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    bad = tmp_path / "notes.txt"
    bad.write_text("secret")
    assert "unsupported file type" in t["extract_prompt"](str(bad))["error"]
    assert "not found" in t["extract_prompt"](str(tmp_path / "nope.png"))["error"]


def test_get_image_metadata_hints_at_extract_prompt(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    loose = _img(tmp_path / "imgs" / "loose.png")
    r = t["get_image_metadata"](str(loose))
    assert "not in library" in r["error"]
    assert "extract_prompt" in r["hint"]


# ------------------------------------------------- security hardening


def test_login_does_not_report_success_for_bad_credentials(tmp_path, monkeypatch):
    """Regression: authorize() returned the stale unlock flag, so any
    later login looked successful."""
    t = _tools(tmp_path, monkeypatch)
    auth.set_credentials("boss", "root1")
    try:
        assert t["login"]("boss", "root1")["ok"] is True
        r = t["login"]("boss", "WRONG")
        assert r["ok"] is False, "bad credentials reported as success"
        assert "invalid" in r["error"]
    finally:
        auth.disable()


def test_unlock_revoked_by_password_change(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    auth.set_credentials("boss", "root1")
    try:
        assert t["login"]("boss", "root1")["ok"] is True
        assert "error" not in t["library_summary"]()
        auth.add_user("boss", "root2", "admin")  # password change
        assert "error" in t["library_summary"](), "stale unlock survived"
    finally:
        auth.disable()


def test_unlock_revoked_by_user_deletion(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    auth.set_credentials("boss", "root1")
    auth.add_user("mole", "pass1", "admin")
    try:
        assert t["login"]("mole", "pass1")["ok"] is True
        assert "error" not in t["library_summary"]()
        auth.delete_user("mole")
        assert "error" in t["library_summary"]()
    finally:
        auth.disable()


def test_tools_locked_until_login(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    auth.set_credentials("boss", "root1")
    try:
        for name in ("search_images", "library_summary", "get_prompt_stats"):
            assert "error" in t[name](), f"{name} was reachable while locked"
        assert "error" in t["extract_prompt"](str(tmp_path / "x.png"))
    finally:
        auth.disable()


def test_negative_limit_does_not_disable_the_limit(tmp_path, monkeypatch):
    """Regression: SQLite treats LIMIT -1 as unlimited."""
    _use_tmp_data(tmp_path, monkeypatch)
    for i in range(5):
        p = _img(tmp_path / "imgs" / f"n{i}.png")
        db.upsert_image({
            "file": str(p), "filename": p.name, "tool": "a1111",
            "prompt": "p", "negative_prompt": "", "params": {}, "error": None,
        })
    total, items = db.query_images(limit=-1)
    assert total == 5
    assert len(items) == 1, f"negative limit returned {len(items)} rows"


def test_restricted_role_cannot_read_arbitrary_paths(tmp_path, monkeypatch):
    """Round 2: extract_prompt must not let a web 'user' account bypass
    the role boundary and read any image on disk."""
    t = _tools(tmp_path, monkeypatch)
    from app.scanner import process_and_store

    indexed = _img(tmp_path / "imgs" / "indexed.png", prompt="indexed prompt")
    process_and_store(indexed)
    outside = _img(tmp_path / "elsewhere" / "private.png", prompt="private prompt")

    auth.set_credentials("boss", "root1")
    auth.add_user("guest", "pass1", "user")
    try:
        assert t["login"]("guest", "pass1")["ok"] is True
        r = t["extract_prompt"](str(outside))
        assert "error" in r and "restricted account" in r["error"]
        assert "private prompt" not in str(r)
        # the indexed image is still allowed
        assert t["extract_prompt"](str(indexed))["data"]["prompt"] == "indexed prompt"

        # an admin may read the same arbitrary path
        assert t["login"]("boss", "root1")["ok"] is True
        assert t["extract_prompt"](str(outside))["data"]["prompt"] == "private prompt"
    finally:
        auth.disable()


def test_extract_prompt_symlink_cannot_disguise_a_non_image(tmp_path, monkeypatch):
    """An image-named symlink pointing at a secret must be refused —
    the extension is checked on the resolved target, not just the name."""
    t = _tools(tmp_path, monkeypatch)
    secret = tmp_path / "secret.txt"
    secret.write_text("OPENAI_API_KEY=sk-symlink")
    disguised = tmp_path / "imgs" / "cat.png"
    disguised.parent.mkdir(parents=True, exist_ok=True)
    disguised.symlink_to(secret)

    r = t["extract_prompt"](str(disguised))
    assert "error" in r and "unsupported file type" in r["error"]
    assert "sk-symlink" not in str(r)

    # a symlink to a genuine image is still usable
    real = _img(tmp_path / "imgs" / "real.png", prompt="linked prompt")
    good = tmp_path / "imgs" / "alias.png"
    good.symlink_to(real)
    assert t["extract_prompt"](str(good))["data"]["prompt"] == "linked prompt"


def test_extract_prompt_rejects_oversized_file(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    monkeypatch.setattr(mcp_server, "MAX_EXTRACT_BYTES", 100)
    p = _img(tmp_path / "imgs" / "big.png")
    r = t["extract_prompt"](str(p))
    assert "too large" in r["error"]


def test_truncate_bounds_dict_entries(tmp_path, monkeypatch):
    _use_tmp_data(tmp_path, monkeypatch)
    hostile = {f"k{i}": "v" for i in range(mcp_server.MAX_DICT_ENTRIES + 50)}
    out = mcp_server._truncate(hostile)
    assert len(out) == mcp_server.MAX_DICT_ENTRIES + 1  # + the marker key
    assert "truncated" in out["…"]


def test_duplicate_group_fan_out_is_bounded(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    monkeypatch.setattr(mcp_server, "MAX_DUPLICATE_FILES", 3)
    for i in range(6):
        p = _img(tmp_path / "imgs" / f"d{i}.png")
        db.upsert_image({
            "file": str(p), "filename": p.name, "tool": "a1111",
            "prompt": "dup", "negative_prompt": "", "params": {}, "error": None,
        }, phash="abcdabcdabcdabcd")
    groups = t["find_duplicates"]()["data"]
    assert groups[0]["count"] == 6
    assert len(groups[0]["files"]) == 3
    assert groups[0]["truncated"] == 3


def test_untrusted_text_is_truncated(tmp_path, monkeypatch):
    t = _tools(tmp_path, monkeypatch)
    huge = "x" * (mcp_server.MAX_TEXT_CHARS + 500)
    p = _img(tmp_path / "imgs" / "big.png", prompt=huge)
    out = t["extract_prompt"](str(p))["data"]["prompt"]
    assert len(out) < len(huge)
    assert "truncated" in out
