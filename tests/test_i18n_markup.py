"""Guards on the translation markup itself.

The frontend applies translations with `el.textContent = t(key)`, which
discards child nodes. That makes two mistakes silent in the browser: a
data-i18n element nested inside another (the outer pass erases the inner,
so the string simply never appears), and a key referenced by markup but
absent from the catalogues (the hard-coded fallback shows in every
language). Both are cheap to assert here and invisible otherwise.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
LANGS = ("ko", "en", "ja")


def _catalog(lang: str) -> dict:
    return json.loads((WEB / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0          # open data-i18n ancestors
        self.stack: list[bool] = []
        self.keys: list[str] = []
        self.nested: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "img", "input", "hr", "meta", "link"):
            return
        d = dict(attrs)
        key = d.get("data-i18n")
        if key:
            self.keys.append(key)
            if self.depth:
                self.nested.append(key)
            self.depth += 1
            self.stack.append(True)
        else:
            self.stack.append(False)

    def handle_endtag(self, tag):
        if tag in ("br", "img", "input", "hr", "meta", "link"):
            return
        if self.stack and self.stack.pop():
            self.depth -= 1


def _parse() -> _Collector:
    c = _Collector()
    c.feed((WEB / "index.html").read_text(encoding="utf-8"))
    return c


def test_no_data_i18n_element_contains_another():
    nested = _parse().nested
    assert not nested, (
        "these keys sit inside another data-i18n element and would be erased "
        f"when the outer element is translated: {nested}"
    )


def test_every_markup_key_exists_in_every_language():
    keys = set(_parse().keys)
    keys |= set(re.findall(
        r'data-i18n-ph="([^"]+)"',
        (WEB / "index.html").read_text(encoding="utf-8"),
    ))
    for lang in LANGS:
        missing = sorted(keys - set(_catalog(lang)))
        assert not missing, f"{lang}.json is missing {missing}"


def test_catalogues_have_identical_key_sets():
    sets = {lang: set(_catalog(lang)) for lang in LANGS}
    base = sets["ko"]
    for lang, s in sets.items():
        assert s == base, (
            f"{lang}.json differs from ko.json — "
            f"missing {sorted(base - s)}, extra {sorted(s - base)}"
        )
