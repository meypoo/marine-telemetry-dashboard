"""Offline tests for the scoring core.

The Ecological Stress Score is the product's headline number and its blend
arithmetic — weight renormalisation when a component drops, band thresholds,
the logistic mapping — was previously only exercised by the live end-to-end
test, which cannot run without network. These build synthetic snapshots and
assert the arithmetic directly. No network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyzer  # noqa: E402
from analyzer import (  # noqa: E402
    NOMINAL_WEIGHTS, _band_for, _logistic, assess_region,
)
from api_clients import (  # noqa: E402
    BuoySnapshot, ClimatologySnapshot, InfrastructureSnapshot, ObisSnapshot,
    Region, RegionSnapshot, SeaStateSnapshot,
)

_REGION = Region.from_point("Test Bay", 36.8, -121.9)


def _climatology(*, years_covered: int = 10, years_requested: int = 10,
                 mean: float = 14.0) -> ClimatologySnapshot:
    return ClimatologySnapshot(
        baseline_mean=mean, baseline_std=0.5, baseline_p90=mean + 1.0,
        observations=200, years_covered=years_covered,
        years_requested=years_requested,
        trend_c_per_decade=0.30, trend_stderr_c_per_decade=0.05,
        trend_r2=0.6, trend_lower_c_per_decade=0.22,
    )


def _obis() -> ObisSnapshot:
    return ObisSnapshot(
        records=50_000, species=800, taxa=900, datasets=40,
        species_level_records=30_000, year_min=1990,
        year_max=datetime.now(timezone.utc).year,
        phylum_records={"Chordata": 500, "Mollusca": 300, "Cnidaria": 200},
        checklist_sampled=200,
    )


def _infrastructure() -> InfrastructureSnapshot:
    return InfrastructureSnapshot(
        node_count=80, way_count=20, total_count=100,
        type_breakdown={"buoy_lateral": 60, "separation_lane": 40},
        traffic_features=40, area_km2=2_000.0, sampled_elements=100,
    )


def _snapshot(**feeds) -> RegionSnapshot:
    return RegionSnapshot(
        region=_REGION, fetched_at=datetime.now(timezone.utc), duration_ms=1.0,
        **feeds,
    )


def _full_snapshot() -> RegionSnapshot:
    return _snapshot(
        climatology=_climatology(),
        buoy=BuoySnapshot(station="46042", status="live", water_temp_c=15.0,
                          age_hours=1.0),
        sea_state=SeaStateSnapshot(latitude=36.8, longitude=-121.9,
                                   current_sst_c=15.1, sst_c=[15.1]),
        obis=_obis(),
        infrastructure=_infrastructure(),
    )


def test_logistic_is_bounded_and_monotonic() -> None:
    assert _logistic(-1e6, 1.0, 1.2) == 0.0, "extreme negative must clamp to 0"
    assert _logistic(1e6, 1.0, 1.2) == 100.0, "extreme positive must clamp to 100"
    assert abs(_logistic(1.0, 1.0, 1.2) - 50.0) < 1e-9, "midpoint must score 50"
    rising = [_logistic(x, 1.0, 1.2) for x in (-2.0, 0.0, 1.0, 2.0, 4.0)]
    assert rising == sorted(rising), "logistic must be monotonically increasing"


def test_band_thresholds_are_exact_at_boundaries() -> None:
    cases = [
        (100.0, "CRITICAL"), (70.0, "CRITICAL"), (69.9, "ELEVATED"),
        (55.0, "ELEVATED"), (54.9, "MODERATE"), (40.0, "MODERATE"),
        (39.9, "GUARDED"), (30.0, "GUARDED"), (29.9, "LOW"), (0.0, "LOW"),
    ]
    for score, expected in cases:
        actual = _band_for(score)
        assert actual == expected, f"score {score} banded {actual}, expected {expected}"


def test_all_components_available_uses_nominal_weights() -> None:
    result = assess_region(_full_snapshot())
    assert result.score is not None, "full snapshot must produce a score"
    assert set(result.effective_weights) == {"thermal", "taxonomic", "pressure"}
    for key, nominal in NOMINAL_WEIGHTS.items():
        actual = result.effective_weights[key]
        assert abs(actual - nominal) < 1e-6, (
            f"{key} weight {actual} should equal nominal {nominal} when all "
            "components are available"
        )
    assert abs(sum(result.effective_weights.values()) - 1.0) < 1e-6


def test_dropped_component_renormalises_surviving_weights() -> None:
    """With OBIS gone, thermal and pressure must re-share the full weight in
    their original proportion — not leave the score deflated by the 0.30 hole."""
    snapshot = _full_snapshot()
    snapshot = _snapshot(
        climatology=snapshot.climatology, buoy=snapshot.buoy,
        sea_state=snapshot.sea_state, infrastructure=snapshot.infrastructure,
    )  # obis omitted
    result = assess_region(snapshot)

    assert result.score is not None, "score must survive one dead component"
    assert "taxonomic" not in result.effective_weights
    # effective_weights are rounded to 4dp for display, so compare at that scale.
    surviving = NOMINAL_WEIGHTS["thermal"] + NOMINAL_WEIGHTS["pressure"]
    expected = NOMINAL_WEIGHTS["thermal"] / surviving
    actual = result.effective_weights["thermal"]
    assert abs(actual - expected) < 1e-3, (
        f"thermal weight {actual} should renormalise to {expected:.4f}"
    )
    assert abs(sum(result.effective_weights.values()) - 1.0) < 1e-3, (
        "renormalised weights must still sum to 1"
    )
    assert any("taxonomic" in d for d in result.degradations), (
        "a dropped component must be explained in degradations"
    )


def test_blend_is_the_weighted_mean_of_components() -> None:
    result = assess_region(_full_snapshot())
    expected = sum(
        result.effective_weights[c.key] * float(c.score)
        for c in result.components
        if c.available and c.score is not None
    )
    assert abs(result.score - round(expected, 1)) < 1e-9, (
        f"score {result.score} is not the weighted mean {expected}"
    )
    assert 0.0 <= result.score <= 100.0


def test_no_components_yields_none_not_zero() -> None:
    """A total outage must report 'no data', never a confident zero."""
    result = assess_region(_snapshot())
    assert result.score is None, "empty snapshot must not fabricate a score"
    assert result.band == "NO DATA"
    assert result.confidence == 0.0
    assert result.effective_weights == {}


def test_confidence_drops_when_a_component_is_missing() -> None:
    full = assess_region(_full_snapshot())
    partial_snapshot = _snapshot(
        climatology=_climatology(),
        buoy=BuoySnapshot(station="46042", status="live", water_temp_c=15.0,
                          age_hours=1.0),
        sea_state=SeaStateSnapshot(latitude=36.8, longitude=-121.9,
                                   current_sst_c=15.1, sst_c=[15.1]),
        obis=_obis(),
    )  # infrastructure omitted
    partial = assess_region(partial_snapshot)
    assert partial.confidence < full.confidence, (
        f"confidence {partial.confidence} should be below the full-fetch "
        f"{full.confidence} when a component is missing"
    )


def test_thermal_quality_tracks_years_requested_not_a_hardcoded_decade() -> None:
    """Regression: quality was `years_covered / 10`, so a 3-year smoke run
    scored 0.3 quality despite complete coverage."""
    snapshot = _snapshot(
        climatology=_climatology(years_covered=3, years_requested=3),
        buoy=BuoySnapshot(station="46042", status="live", water_temp_c=15.0,
                          age_hours=1.0),
        sea_state=SeaStateSnapshot(latitude=36.8, longitude=-121.9,
                                   current_sst_c=15.1, sst_c=[15.1]),
    )
    thermal = assess_region(snapshot).component("thermal")
    assert thermal is not None and thermal.available
    assert abs(thermal.quality - 1.0) < 1e-6, (
        f"complete 3-of-3 coverage scored quality {thermal.quality}, expected 1.0"
    )


def test_partial_baseline_coverage_reduces_quality() -> None:
    snapshot = _snapshot(
        climatology=_climatology(years_covered=5, years_requested=10),
        buoy=BuoySnapshot(station="46042", status="live", water_temp_c=15.0,
                          age_hours=1.0),
        sea_state=SeaStateSnapshot(latitude=36.8, longitude=-121.9,
                                   current_sst_c=15.1, sst_c=[15.1]),
    )
    thermal = assess_region(snapshot).component("thermal")
    assert thermal is not None
    assert abs(thermal.quality - 0.5) < 1e-6, (
        f"5-of-10 coverage scored quality {thermal.quality}, expected 0.5"
    )


def test_only_lower_bound_of_trend_is_scored() -> None:
    """A large point-estimate slope whose lower bound is zero must contribute
    nothing — the guard against noise-driven 1.5 degC/decade readings."""
    noisy = ClimatologySnapshot(
        baseline_mean=14.0, baseline_std=0.5, observations=200,
        years_covered=10, years_requested=10,
        trend_c_per_decade=1.50,            # huge point estimate
        trend_stderr_c_per_decade=1.20,     # ...swamped by its own error
        trend_r2=0.05,
        trend_lower_c_per_decade=0.0,       # does not clear its uncertainty
    )
    snapshot = _snapshot(
        climatology=noisy,
        sea_state=SeaStateSnapshot(latitude=36.8, longitude=-121.9,
                                   current_sst_c=14.0, sst_c=[14.0]),
    )
    thermal = assess_region(snapshot).component("thermal")
    assert thermal is not None and thermal.available
    assert thermal.detail["trend_term"] == 0.0, (
        "a trend that does not clear its own uncertainty must score zero"
    )
    assert thermal.detail["trend_c_per_decade"] == 1.5, (
        "the point estimate should still be reported for display"
    )


def test_empty_bounding_box_is_a_reading_not_missing_data() -> None:
    """Regression: with the single-query Overpass fetch, a region that genuinely
    has no charted seamarks returns total=0 AND sampled=0 — the same shape that
    used to mean "the tag query failed". Reporting it as unavailable data (and
    docking quality for it) was false on both counts."""
    empty = InfrastructureSnapshot(total_count=0, sampled_elements=0,
                                   area_km2=5_000.0)
    component = analyzer._score_pressure(empty)

    assert component.available
    assert component.quality == 1.0, (
        f"a successfully measured empty box scored quality {component.quality}; "
        "nothing was missing, so confidence should not be reduced"
    )
    joined = " ".join(component.notes)
    assert "tag sample unavailable" not in joined, (
        f"claimed the sample was unavailable when it was not: {component.notes}"
    )
    assert "no charted seamarks" in joined, (
        "the empty result should still be explained"
    )


def test_untagged_features_still_report_the_sample_as_unavailable() -> None:
    """The other side of the same branch: features exist but their tags could
    not be fetched, so composition really is unknown and quality really drops."""
    untagged = InfrastructureSnapshot(total_count=227, node_count=200,
                                      way_count=27, sampled_elements=0,
                                      area_km2=3_000.0)
    component = analyzer._score_pressure(untagged)

    assert component.available
    assert component.quality < 1.0, "a missing composition term must cost quality"
    assert any("tag sample unavailable" in n for n in component.notes), (
        f"expected the density-only note, got {component.notes}"
    )


def test_component_failure_is_isolated_not_fatal() -> None:
    """Each component guards its own computation, so bad feed data degrades that
    axis instead of aborting the index. (The guard lives inside each _score_*
    function — assess_region deliberately has no outer catch.)"""
    class Exploding:
        """Stands in for a feed object whose data access blows up."""
        def __getattr__(self, name: str):  # noqa: ANN204
            raise ValueError(f"synthetic failure reading {name}")

    pressure = analyzer._score_pressure(Exploding())
    assert not pressure.available, "a failing component must report unavailable"
    assert pressure.score is None, "a failing component must not emit a score"
    assert "ValueError" in (pressure.unavailable_reason or ""), (
        f"the reason should name the failure, got {pressure.unavailable_reason!r}"
    )

    taxonomic = analyzer._score_taxonomic(Exploding())
    assert not taxonomic.available and taxonomic.score is None


ALL = [
    test_logistic_is_bounded_and_monotonic,
    test_band_thresholds_are_exact_at_boundaries,
    test_all_components_available_uses_nominal_weights,
    test_dropped_component_renormalises_surviving_weights,
    test_blend_is_the_weighted_mean_of_components,
    test_no_components_yields_none_not_zero,
    test_confidence_drops_when_a_component_is_missing,
    test_thermal_quality_tracks_years_requested_not_a_hardcoded_decade,
    test_partial_baseline_coverage_reduces_quality,
    test_only_lower_bound_of_trend_is_scored,
    test_empty_bounding_box_is_a_reading_not_missing_data,
    test_untagged_features_still_report_the_sample_as_unavailable,
    test_component_failure_is_isolated_not_fatal,
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
