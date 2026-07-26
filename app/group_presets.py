"""Starter group rules.

Two sets, both installable from the settings UI:

- "standard": broad categories that apply to most AI image libraries
  (subject, scene, style, quality flags). Matches on the prompt.
- "example": a couple of rules that demonstrate the other targets
  (filename, model) and regex matching, so the rule syntax is obvious
  without reading docs.

Nothing is installed automatically — presets are a starting point the
user edits, not policy.
"""
from __future__ import annotations

# (name, pattern, is_regex, target)
STANDARD: list[tuple[str, str, bool, str]] = [
    # subject
    ("portrait", r"\b(portrait|close-?up|headshot|face focus)\b", True, "prompt"),
    ("full-body", r"\b(full[- ]?body|full shot|standing|cowboy shot)\b", True, "prompt"),
    ("group", r"\b(2girls|3girls|2boys|multiple (girls|boys|people)|crowd)\b",
     True, "prompt"),
    # scene
    ("landscape", r"\b(landscape|mountain|forest|ocean|sunset|scenery|cityscape)\b",
     True, "prompt"),
    ("interior", r"\b(indoor|interior|room|cafe|kitchen|bedroom|office)\b",
     True, "prompt"),
    ("street", r"\b(street|alley|crosswalk|sidewalk|urban)\b", True, "prompt"),
    # style
    ("photoreal", r"\b(photorealistic|photo[- ]?realistic|realistic|raw photo|dslr)\b",
     True, "prompt"),
    ("anime", r"\b(anime|manga|illustration|cel[- ]?shad(ed|ing))\b", True, "prompt"),
    ("3d-render", r"\b(3d render|octane|unreal engine|blender|cgi)\b", True, "prompt"),
    ("watercolor", r"\b(watercolou?r|oil painting|sketch|ink drawing)\b",
     True, "prompt"),
    # lighting / framing
    ("night", r"\b(night|moonlight|neon|dark background|low light)\b", True, "prompt"),
]

EXAMPLE: list[tuple[str, str, bool, str]] = [
    # plain substring on the prompt — the simplest possible rule
    ("example-cat", "cat", False, "prompt"),
    # match on the model name instead of the prompt
    ("example-by-model", "turbo", False, "model"),
    # regex on the filename: files that start with a date stamp
    ("example-dated-files", r"^\d{8}", True, "filename"),
]

PRESETS = {"standard": STANDARD, "example": EXAMPLE}


def entries(preset: str) -> list[dict]:
    rules = PRESETS.get(preset)
    if rules is None:
        raise ValueError(f"unknown preset: {preset}; choose from {list(PRESETS)}")
    return [
        {"name": name, "pattern": pattern, "is_regex": is_regex, "target": target}
        for name, pattern, is_regex, target in rules
    ]
