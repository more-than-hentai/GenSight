"""Unit tests for the metadata parsers."""
import json
import sys
from pathlib import Path

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import metadata  # noqa: E402

A1111_TEXT = (
    "a beautiful landscape, masterpiece, best quality\n"
    "Negative prompt: lowres, bad anatomy\n"
    'Steps: 28, Sampler: Euler a, CFG scale: 7.0, Seed: 12345, '
    'Size: 832x1216, Model hash: 5394fca4fa, Model: someModel_v2'
)

COMFY_GRAPH = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 166465958725488,
            "steps": 8,
            "cfg": 1.0,
            "sampler_name": "euler",
            "scheduler": "simple",
            "denoise": 1.0,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "krea2TurboOfficialComfy_krea2TurboNvfp4.safetensors"},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 832, "height": 1216, "batch_size": 1},
    },
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a stylish woman, photorealistic", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry, lowres", "clip": ["4", 1]}},
}


def _png_with_text(tmp_path: Path, key: str, value: str) -> Path:
    p = tmp_path / "test.png"
    img = Image.new("RGB", (64, 64), "black")
    info = PngImagePlugin.PngInfo()
    info.add_text(key, value)
    img.save(p, pnginfo=info)
    return p


def test_a1111_png(tmp_path):
    p = _png_with_text(tmp_path, "parameters", A1111_TEXT)
    r = metadata.extract(p)
    assert r["tool"] == "a1111"
    assert r["prompt"] == "a beautiful landscape, masterpiece, best quality"
    assert r["negative_prompt"] == "lowres, bad anatomy"
    assert r["params"]["Sampler"] == "Euler a"
    assert r["params"]["CFG scale"] == "7.0"
    assert r["params"]["Seed"] == "12345"
    assert r["params"]["Model hash"] == "5394fca4fa"
    assert r["params"]["Model"] == "someModel_v2"


def test_a1111_no_negative(tmp_path):
    text = "just a prompt\nSteps: 20, Sampler: DPM++ 2M, CFG scale: 5, Seed: 1"
    p = _png_with_text(tmp_path, "parameters", text)
    r = metadata.extract(p)
    assert r["prompt"] == "just a prompt"
    assert r["negative_prompt"] == ""
    assert r["params"]["Sampler"] == "DPM++ 2M"


def test_comfyui_png(tmp_path):
    p = _png_with_text(tmp_path, "prompt", json.dumps(COMFY_GRAPH))
    r = metadata.extract(p)
    assert r["tool"] == "comfyui"
    assert r["prompt"] == "a stylish woman, photorealistic"
    assert r["negative_prompt"] == "blurry, lowres"
    assert r["params"]["Seed"] == "166465958725488"
    assert r["params"]["CFG scale"] == "1.0"
    assert r["params"]["Sampler"] == "euler simple"
    assert r["params"]["Size"] == "832x1216"
    assert r["params"]["Model"] == "krea2TurboOfficialComfy_krea2TurboNvfp4"


def test_novelai_png(tmp_path):
    comment = json.dumps(
        {"prompt": "1girl, best quality", "uc": "lowres", "steps": 28,
         "sampler": "k_euler", "scale": 5.0, "seed": 42, "width": 832, "height": 1216}
    )
    p = tmp_path / "nai.png"
    img = Image.new("RGB", (64, 64))
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", comment)
    info.add_text("Source", "NovelAI Diffusion V3")
    img.save(p, pnginfo=info)
    r = metadata.extract(p)
    assert r["tool"] == "novelai"
    assert r["prompt"] == "1girl, best quality"
    assert r["params"]["CFG scale"] == "5.0"
    assert r["params"]["Model"] == "NovelAI Diffusion V3"


def test_plain_image(tmp_path):
    p = tmp_path / "plain.png"
    Image.new("RGB", (32, 32)).save(p)
    r = metadata.extract(p)
    assert r["tool"] == "unknown"
    assert r["error"] is None
    assert r["params"]["Size"] == "32x32"
