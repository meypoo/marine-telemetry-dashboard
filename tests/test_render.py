"""Offline tests for the Live Index render helpers added alongside the
history, comparison and alerting features.

`terminal_render` builds one HTML/SVG string, so it is testable without a
Streamlit runtime — these assert the markup contains what the panel promises
and, importantly, that the design rules the fidelity check cares about (single
amber accent, no rounded corners, no transitions) are not undone here.
No network.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ui  # noqa: E402
from analyzer import StressAssessment, assess_region  # noqa: E402
from api_clients import (  # noqa: E402
    BuoySnapshot, Region, RegionSnapshot, SeaStateSnapshot,
)
from terminal_render import (  # noqa: E402
    _history_panel, _sst_panel, render_alert_banner, render_comparison,
)

_REGION = Region.from_point("Test Bay", 36.8, -121.9)


def _history(scores: list[float]) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(hours=len(scores))
    return [
        {
            "at": (start + timedelta(hours=i)).isoformat(),
            "score": s, "band": "MODERATE", "confidence": 0.8,
        }
        for i, s in enumerate(scores)
    ]


def _assessment(score: float | None, name: str = "Test Bay",
                band: str = "MODERATE") -> StressAssessment:
    return StressAssessment(region_code="TEST", region_name=name, score=score,
                            band=band, confidence=0.85)


def test_history_panel_empty_state_explains_itself() -> None:
    for history in ([], _history([50.0])):
        html = _history_panel(history)
        assert "collecting" in html, (
            "with fewer than two points the panel should say it is collecting, "
            "not render a misleading one-point trend"
        )
        assert "<svg" not in html, "no chart should be drawn from a single point"


def test_history_panel_charts_a_real_series() -> None:
    html = _history_panel(_history([40.0, 45.0, 52.0, 61.0]))
    assert "<svg" in html and "polyline" in html, "a multi-point series must chart"
    assert "<details" in html, "the full log drawer should be present"
    assert "+21.0" in html, f"the net change should be summarised: {html[:400]}"
    assert "61.0" in html and "40.0" in html


def test_history_panel_survives_malformed_entries() -> None:
    """The panel reads persisted JSON, so it must tolerate a torn row."""
    history = [
        {"at": "not-a-date", "score": 50.0},
        {"score": 51.0},                      # no timestamp
        *_history([40.0, 44.0]),
    ]
    html = _history_panel(history)
    assert html, "a malformed entry must not blank the panel"
    assert "<svg" in html, "the well-formed entries should still chart"


def test_history_chart_is_spaced_by_time_not_by_index() -> None:
    """Regression: points were laid out by ordinal, so a 10-day drift and a
    30-minute jump were drawn as identical slopes."""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    history = [
        {"at": base.isoformat(), "score": 30.0, "band": "GUARDED", "confidence": 0.8},
        {"at": (base + timedelta(days=10)).isoformat(), "score": 40.0,
         "band": "MODERATE", "confidence": 0.8},
        {"at": (base + timedelta(days=10, minutes=30)).isoformat(), "score": 61.0,
         "band": "ELEVATED", "confidence": 0.8},
    ]
    html = _history_panel(history)
    points = re.search(r'points="([^"]+)"', html)
    assert points, "the series should chart"
    xs = [float(p.split(",")[0]) for p in points.group(1).split()]
    assert len(xs) == 3
    # The 10-day gap is 480x the 30-minute one, so it must dominate the width.
    first_gap, second_gap = xs[1] - xs[0], xs[2] - xs[1]
    assert first_gap > second_gap * 50, (
        f"gaps rendered as {first_gap:.1f} and {second_gap:.1f} px — the chart "
        "is still spacing points by index, not by time"
    )


def test_history_panel_sorts_out_of_order_entries() -> None:
    """The chart maps time to position, so an unsorted file must not draw a
    line that runs backwards (or report a negative window)."""
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    history = [
        {"at": (base + timedelta(hours=5)).isoformat(), "score": 60.0},
        {"at": base.isoformat(), "score": 30.0},
        {"at": (base + timedelta(hours=2)).isoformat(), "score": 45.0},
    ]
    html = _history_panel(history)
    assert "-" not in re.search(r"Window</span>.*?>([^<]+)<", html, re.S).group(1), (
        "an out-of-order history produced a negative window"
    )
    points = re.search(r'points="([^"]+)"', html)
    xs = [float(p.split(",")[0]) for p in points.group(1).split()]
    assert xs == sorted(xs), f"line runs backwards in time: {xs}"


def test_sst_series_share_one_time_domain() -> None:
    """Regression: the model returns ~6 days and the buoy history 48 h, but
    both were spaced evenly across the panel — stretching the buoy trace over
    the model's window and making the in-situ/model cross-check compare unlike
    instants."""
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    model_times = [now - timedelta(hours=144 - i * 6) for i in range(24)]
    buoy_times = [now - timedelta(hours=48 - i * 2) for i in range(24)]
    snapshot = RegionSnapshot(
        region=_REGION, fetched_at=now, duration_ms=1.0,
        sea_state=SeaStateSnapshot(
            latitude=36.8, longitude=-121.9, times=model_times,
            sst_c=[15.0 + i * 0.01 for i in range(24)], current_sst_c=15.2,
        ),
        buoy=BuoySnapshot(
            station="46042", status="live", water_temp_c=15.3,
            history_times=buoy_times,
            history_wtmp_c=[15.2 + i * 0.01 for i in range(24)],
        ),
    )
    html = _sst_panel(snapshot, assess_region(snapshot), _REGION)

    polylines = re.findall(r'points="([^"]+)"', html)
    assert len(polylines) == 2, f"expected two series, got {len(polylines)}"
    starts = []
    for line in polylines:
        xs = [float(p.split(",")[0]) for p in line.split()]
        starts.append(min(xs))
    # The 48 h series covers the last third of a 144 h domain, so it must start
    # well into the panel, not at x=0 alongside the six-day series.
    late_start = max(starts)
    assert late_start > 400.0, (
        f"the shorter series starts at x={late_start:.0f} of 1000 — both series "
        "are still being stretched across the full width"
    )
    assert min(starts) < 1.0, "the longer series should start at the left edge"


def test_sst_panel_survives_missing_buoy_timestamps() -> None:
    """History values without matching times must fall back to even spacing
    rather than inventing instants — or dropping the series."""
    now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    snapshot = RegionSnapshot(
        region=_REGION, fetched_at=now, duration_ms=1.0,
        sea_state=SeaStateSnapshot(
            latitude=36.8, longitude=-121.9,
            times=[now - timedelta(hours=24 - i) for i in range(24)],
            sst_c=[15.0] * 24, current_sst_c=15.0,
        ),
        buoy=BuoySnapshot(
            station="46042", status="live", water_temp_c=15.3,
            history_times=[],                      # no timestamps at all
            history_wtmp_c=[15.2 + i * 0.01 for i in range(24)],
        ),
    )
    html = _sst_panel(snapshot, assess_region(snapshot), _REGION)
    assert len(re.findall(r'points="', html)) == 2, "both series should still draw"


def test_alert_fires_only_at_or_above_threshold() -> None:
    assert render_alert_banner(_assessment(69.9), 70.0, None) == "", (
        "below threshold must not alert"
    )
    assert render_alert_banner(_assessment(70.0), 70.0, None) != "", (
        "the threshold itself must alert (>=, not >)"
    )
    assert render_alert_banner(_assessment(None), 70.0, None) == "", (
        "a scoreless assessment must never alert"
    )


def test_alert_reports_direction_of_movement() -> None:
    rising = render_alert_banner(_assessment(82.0), 70.0, 75.0)
    assert "up 7.0" in rising, f"should report the rise: {rising}"
    # The direction word carries the sign, so the magnitude must not repeat it
    # ("down -8.0" reads as a double negative).
    falling = render_alert_banner(_assessment(72.0), 70.0, 80.0)
    assert "down 8.0" in falling, f"should report the fall: {falling}"
    assert "-8" not in falling and "+8" not in falling
    first = render_alert_banner(_assessment(82.0), 70.0, None)
    assert "no previous reading" in first


def test_alert_says_unchanged_rather_than_down_zero() -> None:
    """Regression: equality fell through to the 'down' branch, producing the
    self-contradicting 'down +0.0'."""
    level = render_alert_banner(_assessment(75.0), 70.0, 75.0)
    assert "unchanged" in level, f"an unmoved score should read as unchanged: {level}"
    assert "down" not in level and "up" not in level


def test_alert_threshold_keeps_its_decimal() -> None:
    """?alert= is user-settable, so the threshold echoed back must be the one
    that actually applied — 72.5 was being displayed as 72."""
    banner = render_alert_banner(_assessment(80.0), 72.5, None)
    assert "72.5 threshold" in banner, banner
    whole = render_alert_banner(_assessment(80.0), 70.0, None)
    assert "70 threshold" in whole, "a whole number should not gain a decimal"


def test_alert_states_the_scale_direction() -> None:
    banner = render_alert_banner(_assessment(88.0), 70.0, None)
    assert "Higher scores mean more stress" in banner, (
        "an alert on a 'Health Index' must say which way the scale runs"
    )


def test_alert_uses_the_amber_accent_not_a_red_hue() -> None:
    """The palette has two accents and no alert-red; an alert is still amber."""
    html = render_alert_banner(_assessment(95.0, band="CRITICAL"), 70.0, 60.0)
    assert ui.SIGNAL in html, "the alert should carry the amber SIGNAL token"
    for banned in ("#FF3B30", "#F00", "red"):
        assert banned.lower() not in html.lower(), f"alert introduced {banned}"
    for banned in ("border-radius", "transition", "animation", "box-shadow"):
        assert banned not in html, f"alert introduced a banned effect: {banned}"


def test_comparison_reports_delta_and_both_regions() -> None:
    html = render_comparison(_assessment(82.0, "Hot Bay"), _assessment(41.0, "Cool Bay"))
    assert "Hot Bay" in html and "Cool Bay" in html
    assert "+41.0" in html, f"the delta should be shown: {html[:400]}"
    assert "more stressed" in html


def test_comparison_handles_a_missing_score() -> None:
    html = render_comparison(_assessment(50.0, "Scored"), _assessment(None, "Unscored"))
    assert "not comparable" in html, (
        "a missing score must be stated, not rendered as a zero delta"
    )
    assert "Unscored" in html


def test_comparison_escapes_region_names() -> None:
    """Region names can come from user search text via the geocoder."""
    html = render_comparison(
        _assessment(50.0, '<script>alert(1)</script>'), _assessment(40.0, "Other")
    )
    assert "<script>" not in html, "region name was not escaped"
    assert "&lt;script&gt;" in html


def test_previous_score_accounts_for_rate_limited_history() -> None:
    """``_append_history`` only records every 30 min, so the newest entry is
    sometimes this load's reading and sometimes an earlier one. Taking
    ``history[-2]`` unconditionally quoted a two-ago value as "the previous
    reading" — which is the common case at the normal refresh cadence."""
    import page_live

    now = datetime.now(timezone.utc)
    older = {"at": (now - timedelta(hours=3)).isoformat(), "score": 40.0}
    recent = {"at": (now - timedelta(minutes=20)).isoformat(), "score": 55.0}

    # Rate-limited: nothing was appended this cycle, so the newest stored point
    # IS the previous reading.
    assert page_live._previous_score([older, recent], 61.0) == 55.0

    # Just appended: the newest entry is this load's own reading, so the
    # previous one is the entry before it.
    just_written = {"at": now.isoformat(), "score": 61.0}
    assert page_live._previous_score([older, recent, just_written], 61.0) == 55.0

    assert page_live._previous_score([], 50.0) is None
    assert page_live._previous_score([just_written], 61.0) is None, (
        "a single entry that is this load's own reading has no predecessor"
    )


