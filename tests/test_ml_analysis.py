"""Regression tests for the Data Lab analysis engine, against KNOWN ground truth.

The load-bearing test is `test_known_truth_recovery`: the fixture plants a
+0.30 degC/year trend under a 5 degC annual cycle, and a naive linear fit reads
it as -0.72 (the seasonal-confounding bug). This asserts the corrected engine
recovers +0.30 within its confidence interval. If this fails, the seasonal
adjustment has regressed.

Offline and deterministic — no network. Run standalone (`python
tests/test_ml_analysis.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conftest  # noqa: E402,F401  (sys.path + FIXTURES)
from conftest import FIXTURES  # noqa: E402

from ml_analysis import analyze_dataset, profile_dataset, read_uploaded  # noqa: E402

# Ground truth planted by tests/make_fixtures.py.
TRUE_TEMP_TREND = 0.30
TRUE_PH_TREND = -0.02
TRUE_PERIOD = 365.25
STEP_ROW = 700
N_SPIKES = 14


def _load(name: str):
    path = FIXTURES / name
    frame = read_uploaded(str(path), path.read_bytes())
    profile = profile_dataset(frame, name)
    return frame, profile, analyze_dataset(frame, profile)


def test_known_truth_recovery() -> None:
    _, _, report = _load("fixture_known.csv")

    temp = report.parameter("temperature_c")
    assert temp is not None and temp.trend.available
    ts = temp.trend.theil_sen_slope
    assert ts is not None, "no Theil-Sen slope"
    # The whole point: the recovered warming trend is positive and near +0.30,
    # NOT the naive-fit -0.72.
    assert 0.18 <= ts <= 0.40, f"temperature trend {ts:+.3f} not near +0.30"
    assert temp.trend.theil_sen_low <= TRUE_TEMP_TREND <= temp.trend.theil_sen_high, (
        f"CI [{temp.trend.theil_sen_low:.3f}, {temp.trend.theil_sen_high:.3f}] "
        "excludes the planted +0.30"
    )
    assert temp.trend.direction == "increasing"
    assert temp.trend.significant
    assert temp.trend.seasonally_adjusted, "seasonal cycle was not removed before fitting"

    # Periodicity snapped to the exact annual constant.
    assert temp.seasonality.detected
    assert temp.seasonality.period_used_days == TRUE_PERIOD, (
        f"period {temp.seasonality.period_used_days} not snapped to {TRUE_PERIOD}"
    )

    # Injected spikes recovered as outliers (14 planted; allow small slack).
    assert temp.robust_outliers >= 12, f"only {temp.robust_outliers} of 14 spikes flagged"


def test_ph_trend_recovered() -> None:
    _, _, report = _load("fixture_known.csv")
    ph = report.parameter("ph")
    assert ph is not None and ph.trend.available
    ts = ph.trend.theil_sen_slope
    assert -0.026 <= ts <= -0.014, f"ph trend {ts:+.4f} not near -0.020"
    assert ph.trend.theil_sen_low <= TRUE_PH_TREND <= ph.trend.theil_sen_high
    assert ph.trend.direction == "decreasing" and ph.trend.significant


def test_salinity_step_not_seasonality() -> None:
    _, _, report = _load("fixture_known.csv")
    sal = report.parameter("salinity_psu")
    assert sal is not None
    # A single step change, near row 700 — not a spurious trend or cycle.
    idx = sal.changepoints.changepoint_indices
    assert len(idx) == 1, f"expected 1 changepoint, got {idx}"
    assert abs(idx[0] - STEP_ROW) <= 25, f"changepoint at {idx[0]}, expected ~{STEP_ROW}"
    assert not sal.seasonality.detected, "flat salinity should have no seasonality"


def test_correlation_structure() -> None:
    _, _, report = _load("fixture_known.csv")
    pairs = {frozenset((p["a"], p["b"])): p["spearman"] for p in report.correlation.strongest}
    td = pairs.get(frozenset(("temperature_c", "do_mg_l")))
    tp = pairs.get(frozenset(("temperature_c", "ph")))
    assert td is not None and td < -0.8, f"temp~DO should be strongly negative, got {td}"
    assert tp is not None and tp < -0.4, f"temp~pH should be negative, got {tp}"


def test_arbitrary_numeric_columns_are_analysed() -> None:
    """The Data Lab must analyse columns whose names match no known parameter."""
    _, profile, report = _load("fixture_nonmarine.csv")
    assert set(profile.numeric_columns) == {
        "reactor_pressure_kpa", "widget_throughput", "gizmo_index"
    }
    for column in profile.numeric_columns:
        analysis = report.parameter(column)
        assert analysis is not None and analysis.n > 0
        assert analysis.trend.available, f"{column} was not analysed"
        # No canonical parameter/unit for unrecognised names — labelling only.
        assert analysis.canonical_parameter is None
    # The seasonal cycle planted in widget_throughput is still found.
    widget = report.parameter("widget_throughput")
    assert widget.seasonality.available


def test_edge_cases_degrade_cleanly() -> None:
    _, _, tiny = _load("fixture_tiny.csv")
    reading = tiny.parameter("reading")
    assert reading is not None and not reading.trend.available
    assert "8" in (reading.trend.reason or ""), "short series should cite the point minimum"

    _, _, notime = _load("fixture_notime.csv")
    const = notime.parameter("c")
    assert const is not None and not const.trend.available
    assert "constant" in (const.trend.reason or "").lower()
    a = notime.parameter("a")
    assert a is not None and a.trend.available and a.trend.per == "sample"


def test_semicolon_delimiter_sniffed() -> None:
    _, profile, _ = _load("fixture_semicolon.csv")
    assert profile.timestamp_column == "timestamp"
    assert "temperature_c" in profile.numeric_columns


def test_oversized_upload_rejected_before_parse() -> None:
    """An oversized payload is rejected on byte count, not parsed into memory."""
    import ml_analysis

    original = ml_analysis.MAX_UPLOAD_BYTES
    ml_analysis.MAX_UPLOAD_BYTES = 100
    try:
        raised = False
        try:
            ml_analysis.read_uploaded("big.csv", b"col\n" + b"1\n" * 500)
        except ValueError as exc:
            raised = "limit" in str(exc).lower()
        assert raised, "oversized upload should raise ValueError mentioning the limit"
    finally:
        ml_analysis.MAX_UPLOAD_BYTES = original


ALL = [
    test_known_truth_recovery,
    test_ph_trend_recovered,
    test_salinity_step_not_seasonality,
    test_correlation_structure,
    test_arbitrary_numeric_columns_are_analysed,
    test_edge_cases_degrade_cleanly,
    test_semicolon_delimiter_sniffed,
    test_oversized_upload_rejected_before_parse,
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
