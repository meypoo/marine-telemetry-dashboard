"""Offline guards against layout clipping and overlap.

The Live Index is a fixed 1920px frame emitted as one HTML/SVG string, so the
things that break it — text with no overflow rule, a nowrap title in a column
narrower than itself, a chart scale that flattens every bar — are all visible
in the generated CSS and markup without a browser.

Each check here corresponds to a defect that was actually present: long phylum
and seamark names wrapping and misaligning their bars, a long searched-region
name wrapping the amber SST chip, the product title bleeding into the search
box below ~1000px, and yearly baseline bars rendering indistinguishable on a
zero-based scale. No network, no Streamlit runtime.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ui  # noqa: E402
from terminal_render import _bar_chart, _hbars, render_topbar_left  # noqa: E402
from ui import TerminalConfig  # noqa: E402

_CSS = ui._base_css(TerminalConfig()) + ui._terminal_css(TerminalConfig())

#: _CSS with every @media block removed. _rule() counts duplicates against this,
#: because a breakpoint override is an *intentional* second declaration — the
#: duplicate guard exists to catch two competing top-level rules, where one is
#: silently lost. (The regex handles one nesting level, which is all a media
#: block containing plain rules needs.)
_FLAT_CSS = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^}]*\})*[^{}]*\}", "", _CSS)

#: Every colour the design system defines. Nothing downstream may hard-code one.
_TOKENS = {
    ui.INK, ui.PAPER, ui.PAPER_DIM, ui.MIST, ui.LINE, ui.BORDER_STRONG,
    ui.SIGNAL, ui.CANOPY, ui.PANEL,
}


def _rule(selector: str) -> str:
    """Declarations for a selector. Fails loudly if the selector is duplicated
    at the top level — two competing rules for one selector is how an override
    silently goes missing. @media re-declarations are excluded: a breakpoint
    override is deliberate and the cascade resolves it by viewport, not by
    accident of source order."""
    matches = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", _FLAT_CSS)
    assert matches, f"no CSS rule found for {selector}"
    assert len(matches) == 1, (
        f"{selector} is defined {len(matches)} times; merge them or one will be "
        "silently overridden"
    )
    return matches[0]


def test_text_elements_have_overflow_handling() -> None:
    """Anything that can receive an unbounded string must ellipsize."""
    for selector, why in [
        (".tm-hlabel", "long phylum/seamark names wrap and misalign their bar"),
        (".tm-chip", "a long searched-region name wraps the amber chip"),
        (".mx-sub", "Lab metric subtitles wrap when six columns get narrow"),
        (".tm-log > div", "console ENDPOINT cells wrap"),
    ]:
        body = _rule(selector)
        assert "overflow: hidden" in body and "text-overflow: ellipsis" in body, (
            f"{selector} has no ellipsis rule — {why}"
        )


def test_footnote_prose_wraps_instead_of_truncating() -> None:
    """Regression: the Data Lab footer reused the metric-tile class, so it was
    clipped mid-sentence — hiding the "parsed locally, not transmitted"
    statement entirely. Prose must wrap; only the narrow tiles ellipsize."""
    body = _rule(".mx-footnote")
    assert "white-space: normal" in body, ".mx-footnote must wrap"
    assert "text-overflow: ellipsis" not in body, (
        "a privacy statement must never be truncated"
    )
    # And the tile subtitle keeps its ellipsis, which is correct there.
    assert "text-overflow: ellipsis" in _rule(".mx-sub")


def test_long_kv_key_cannot_touch_its_value() -> None:
    """A key wider than the column ("temperature ~ dissolved_oxygen") ran
    straight into its value with no separating space."""
    body = _rule(".mx-kv .k")
    assert "padding-right" in body, (
        "the key needs padding so an overlong key still separates from its value"
    )
    assert "box-sizing: border-box" in body, (
        "border-box keeps short keys aligned once padding is added"
    )


def test_nav_link_has_no_light_background_or_radius() -> None:
    """Streamlit's active-page nav link carries a light, rounded background by
    default, which renders as a white box on INK and breaks the sharp-corner
    rule."""
    body = _rule('a[data-testid="stPageLink-NavLink"]')
    assert "background: transparent" in body, "nav link background not overridden"
    assert "border-radius: 0" in body, "nav link must not be rounded"


def test_panel_head_shrinks_so_the_chip_can_ellipsize() -> None:
    """An ellipsis only takes effect if the flex item is allowed to shrink."""
    assert "min-width: 0" in _rule(".tm-panelhead > div:first-child"), (
        "the chip side of a panel head must shrink or the chip cannot truncate"
    )
    assert "flex-shrink: 0" in _rule(".tm-legend"), (
        "the legend must hold its width so the chip absorbs the shrink"
    )


def test_title_truncates_instead_of_bleeding_into_the_search_box() -> None:
    markup = render_topbar_left(TerminalConfig(), "subtitle", "stamp")
    assert "white-space:nowrap" in markup, "the title should not shatter across lines"
    assert "overflow:hidden" in markup and "text-overflow:ellipsis" in markup, (
        "a nowrap title with no overflow rule bleeds into the neighbouring "
        "column once its own column is narrower than the text"
    )


def test_axis_labels_do_not_wrap() -> None:
    assert "white-space: nowrap" in _rule(".tm-xaxis > div")


def test_long_bar_label_is_truncated_but_still_readable() -> None:
    """Truncation must not destroy the information — hence the title tooltip."""
    label = "traffic_separation_scheme_precautionary_area"
    markup = _hbars([(label, 5.0, "5")], 150, 10.0)
    assert f'title="{label}"' in markup, (
        "a truncated label needs a title attribute or the full name is lost"
    )


def test_sidebar_yields_width_before_forcing_page_scroll() -> None:
    assert "@media (max-width: 1280px)" in _CSS, (
        "the fixed 360px sidebar is the only element that can push the whole "
        "page into horizontal scroll; it needs a breakpoint"
    )
    # The mobile retrofit rides on two further breakpoints: <=1000px stacks the
    # sidebar above the main column, <=640px applies the phone refinements.
    # String-level guards, because AppTest cannot measure rendered layout.
    for breakpoint_ in ("@media (max-width: 1000px)", "@media (max-width: 640px)"):
        assert breakpoint_ in _CSS, (
            f"{breakpoint_} missing — the stacked mobile layout is gone"
        )


def test_baseline_bars_are_distinguishable() -> None:
    """Regression: on a zero-based scale, yearly SST means (which differ by
    tenths of a degree) all rendered at ~90-100% height and looked identical."""
    years = list(range(2015, 2025))
    values = [14.0, 14.1, 14.05, 14.2, 14.15, 14.3, 14.25, 14.4, 14.35, 14.5]
    chart = _bar_chart(years, values, 14.6)

    heights = [
        float(h)
        for h in re.findall(r'class="tm-bar" style="height:([\d.]+)px', chart)
    ]
    assert len(heights) == len(values), (
        f"every year should draw a bar; got {len(heights)} for {len(values)} values"
    )
    # On the old zero-based scale this spread was ~5px across the whole decade.
    spread = max(heights) - min(heights)
    assert spread > 25.0, (
        f"a 0.5 degC range compressed into {spread:.1f}px of bar height — the "
        "scale is not anchored near the data"
    )

    ticks = re.findall(r"<div>(-?\d+\.\d)</div>", chart)
    assert ticks, "the y-axis should carry ticks"
    assert len(set(ticks)) == len(ticks), (
        f"axis ticks repeat after rounding: {ticks}"
    )


def test_bar_chart_threshold_stays_inside_its_track() -> None:
    chart = _bar_chart([2020, 2021], [14.0, 14.2], 99.0)  # absurd threshold
    offsets = [float(o) for o in re.findall(r"bottom:([\d.]+)px", chart)]
    assert offsets, "the threshold line should render"
    assert all(0.0 <= o <= 130.0 for o in offsets), (
        f"threshold escaped the 130px track: {offsets}"
    )


def test_bar_chart_handles_no_data() -> None:
    assert "baseline unavailable" in _bar_chart([], [], None)


def test_design_rules_hold() -> None:
    """The three rules the handoff calls out as easy to undo by accident."""
    assert "border-radius: 0 !important" in _CSS, "corners must stay sharp"
    for banned in ("transition", "animation", "@keyframes"):
        assert banned not in _CSS, f"the palette forbids {banned} (no glow/pulse)"
    assert "box-shadow: 0 0" not in _CSS, "no glow effects"


def test_no_hard_coded_hex_outside_the_token_table() -> None:
    known = {t.lower() for t in _TOKENS}
    found = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", _CSS)}
    stray = found - known
    assert not stray, (
        f"colours hard-coded outside ui.py's token table: {sorted(stray)}"
    )


ALL = [
    test_text_elements_have_overflow_handling,
    test_footnote_prose_wraps_instead_of_truncating,
    test_long_kv_key_cannot_touch_its_value,
    test_nav_link_has_no_light_background_or_radius,
    test_panel_head_shrinks_so_the_chip_can_ellipsize,
    test_title_truncates_instead_of_bleeding_into_the_search_box,
    test_axis_labels_do_not_wrap,
    test_long_bar_label_is_truncated_but_still_readable,
    test_sidebar_yields_width_before_forcing_page_scroll,
    test_baseline_bars_are_distinguishable,
    test_bar_chart_threshold_stays_inside_its_track,
    test_bar_chart_handles_no_data,
    test_design_rules_hold,
    test_no_hard_coded_hex_outside_the_token_table,
]


if __name__ == "__main__":
    failures = 0
    for fn in ALL:
        try:
            fn()
            print(f"[ OK ] {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[ERR ] {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL) - failures}/{len(ALL)} passed")
    sys.exit(1 if failures else 0)