def test_jargon_terms_carry_a_hover_gloss() -> None:
    """Specialist terms printed on the face get a title tooltip so a
    non-specialist can read the dashboard without leaving it."""
    from ui import gloss_attr

    for term in ("OISST baseline", "Theil-Sen slope", "Mann-Kendall p",
                 "Seamark density", "Anomaly (sigma)", "silhouette",
                 "Weighted sensitivity"):
        attr = gloss_attr(term)
        assert attr.startswith(' title="') and len(attr) > 12, (
            f"{term!r} should carry a gloss, got {attr!r}"
        )
    # An ordinary label must NOT get a spurious tooltip.
    assert gloss_attr("Water temperature") == ""
    assert gloss_attr("Distance from centroid") == ""


def test_gloss_is_html_escaped() -> None:
    """The gloss text is injected as an attribute, so it must be escaped."""
    import ui

    original = dict(ui.GLOSSARY)
    ui.GLOSSARY["xsspixel"] = 'evil " onmouseover=alert(1)'
    try:
        attr = ui.gloss_attr("xsspixel here")
        assert '"onmouseover' not in attr.replace(" ", "")
        assert "&quot;" in attr or "&#34;" in attr, "quote was not escaped"
    finally:
        ui.GLOSSARY.clear()
        ui.GLOSSARY.update(original)


