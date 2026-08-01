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


# ---------------------------------------------------------------- LoRA
#
# Shapes copied from real files in this library. The one thing every case is
# really testing: "applied" comes from the loader's own enable flag, never from
# the weight and never from the prompt text. These loaders leave switched-off
# LoRAs in their text widget at full strength, so a weight threshold or a text
# parse reports them as applied — measured at two thirds of all entries.

def test_a1111_inline_loras_are_listed_with_weights(tmp_path):
    text = (
        "portrait <lora:krea2_doll_portrait:0.9> and "
        "<lora:krea2_kgirl_v3:0.4>, detailed\n"
        "Steps: 8, Sampler: euler, Seed: 1"
    )
    r = metadata.extract(_png_with_text(tmp_path, "parameters", text))
    assert r["params"]["Lora"] == "krea2_doll_portrait (0.9), krea2_kgirl_v3 (0.4)"
    assert "Lora (off)" not in r["params"]


def test_a1111_without_loras_adds_no_field(tmp_path):
    r = metadata.extract(_png_with_text(tmp_path, "parameters", A1111_TEXT))
    assert "Lora" not in r["params"]


def _comfy_with(node: dict, tmp_path):
    graph = dict(COMFY_GRAPH)
    graph["99"] = node
    return metadata.extract(_png_with_text(tmp_path, "prompt", json.dumps(graph)))


def test_lora_manager_reports_only_active_entries(tmp_path):
    """The crux: five disabled entries at strength 1 alongside one enabled at
    0.90, all six present in the node's own `text` widget."""
    r = _comfy_with({
        "class_type": "Lora Loader (LoraManager)",
        "inputs": {
            "text": ("<lora:fedor_bypass:1.00> <lora:krea2_kgirl_v1:1.00> "
                     "<lora:krea2_kgirl_v2:1.00> <lora:krea2_raw_krwoman_v1:1.00> "
                     "<lora:krea2_kgirl_v3:0.90> <lora:krea2filterbypass:1.00>"),
            "loras": {"__value__": [
                {"name": "fedor_bypass", "strength": 1, "active": False},
                {"name": "krea2_kgirl_v1", "strength": 1, "active": False},
                {"name": "krea2_kgirl_v2", "strength": 1, "active": False},
                {"name": "krea2_raw_krwoman_v1", "strength": 1, "active": False},
                {"name": "krea2_kgirl_v3", "strength": "0.90", "active": True},
                {"name": "krea2filterbypass", "strength": 1, "active": False},
            ]},
        },
    }, tmp_path)
    assert r["params"]["Lora"] == "krea2_kgirl_v3 (0.9)"
    assert r["params"]["Lora (off)"] == "5"


def test_lora_manager_editor_history_is_ignored(tmp_path):
    """__lm_autocomplete_meta_text holds what was last typed, not what was
    applied — its weight disagrees with the real one."""
    r = _comfy_with({
        "class_type": "Lora Loader (LoraManager)",
        "inputs": {
            "__lm_autocomplete_meta_text": {
                "lastAccepted": {"insertedText": "<lora:KNPV4.1_pre:1>"}},
            "text": "<lora:KNPV4.1_pre:0.15>",
            "loras": {"__value__": [
                {"name": "KNPV4.1_pre", "strength": "0.15", "active": True}]},
        },
    }, tmp_path)
    assert r["params"]["Lora"] == "KNPV4.1_pre (0.15)"


def test_power_lora_loader_uses_the_on_flag(tmp_path):
    r = _comfy_with({
        "class_type": "Power Lora Loader (rgthree)",
        "inputs": {
            "PowerLoraLoaderHeaderWidget": {"type": "PowerLoraLoaderHeaderWidget"},
            "lora_1": {"on": True, "lora": "krea2_doll_portrait_v5.safetensors",
                       "strength": 1},
            "lora_2": {"on": False, "lora": "unused_v2.safetensors", "strength": 0.8},
            "➕ Add Lora": "",
        },
    }, tmp_path)
    assert r["params"]["Lora"] == "krea2_doll_portrait_v5 (1)"
    assert r["params"]["Lora (off)"] == "1"


def test_easy_lora_stack_respects_toggle_and_num_loras(tmp_path):
    """Slots past num_loras keep stale values from earlier edits, and the whole
    stack is gated by one toggle."""
    inputs = {"toggle": True, "num_loras": 2}
    for i, (name, strength) in enumerate(
            [("first_v1.safetensors", 0.7), ("second_v2.safetensors", 1.1),
             ("stale_leftover.safetensors", 2.0)], start=1):
        inputs[f"lora_{i}_name"] = name
        inputs[f"lora_{i}_model_strength"] = strength
    r = _comfy_with({"class_type": "easy loraStack", "inputs": inputs}, tmp_path)
    assert r["params"]["Lora"] == "first_v1 (0.7), second_v2 (1.1)"
    assert "stale_leftover" not in r["params"]["Lora"]

    inputs["toggle"] = False
    off = _comfy_with({"class_type": "easy loraStack", "inputs": inputs}, tmp_path)
    assert "Lora" not in off["params"]
    assert off["params"]["Lora (off)"] == "2"


