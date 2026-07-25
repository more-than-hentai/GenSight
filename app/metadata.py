"""AI-generated image metadata extraction.

Supported sources:
- AUTOMATIC1111 / Forge / SD.Next: PNG tEXt "parameters" chunk, or the
  same text embedded in JPEG/WebP EXIF UserComment.
- ComfyUI: PNG "prompt" (API graph) and "workflow" chunks. The graph is
  walked to recover positive/negative prompts, sampler settings, model
  and latent size.
- NovelAI: PNG "Comment" JSON chunk.

The normalized result mirrors the board-friendly output format:
prompt / negative_prompt / Sampler / CFG scale / Seed / Size / Steps /
Model / Model hash, plus tool detection and the raw payload.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from PIL import Image, ExifTags

Image.MAX_IMAGE_PIXELS = None  # trust local files; avoid DecompressionBomb errors

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tiff"}

_EXIF_USER_COMMENT = next(
    (k for k, v in ExifTags.TAGS.items() if v == "UserComment"), 0x9286
)

# A1111 "Steps: 20, Sampler: Euler a, CFG scale: 7, ..." key-value pairs.
# Values may contain quoted strings with commas.
_A1111_KV = re.compile(r'\s*([\w ]+):\s*("(?:\\.|[^"\\])*"|[^,]*)(?:,|$)')

_COMFY_SAMPLER_CLASSES = {
    "KSampler",
    "KSamplerAdvanced",
    "KSampler (Efficient)",
    "SamplerCustom",
    "SamplerCustomAdvanced",
}
_COMFY_MODEL_KEYS = ("ckpt_name", "unet_name", "model_name")
_COMFY_LATENT_CLASSES = (
    "EmptyLatentImage",
    "EmptySD3LatentImage",
    "EmptyFluxLatentImage",
    "LatentImage",
)


def extract(path: str | Path) -> dict[str, Any]:
    """Extract and normalize generation metadata from one image file."""
    p = Path(path)
    result: dict[str, Any] = {
        "file": str(p),
        "filename": p.name,
        "tool": "unknown",
        "prompt": "",
        "negative_prompt": "",
        "params": {},
        "raw": {},
        "error": None,
    }
    try:
        with Image.open(p) as img:
            result["params"]["Size"] = f"{img.width}x{img.height}"
            info = dict(getattr(img, "info", {}) or {})
            exif_text = _read_exif_user_comment(img)
        result["raw"] = {
            k: v for k, v in info.items() if isinstance(v, str) and len(v) < 200_000
        }

        if "parameters" in info and isinstance(info["parameters"], str):
            _parse_a1111(info["parameters"], result)
            result["tool"] = "a1111"
        elif "prompt" in info and _looks_like_json(info.get("prompt")):
            _parse_comfyui(info["prompt"], result)
            result["tool"] = "comfyui"
        elif "Comment" in info and _looks_like_json(info.get("Comment")):
            _parse_novelai(info, result)
            result["tool"] = "novelai"
        elif exif_text:
            result["raw"]["exif_user_comment"] = exif_text
            if _looks_like_json(exif_text):
                _parse_comfyui(exif_text, result)
                result["tool"] = "comfyui"
            else:
                _parse_a1111(exif_text, result)
                result["tool"] = "a1111"
    except Exception as e:  # noqa: BLE001 - per-file failures must not kill a scan
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _looks_like_json(text: Any) -> bool:
    return isinstance(text, str) and text.lstrip()[:1] in ("{", "[")


def _read_exif_user_comment(img: Image.Image) -> str:
    try:
        exif = img.getexif()
        raw = exif.get(_EXIF_USER_COMMENT)
        if raw is None:
            raw = exif.get_ifd(0x8769).get(_EXIF_USER_COMMENT)
        if isinstance(raw, bytes):
            if raw.startswith(b"UNICODE\x00"):
                return raw[8:].decode("utf-16-be", errors="ignore").strip("\x00")
            if raw.startswith(b"ASCII\x00\x00\x00"):
                return raw[8:].decode("ascii", errors="ignore")
            return raw.decode("utf-8", errors="ignore").strip("\x00")
        if isinstance(raw, str):
            return raw
    except Exception:  # noqa: BLE001
        pass
    return ""


# ---------------------------------------------------------------- A1111


def _parse_a1111(text: str, result: dict) -> None:
    text = text.strip()
    neg_idx = text.find("\nNegative prompt:")
    settings_match = re.search(r"\n(Steps|Sampler|CFG scale|Seed): ", text)

    if neg_idx != -1:
        result["prompt"] = text[:neg_idx].strip()
        rest = text[neg_idx + len("\nNegative prompt:") :]
        settings_split = re.search(r"\n(?=(?:Steps|Sampler|CFG scale|Seed): )", rest)
        if settings_split:
            result["negative_prompt"] = rest[: settings_split.start()].strip()
            _parse_a1111_settings(rest[settings_split.start() :], result)
        else:
            result["negative_prompt"] = rest.strip()
    elif settings_match:
        result["prompt"] = text[: settings_match.start()].strip()
        _parse_a1111_settings(text[settings_match.start() :], result)
    else:
        result["prompt"] = text


def _parse_a1111_settings(text: str, result: dict) -> None:
    for key, value in _A1111_KV.findall(text.strip()):
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        if key and value:
            result["params"][key] = value


# ---------------------------------------------------------------- ComfyUI


def _parse_comfyui(prompt_json: str, result: dict) -> None:
    try:
        graph = json.loads(prompt_json)
    except json.JSONDecodeError:
        return
    if not isinstance(graph, dict):
        return

    nodes = {
        str(k): v
        for k, v in graph.items()
        if isinstance(v, dict) and "class_type" in v
    }
    if not nodes:
        return

    sampler = _find_comfy_sampler(nodes)
    if sampler:
        inputs = sampler.get("inputs", {})
        _set_param(result, "Seed", inputs.get("seed", inputs.get("noise_seed")))
        _set_param(result, "Steps", inputs.get("steps"))
        _set_param(result, "CFG scale", inputs.get("cfg"))
        sampler_name = inputs.get("sampler_name", "")
        scheduler = inputs.get("scheduler", "")
        if sampler_name or scheduler:
            result["params"]["Sampler"] = f"{sampler_name} {scheduler}".strip()
        _set_param(result, "Denoise", inputs.get("denoise"))

        result["prompt"] = _resolve_comfy_text(nodes, inputs.get("positive"))
        result["negative_prompt"] = _resolve_comfy_text(nodes, inputs.get("negative"))

    for node in nodes.values():
        inputs = node.get("inputs", {})
        for key in _COMFY_MODEL_KEYS:
            name = inputs.get(key)
            if isinstance(name, str) and name:
                model = re.sub(r"\.(safetensors|ckpt|gguf|pt)$", "", Path(name).name)
                result["params"].setdefault("Model", model)
        if node.get("class_type") in _COMFY_LATENT_CLASSES:
            w, h = inputs.get("width"), inputs.get("height")
            if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                result["params"]["Size"] = f"{int(w)}x{int(h)}"

    # Fallback: no sampler found — grab the longest CLIPTextEncode text
    if not result["prompt"]:
        texts = [
            t
            for n in nodes.values()
            if "CLIPTextEncode" in str(n.get("class_type"))
            and isinstance(t := n.get("inputs", {}).get("text"), str)
        ]
        if texts:
            result["prompt"] = max(texts, key=len)


def _find_comfy_sampler(nodes: dict) -> dict | None:
    candidates = []
    for node in nodes.values():
        cls = str(node.get("class_type", ""))
        inputs = node.get("inputs", {})
        if cls in _COMFY_SAMPLER_CLASSES or (
            "Sampler" in cls and ("seed" in inputs or "noise_seed" in inputs)
        ):
            candidates.append(node)
    # Prefer the one holding prompt links
    for node in candidates:
        if "positive" in node.get("inputs", {}):
            return node
    return candidates[0] if candidates else None


def _resolve_comfy_text(nodes: dict, link: Any, depth: int = 0) -> str:
    """Follow a [node_id, slot] link until reaching a text input."""
    if depth > 12:
        return ""
    if isinstance(link, str):
        return link
    if not (isinstance(link, list) and link):
        return ""
    node = nodes.get(str(link[0]))
    if not node:
        return ""
    inputs = node.get("inputs", {})
    for key in ("text", "text_g", "string", "prompt", "wildcard_text", "populated_text"):
        val = inputs.get(key)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, list):
            resolved = _resolve_comfy_text(nodes, val, depth + 1)
            if resolved:
                return resolved
    # Conditioning combinators / rerouting: follow any upstream link
    for val in inputs.values():
        if isinstance(val, list) and len(val) == 2:
            resolved = _resolve_comfy_text(nodes, val, depth + 1)
            if resolved:
                return resolved
    return ""


def _set_param(result: dict, key: str, value: Any) -> None:
    if value is not None and not isinstance(value, (list, dict)):
        result["params"][key] = str(value)


# ---------------------------------------------------------------- NovelAI


def _parse_novelai(info: dict, result: dict) -> None:
    try:
        comment = json.loads(info["Comment"])
    except (json.JSONDecodeError, TypeError):
        return
    result["prompt"] = comment.get("prompt", info.get("Description", ""))
    result["negative_prompt"] = comment.get("uc", "")
    mapping = {
        "steps": "Steps",
        "sampler": "Sampler",
        "scale": "CFG scale",
        "seed": "Seed",
    }
    for src, dst in mapping.items():
        if src in comment:
            result["params"][dst] = str(comment[src])
    if "width" in comment and "height" in comment:
        result["params"]["Size"] = f"{comment['width']}x{comment['height']}"
    if info.get("Source"):
        result["params"]["Model"] = str(info["Source"])
