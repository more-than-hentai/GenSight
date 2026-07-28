"""Guards on the phone layout.

These assert the shape of the CSS and markup rather than rendered geometry,
because the failures they catch are invisible without a phone-sized browser:
a tab whose Korean label wraps one glyph per line, a grid track that inflates
past the viewport, a control that renders at 15px on a touch screen. Each rule
here corresponds to a defect that was measured at 375x812 and fixed.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
CSS = (WEB / "style.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "app.js").read_text(encoding="utf-8")

PHONE_QUERY = "@media (max-width: 560px)"


def _block(text: str, start: int) -> str:
    """The brace-balanced block beginning at the first '{' at or after `start`."""
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i]
    raise AssertionError("unbalanced braces in CSS")


def _phone_block() -> str:
    return _block(CSS, CSS.index(PHONE_QUERY))


def _rule(selector: str, source: str | None = None) -> str:
    """Declarations of the first rule whose selector list matches exactly."""
    src = CSS if source is None else source
    pattern = re.compile(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{", re.M)
    m = pattern.search(src)
    assert m, f"no rule for selector {selector!r}"
    return _block(src, m.end() - 1)


# ---------------------------------------------------------------- breakpoints

def test_exactly_one_phone_breakpoint():
    assert CSS.count(PHONE_QUERY) == 1
    # Only @media widths — `max-width` also appears as a plain declaration.
    widths = [int(w) for w in re.findall(
        r"@media[^{]*max-width:\s*(\d+)px", CSS)]
    below_tablet = sorted(w for w in widths if w < 700)
    assert below_tablet == [560], (
        f"expected one phone breakpoint at 560px, found {below_tablet}")


def test_phone_block_is_last_so_the_cascade_favours_it():
    queries = [m.start() for m in re.finditer(r"@media", CSS)]
    phone = CSS.index(PHONE_QUERY)
    after = [q for q in queries if q > phone]
    # Only the landscape max-height companion may follow it.
    assert len(after) == 1, "unexpected media queries after the phone block"
    assert "max-height" in CSS[after[0]:after[0] + 60]


def test_phone_block_needs_no_important():
    assert "!important" not in _phone_block()


def test_breakpoint_is_declared_once_and_read_by_js():
    assert "--phone: 0" in _rule(":root")
    assert "--phone: 1" in _phone_block()
    assert "--phone" in JS, "app.js should read the breakpoint from CSS"
    assert "560" not in JS, "the breakpoint width must not be duplicated in JS"


# ------------------------------------------------- width-independent bug fixes

def test_tabs_do_not_wrap_per_character():
    """The header's root defect: Korean breaks at any character, so a shrunk
    tab collapsed to one glyph wide and 116px tall instead of nav scrolling."""
    tab = _rule(".tab")
    assert "white-space: nowrap" in tab
    assert "flex: 0 0 auto" in tab, "nowrap alone lets the text spill out"


def test_copy_format_buttons_do_not_wrap_per_character():
    """Same trap inside the modal's horizontally scrolling copy row."""
    rule = _rule(".copy-formats button", _phone_block())
    assert "white-space: nowrap" in rule and "flex: 0 0 auto" in rule


def test_scrolling_rows_cannot_prop_open_their_ancestors():
    assert "min-width: 0" in _rule(".copy-formats", _phone_block())


def test_toast_is_bounded():
    """It renders arbitrary server `detail` strings."""
    assert "max-width" in _rule(".toast")


def test_rows_wrap_by_default():
    assert "flex-wrap: wrap" in _rule(".row")


def test_decorative_hover_states_are_gated_on_a_real_pointer():
    """On touch, :hover latches after a tap — the .dir-chip strikethrough
    would become a permanent, meaningless line."""
    for selector in (".item:hover", ".dir-chip:hover",
                     ".trash-btn:hover", ".similar-strip img:hover"):
        idx = CSS.index(selector)
        preceding = CSS[:idx]
        assert "@media (hover: hover)" in preceding[-260:], (
            f"{selector} is not inside a (hover: hover) query")


# ------------------------------------------------------- grid track inflation

def test_flexible_grid_tracks_have_a_zero_floor():
    """A bare `1fr` floors at its content's min-content width, so one wide
    child pushes the track past the viewport and scrolls the page sideways.
    This bit both the detail modal and the stats columns."""
    bare = [decl for decl in re.findall(r"grid-template-columns:[^;}]*", CSS)
            if re.search(r"(?<!minmax\(0,\s)\b1fr", decl)
            and "auto-fill" not in decl]
    assert not bare, f"grid tracks still using a bare 1fr: {bare}"


def test_modal_and_stats_declare_minmax_tracks():
    assert "minmax(0, 1fr)" in _rule(".modal-body")
    assert "minmax(0, 1fr)" in _rule(".stats-cols")