def test_empty_stack_slots_are_skipped(tmp_path):
    r = _comfy_with({
        "class_type": "easy loraStack",
        "inputs": {"toggle": True, "num_loras": 3,
                   "lora_1_name": "None", "lora_2_name": "", "lora_3_name": "None"},
    }, tmp_path)
    assert "Lora" not in r["params"]
    assert "Lora (off)" not in r["params"]


def test_lora_names_are_normalised_across_loaders(tmp_path):
    """A directory prefix and a .safetensors suffix must not make the same
    LoRA read as two different ones."""
    r = _comfy_with({
        "class_type": "Power Lora Loader (rgthree)",
        "inputs": {"lora_1": {"on": True, "lora": "sub/dir/style_v1.safetensors",
                              "strength": 0.5}},
    }, tmp_path)
    assert r["params"]["Lora"] == "style_v1 (0.5)"


def _workflow_png(tmp_path, nodes, with_parameters=True):
    """A file shaped like this library's current ComfyUI output: an A1111-style
    `parameters` block plus the UI graph in `workflow`, and no `prompt`."""
    p = tmp_path / "wf.png"
    img = Image.new("RGB", (64, 64), "black")
    info = PngImagePlugin.PngInfo()
    if with_parameters:
        info.add_text("parameters", A1111_TEXT)
    info.add_text("workflow", json.dumps({"nodes": nodes}))
    img.save(p, pnginfo=info)
    return p


def _multi_lora_node(entries, mode=0, node_id=186):
    # lora_data is a JSON *string* inside properties — one layer deeper than
    # the other loaders keep their slots.
    return {"id": node_id, "type": "MultiLoRALoader", "mode": mode,
            "properties": {"lora_data": json.dumps(entries)}}


def test_multi_lora_loader_read_from_the_workflow_chunk(tmp_path):
    """The file is detected as a1111 because `parameters` exists, so the LoRAs
    are only reachable through `workflow`."""
    p = _workflow_png(tmp_path, [_multi_lora_node([
        {"on": True, "lora": "Krea 2/concept/snofs_krea_v1_1.safetensors",
         "str": 0.2, "clip": 1},
        {"on": True, "lora": "trainer/krea2_doll/krea2_doll_v1.2.safetensors",
         "str": 1, "clip": 1},
        {"on": False, "lora": "Krea 2/style/unused.safetensors", "str": 1},
    ])])
    r = metadata.extract(p)
    assert r["tool"] == "a1111"
    assert r["params"]["Lora"] == "snofs_krea_v1_1 (0.2), krea2_doll_v1.2 (1)"
    assert r["params"]["Lora (off)"] == "1"


def test_bypassed_lora_node_is_ignored(tmp_path):
    """mode 4 is bypass and mode 2 is mute — the node never ran, so nothing it
    lists was applied, however its widgets are set."""
    for mode in (2, 4):
        p = _workflow_png(tmp_path, [_multi_lora_node(
            [{"on": True, "lora": "applied_anyway.safetensors", "str": 1}],
            mode=mode)])
        r = metadata.extract(p)
        assert "Lora" not in r["params"], f"mode={mode} should not contribute"


def test_workflow_loras_do_not_override_a_richer_source(tmp_path):
    """A file carrying inline A1111 tags already has its answer; the workflow
    pass must not replace it."""
    text = ("a knight <lora:inline_v1:0.55>\n"
            "Steps: 8, Sampler: euler, Seed: 1")
    p = tmp_path / "both.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", text)
    info.add_text("workflow", json.dumps({"nodes": [_multi_lora_node(
        [{"on": True, "lora": "from_graph.safetensors", "str": 1}])]}))
    Image.new("RGB", (64, 64), "black").save(p, pnginfo=info)
    r = metadata.extract(p)
    assert r["params"]["Lora"] == "inline_v1 (0.55)"


def test_malformed_workflow_chunk_is_survivable(tmp_path):
    for bad in ("not json at all", "{}", '{"nodes": "nope"}',
                '{"nodes": [{"type": "MultiLoRALoader", "mode": 0,'
                ' "properties": {"lora_data": "<<broken>>"}}]}'):
        p = tmp_path / "bad.png"
        info = PngImagePlugin.PngInfo()
        info.add_text("parameters", A1111_TEXT)
        info.add_text("workflow", bad)
        Image.new("RGB", (64, 64), "black").save(p, pnginfo=info)
        r = metadata.extract(p)
        assert r["error"] is None, bad
        assert "Lora" not in r["params"]
