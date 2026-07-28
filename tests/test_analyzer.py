"""Offline tests for the scoring core.

The Ecological Stress Score is the product's headline number and its blend
arithmetic — weight renormalisation when a component drops, band thresholds,
the logistic mapping — was previously only exercised by the live end-to-end
test, which cannot run without network. These build synthetic snapshots and
assert the arithmetic directly. No network.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyzer  # noqa: E402
from analyzer import (  # noqa: E402
    NOMINAL_WEIGHTS, _band_for, _logistic, assess_region,
)
from api_clients import (  # noqa: E402
    BuoySnapshot, ClimatologySnapshot, InfrastructureSnapshot, ObisSnapshot,
    Region, RegionSnapshot, SeaStateSnapshot, ThermalStressSnapshot,
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


def _thermal_stress(*, dhw: float | None = 1.0, peaks: dict[int, float] | None = None,
                    now_year: int = 2026) -> ThermalStressSnapshot:
    """A CRW snapshot. ``peaks`` is the annual-peak history; omit it to model a
    location whose history could not be fetched."""
    snapshot = ThermalStressSnapshot(
        dhw_c_weeks=dhw, hotspot_c=0.5, latest_sst_c=16.5,
        latest_day=date(2026, 7, 25), lag_days=2,
    )
    if peaks:
        snapshot.annual_peak_dhw = dict(sorted(peaks.items()))
        snapshot.history_observations = len(peaks) * 36
        snapshot.as_of_year = now_year
        snapshot.worst_year = max(peaks, key=lambda y: peaks[y])
        snapshot.worst_dhw = peaks[snapshot.worst_year]
        snapshot.years_since_worst = now_year - snapshot.worst_year
    return snapshot


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
        thermal_stress=_thermal_stress(peaks={2024: 1.0, 2025: 1.2, 2026: 1.0}),
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
    assert set(result.effective_weights) == {
        "thermal", "thermal_stress", "taxonomic", "pressure"
    }
    for key, nominal in NOMINAL_WEIGHTS.items():
        actual = result.effective_weights[key]
        assert abs(actual - nominal) < 1e-6, (
            f"{key} weight {actual} should equal nominal {nominal} when all "
            "components are available"
        )
    assert abs(sum(result.effective_weights.values()) - 1.0) < 1e-6


def test_microbial_records_do_not_drive_the_assemblage_score() -> None:
    """OBIS carries whole metagenomics surveys. At Massachusetts Bay measured
    live, Proteobacteria alone is 48.5% of the checklist and prokaryotes are
    ~72%. None of them appear in PHYLUM_SENSITIVITY, so before this filter they
    all landed on the "unknown" default and then drove both the sensitivity
    mean *and* the evenness term — a microbial survey scored by a table written
    for calcifiers.
    """
    from analyzer import _score_taxonomic

    # What the region actually looks like once the bacteria are set aside.
    animals = {"Chordata": 96, "Arthropoda": 60, "Mollusca": 40, "Annelida": 30}
    clean = ObisSnapshot(
        records=900_000, species=1791, taxa=1791, datasets=120,
        species_level_records=800_000, year_min=1871, year_max=2026,
        phylum_records=animals, checklist_sampled=200,
        non_target_records=0,
    )
    contaminated = ObisSnapshot(
        records=900_000, species=1791, taxa=1791, datasets=120,
        species_level_records=800_000, year_min=1871, year_max=2026,
        phylum_records=animals, checklist_sampled=200,
        non_target_records=580,  # ~72% of the sample
        non_target_kingdoms={"Bacteria": 520, "Archaea": 60},
    )

    a, b = _score_taxonomic(clean), _score_taxonomic(contaminated)
    assert a.score == b.score, (
        "excluded records must not reach the score at all — the mix is computed "
        "over the survivors either way"
    )
    assert b.quality < a.quality, (
        "a sample that was mostly microbial is a thinner basis for an "
        "assemblage claim and must cost confidence"
    )
    assert any("excluded" in n and "Bacteria" in n for n in b.notes), (
        "the panel must say what was dropped and why"
    )
    assert not any("excluded" in n for n in a.notes), (
        "a clean sample should not carry the caveat"
    )


def test_dhw_is_anchored_on_noaa_calibrated_thresholds() -> None:
    """This is the one component whose number means something outside this
    dashboard, so the mapping must land NOAA's published thresholds exactly
    where the dashboard's own bands claim they are: 4 °C-weeks ("significant
    bleaching likely") at the bottom of CRITICAL, 8 ("severe with mortality")
    at the top of the scale."""
    from analyzer import _score_thermal_stress

    reef_lat = 24.8  # Florida Keys
    at_zero = _score_thermal_stress(_thermal_stress(dhw=0.0), reef_lat)
    at_threshold = _score_thermal_stress(_thermal_stress(dhw=4.0), reef_lat)
    at_severe = _score_thermal_stress(_thermal_stress(dhw=8.0), reef_lat)

    assert at_zero.score == 0.0, "no accumulation must score zero, not a floor"
    assert abs(at_threshold.score - 70.0) < 1e-6, (
        "DHW 4 is NOAA's bleaching threshold and must land at 70, the bottom "
        "of the CRITICAL band"
    )
    assert abs(at_severe.score - 100.0) < 1e-6, "DHW 8 must saturate the scale"
    # Beyond severe it clamps rather than running off the top.
    assert _score_thermal_stress(_thermal_stress(dhw=25.0), reef_lat).score == 100.0
    # Monotonic in between.
    scores = [
        _score_thermal_stress(_thermal_stress(dhw=d), reef_lat).score
        for d in (0.0, 1.0, 2.0, 4.0, 6.0, 8.0)
    ]
    assert scores == sorted(scores), "DHW score must rise with accumulation"


def test_bleaching_wording_is_gated_on_reef_latitude() -> None:
    """The °C-weeks are physical at any latitude; the 4/8 bands are calibrated
    against coral mortality. Announcing "bleaching likely" for a kelp forest
    would be the same species of confident-but-wrong the trend guard exists to
    prevent."""
    from analyzer import _score_thermal_stress

    keys = _score_thermal_stress(_thermal_stress(dhw=5.0), 24.8)
    monterey = _score_thermal_stress(_thermal_stress(dhw=5.0), 36.8)

    assert any("bleaching likely" in n for n in keys.notes), (
        "a reef at DHW 5 must carry NOAA's interpretation"
    )
    assert not any("bleaching likely" in n or "bleaching with mortality" in n
                   for n in monterey.notes), (
        "a temperate location must not be told it is bleaching"
    )
    assert any("outside reef latitudes" in n and "coral" in n
               for n in monterey.notes), (
        "and must say the scale it is being read on is coral-derived"
    )
    # Same accumulation scores the same either way — only the wording differs.
    assert keys.score == monterey.score


def test_thermal_stress_degrades_without_any_dhw_data() -> None:
    """No current reading and no history means nothing to score. The component
    must report itself unavailable so its weight renormalises away, rather than
    scoring 0.0 — which on this scale reads as "pristine"."""
    from analyzer import _score_thermal_stress
    from api_clients import ThermalStressSnapshot

    for label, snapshot in [
        ("feed missing", None),
        ("empty payload", ThermalStressSnapshot()),
    ]:
        component = _score_thermal_stress(snapshot, 24.8)
        assert not component.available, f"{label} must not be scored"
        assert component.score is None, f"{label} must not invent a score"
        assert component.unavailable_reason, f"{label} must say why"


def test_past_bleaching_still_counts_in_the_cool_season() -> None:
    """The whole reason the history exists.

    DHW's window is 12 weeks, so a reef that bleached last summer reads a
    genuine 0.0 in its winter — which is exactly how the Great Barrier Reef
    scored LOW while carrying 7.3 °C-weeks from the previous year. The
    component must score the remembered event, not the empty window.
    """
    from analyzer import _score_thermal_stress

    winter_after_bleaching = _thermal_stress(
        dhw=0.0, peaks={2024: 7.3, 2025: 6.5, 2026: 0.0}, now_year=2026
    )
    component = _score_thermal_stress(winter_after_bleaching, -18.3)

    assert component.available
    assert component.score > 70.0, (
        f"a reef with 6.5 °C-weeks last year scored {component.score}; the "
        "12-week window being empty today must not read as healthy"
    )
    assert any("history" in n for n in component.notes), (
        "the panel must say the score came from history, not the current window"
    )

    # And with no such history, the same empty window is genuinely calm.
    calm = _score_thermal_stress(
        _thermal_stress(dhw=0.0, peaks={2024: 0.1, 2025: 0.0, 2026: 0.0}), -18.3
    )
    assert calm.score < 5.0, "a location with no thermal history must score low"


def test_older_events_count_less_than_recent_ones() -> None:
    """The decay is a modelling choice, not a NOAA standard, but it must at
    least be monotonic: a severe event last year cannot count for less than the
    same event a decade ago."""
    from analyzer import _recency_weight, _score_thermal_stress

    weights = [_recency_weight(y) for y in range(0, 12)]
    assert weights == sorted(weights, reverse=True), (
        f"recency weight must never increase with age: {weights}"
    )
    assert weights[0] == 1.0, "a current event counts in full"
    assert weights[11] == 0.0, "beyond the recovery window it stops counting"

    recent = _score_thermal_stress(
        _thermal_stress(dhw=0.0, peaks={2025: 8.0}, now_year=2026), -18.3
    )
    ancient = _score_thermal_stress(
        _thermal_stress(dhw=0.0, peaks={2015: 8.0}, now_year=2026), -18.3
    )
    assert recent.score > ancient.score, (
        "an event last year must outweigh the same event eleven years ago"
    )


def test_an_old_severe_event_cannot_mask_a_recent_one() -> None:
    """Regression: scoring decayed only the single worst year, so the Great
    Barrier Reef scored 12.5 — its 2017 peak (8.6 °C-weeks) is nine years gone
    and worth 12% of itself, which buried 2024's still-current 7.3. Every year
    must be aged on its own and the strongest survivor wins.
    """
    from analyzer import _score_thermal_stress

    gbr = _thermal_stress(
        dhw=0.0,
        peaks={2016: 5.5, 2017: 8.6, 2018: 0.3, 2019: 0.0, 2020: 6.5,
               2021: 0.1, 2022: 6.2, 2023: 1.5, 2024: 7.3, 2025: 6.5,
               2026: 2.8},
        now_year=2026,
    )
    component = _score_thermal_stress(gbr, -18.3)

    assert component.score > 80.0, (
        f"the GBR scored {component.score}; 7.3 °C-weeks two years ago must "
        "not be masked by an older, larger event that has since decayed"
    )
    assert component.detail["scoring_year"] in (2024, 2025), (
        f"scored on {component.detail['scoring_year']}, expected a recent year"
    )
    # The raw worst is still reported, just not what drives the score.
    assert component.detail["worst_year"] == 2017


def test_temperate_current_reading_uses_hobday_not_coral_thresholds() -> None:
    """NOAA's 4 and 8 °C-weeks are coral mortality numbers and mean nothing in
    a kelp forest. Hobday's marine-heatwave categories are percentile-based, so
    they carry the same meaning at any latitude and take over the current term
    outside reef latitudes."""
    from analyzer import _mhw_category, _mhw_multiple, _score_thermal_stress

    clim = _climatology(mean=14.0)          # baseline_p90 == mean + 1.0
    # 3x the (p90 - mean) gap is Hobday category III, Severe.
    severe_sst = 14.0 + 3.0

    multiple = _mhw_multiple(severe_sst, clim)
    assert multiple is not None and abs(multiple - 3.0) < 1e-9
    assert _mhw_category(multiple) == (3, "Severe")

    temperate = _score_thermal_stress(
        _thermal_stress(dhw=0.5, peaks={2026: 0.5}), 36.8,
        climatology=clim, current_sst_c=severe_sst,
    )
    assert temperate.score >= 70.0, (
        f"a Severe marine heatwave scored {temperate.score}; 0.5 °C-weeks on "
        "the coral scale would have read as almost nothing"
    )
    assert temperate.detail["current_term_scale"] == "Hobday MHW"
    assert any("five consecutive days" in n for n in temperate.notes), (
        "the duration criterion cannot be checked and must be disclosed"
    )

    # A reef at the same latitude-independent intensity keeps the coral scale,
    # because there the calibrated thresholds are the better instrument.
    reef = _score_thermal_stress(
        _thermal_stress(dhw=0.5, peaks={2026: 0.5}), 24.8,
        climatology=clim, current_sst_c=severe_sst,
    )
    assert reef.detail["current_term_scale"] == "coral DHW"


def test_mhw_scoring_degrades_without_a_baseline_percentile() -> None:
    """No percentile means no Hobday category. The component must fall back to
    the °C-week scale and say so, not silently score zero."""
    from analyzer import _mhw_multiple, _score_thermal_stress

    assert _mhw_multiple(15.0, None) is None
    assert _mhw_multiple(None, _climatology()) is None

    component = _score_thermal_stress(
        _thermal_stress(dhw=6.0, peaks={2026: 6.0}), 36.8,
        climatology=None, current_sst_c=None,
    )
    assert component.available and component.score > 0
    assert any("falls back to the coral-derived" in n for n in component.notes)


def test_current_reading_wins_when_it_is_worse_than_history() -> None:
    """A reef cooking right now must not be discounted because its past was
    calm — the component takes the worse of the two readings."""
    from analyzer import _score_thermal_stress

    component = _score_thermal_stress(
        _thermal_stress(dhw=10.0, peaks={2020: 0.5, 2026: 10.0}, now_year=2026), 24.8
    )
    assert component.score == 100.0, "10 °C-weeks now is past NOAA severe"
    assert any("severe" in n.lower() for n in component.notes)


def test_missing_history_still_scores_the_current_window() -> None:
    """History is best-effort: if that request fails the current reading is
    still worth having, flagged for what it cannot see."""
    from analyzer import _score_thermal_stress

    component = _score_thermal_stress(_thermal_stress(dhw=6.0, peaks=None), 24.8)
    assert component.available and component.score > 0
    assert component.quality < 1.0, "a partial answer must reduce confidence"
    assert any("no peak history" in n for n in component.notes)


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
    test_microbial_records_do_not_drive_the_assemblage_score,
    test_dhw_is_anchored_on_noaa_calibrated_thresholds,
    test_bleaching_wording_is_gated_on_reef_latitude,
    test_thermal_stress_degrades_without_any_dhw_data,
    test_past_bleaching_still_counts_in_the_cool_season,
    test_older_events_count_less_than_recent_ones,
    test_an_old_severe_event_cannot_mask_a_recent_one,
    test_temperate_current_reading_uses_hobday_not_coral_thresholds,
    test_mhw_scoring_degrades_without_a_baseline_percentile,
    test_current_reading_wins_when_it_is_worse_than_history,
    test_missing_history_still_scores_the_current_window,
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