# ----------------------------------------------------------- touch targets

def test_controls_reach_44px_on_phones():
    phone = _phone_block()
    assert "min-height: 44px" in _rule("input, select, button", phone)


def test_stars_get_a_real_hit_box():
    """Five adjacent 15x16px targets, rendered by the one starSpan() helper
    that serves both the library cards and the modal."""
    rule = _rule(".stars .s", _phone_block())
    m = re.search(r"min-width:\s*(\d+)px", rule)
    assert m and int(m.group(1)) >= 44, rule
    assert "touch-action: manipulation" in rule


def test_pinch_zoom_is_never_disabled():
    assert "user-scalable" not in HTML
    assert "maximum-scale" not in HTML
    assert 'name="viewport"' in HTML


def test_inputs_avoid_ios_zoom_on_focus():
    """Below 16px iOS Safari zooms the viewport when a field is focused."""
    assert "font-size: 16px" in _rule("input, select, button", _phone_block())


# --------------------------------------------------------- modal priority

def test_modal_image_is_capped_on_phones():
    """Uncapped, the image took 74% of the screen and pushed the metadata —
    the thing users came to copy — entirely below the fold."""
    rule = _rule(".modal-body img, .modal-body.vertical img", _phone_block())
    m = re.search(r"max-height:\s*(\d+)vh", rule)
    assert m and int(m.group(1)) <= 40, rule


def test_metadata_is_not_a_nested_scroller_on_phones():
    """A <pre> scrolling inside a scrolling .modal-body is the classic
    can't-reach-it trap on touch."""
    assert "max-height: none" in _rule("#modalMeta", _phone_block())


def test_layout_toggle_is_hidden_only_because_it_is_redundant():
    phone = _phone_block()
    assert "#layoutToggle { display: none; }" in phone
    # It is redundant precisely because both layouts get the same image cap.
    assert ".modal-body img, .modal-body.vertical img" in phone


# --------------------------------------------------------------- markup

class _Structure(HTMLParser):
    """Tracks <details> ancestry and per-element attributes."""

    VOID = {"br", "img", "input", "hr", "meta", "link", "source"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict]] = []
        self.details_depth = 0
        self.ids_in_details: set[str] = set()
        self.ids_outside_details: set[str] = set()
        self.details: list[dict] = []
        self.summaries: list[list[str]] = []   # data-i18n keys per summary
        self._summary: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag in self.VOID:
            self._record(d)
            return
        if tag == "details":
            self.details.append(d)
            self.details_depth += 1
        if tag == "summary":
            self._summary = []
        self._record(d)
        if self._summary is not None and "data-i18n" in d:
            self._summary.append(d["data-i18n"])
        self.stack.append((tag, d))

    def _record(self, d: dict) -> None:
        if "id" in d:
            target = (self.ids_in_details if self.details_depth
                      else self.ids_outside_details)
            target.add(d["id"])

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        while self.stack:
            open_tag, _ = self.stack.pop()
            if open_tag == tag:
                break
        if tag == "details":
            self.details_depth -= 1
        if tag == "summary" and self._summary is not None:
            self.summaries.append(self._summary)
            self._summary = None


def _structure() -> _Structure:
    s = _Structure()
    s.feed(HTML)
    return s


def test_search_stays_outside_the_filter_disclosure():
    """Collapsing the filters must not hide the primary control."""
    s = _structure()
    assert "libSearch" in s.ids_outside_details
    assert "libCount" in s.ids_outside_details
    for ident in ("libTool", "libSort1", "libSort3", "libExportCsv"):
        assert ident in s.ids_in_details, ident


def test_every_details_ships_open():
    """The fail-safe direction: if the collapse JS ever breaks, the layout
    degrades to the pre-existing one rather than to something unusable."""
    s = _structure()
    assert s.details, "no <details> found"
    for d in s.details:
        assert "open" in d, f"<details> without open: {d}"


def test_settings_summaries_reuse_the_existing_headings():
    """One h2[data-i18n] per summary means the accordion costs no new
    translations, and a non-translated <summary> wrapper keeps the
    no-nested-data-i18n rule (see test_i18n_markup) satisfied."""
    s = _structure()
    card_summaries = [k for k in s.summaries if k and k[0].startswith("settings.")]
    assert len(card_summaries) == 11, card_summaries
    for keys in card_summaries:
        assert len(keys) == 1, keys
    assert 'data-i18n="settings' not in HTML.split("<summary>")[0][-40:]


def test_the_settings_save_row_survives_collapsing():
    assert "position: sticky" in _rule("#tab-settings > .row:last-child",
                                       _phone_block())