def test_new_panels_hardcode_no_hex_outside_the_token_table() -> None:
    """Every colour in the new markup must come from ui's tokens."""
    tokens = {
        ui.INK, ui.PAPER, ui.PAPER_DIM, ui.MIST, ui.LINE, ui.BORDER_STRONG,
        ui.SIGNAL, ui.CANOPY, ui.PANEL,
    }
    known = {t.lower() for t in tokens}
    markup = "".join(
        [
            _history_panel(_history([40.0, 55.0, 62.0])),
            render_alert_banner(_assessment(88.0), 70.0, 70.0),
            render_comparison(_assessment(80.0), _assessment(30.0, "Other")),
        ]
    )
    found = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", markup)}
    stray = found - known
    assert not stray, f"hard-coded hex outside the token table: {sorted(stray)}"


ALL = [
    test_history_panel_empty_state_explains_itself,
    test_history_panel_charts_a_real_series,
    test_history_panel_survives_malformed_entries,
    test_history_chart_is_spaced_by_time_not_by_index,
    test_history_panel_sorts_out_of_order_entries,
    test_sst_series_share_one_time_domain,
    test_sst_panel_survives_missing_buoy_timestamps,
    test_alert_fires_only_at_or_above_threshold,
    test_alert_reports_direction_of_movement,
    test_alert_says_unchanged_rather_than_down_zero,
    test_alert_threshold_keeps_its_decimal,
    test_alert_states_the_scale_direction,
    test_alert_uses_the_amber_accent_not_a_red_hue,
    test_comparison_reports_delta_and_both_regions,
    test_comparison_handles_a_missing_score,
    test_comparison_escapes_region_names,
    test_previous_score_accounts_for_rate_limited_history,
    test_jargon_terms_carry_a_hover_gloss,
    test_gloss_is_html_escaped,
    test_new_panels_hardcode_no_hex_outside_the_token_table,
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
