"""Ecological Stress Score computation for the Marine Ecosystem Health Index.

The headline number is a **stress** score on 0-100 where *higher means more
stressed*: 0 is an undisturbed reference state, 100 is severe compound pressure.
The dashboard colours it accordingly via ``ui.stress_accent`` (amber at or above
70, canopy green below 30 — the palette has no alert-red hue).

The score is a weighted blend of three independently computed components:

===================  ======  ==========================================================
Component            Weight  Derived from
===================  ======  ==========================================================
Thermal anomaly       0.45   Present SST vs a ten-year NOAA OISST day-of-year baseline,
                             expressed in standard deviations, plus the decadal trend.
Taxonomic exposure    0.30   OBIS phylum composition weighted by thermal/acidification
                             sensitivity, plus assemblage evenness.
Anthropogenic         0.25   OpenSeaMap seamark density per 1 000 km² and the share of
pressure                     features that mark vessel routing or berthing.
===================  ======  ==========================================================

Every component is computed defensively. A component that cannot be derived
(offline buoy, Overpass mirror outage, OBIS returning no classified records) is
marked unavailable, its weight is redistributed across the survivors, and the
reason is surfaced in ``degradations``. The score is therefore always either a
real number backed by real observations or ``None`` — never a filled-in guess.

Weights are renormalised over available components only, so a partial fetch
yields a comparable 0-100 figure at reduced ``confidence`` rather than an
artificially deflated one.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Final

from pydantic import BaseModel, Field

from api_clients import (
    DHW_BLEACHING_LIKELY,
    DHW_SEVERE,
    BuoySnapshot,
    ClimatologySnapshot,
    InfrastructureSnapshot,
    ObisSnapshot,
    RegionSnapshot,
    SeaStateSnapshot,
    ThermalStressSnapshot,
    is_reef_latitude,
)

__all__ = [
    "ComponentScore",
    "StressAssessment",
    "assess_region",
    "PHYLUM_SENSITIVITY",
]

# --------------------------------------------------------------------------- #
# Tunable constants
# --------------------------------------------------------------------------- #
#: The thermal budget is 0.45 and always has been; what changed is that it is
#: now *split* between the instantaneous reading and the accumulated one,
#: rather than resting entirely on "how hot is it right now". Taxonomic and
#: pressure are untouched, so a score moves only because a genuinely new signal
#: entered the thermal half — not because every weight was re-editorialised.
NOMINAL_WEIGHTS: Final[dict[str, float]] = {
    "thermal": 0.25,
    "thermal_stress": 0.20,
    "taxonomic": 0.30,
    "pressure": 0.25,
}

#: Relative susceptibility of each phylum to warming and acidification, on 0-1.
#: Calcifiers and sessile taxa score highest (reef-building Cnidaria bleach and
#: dissolve; Mollusca and Echinodermata face aragonite undersaturation), mobile
#: generalists lowest. These are ordinal weightings used to characterise how
#: *exposed* an assemblage is, not a measurement of observed decline.
PHYLUM_SENSITIVITY: Final[dict[str, float]] = {
    "Cnidaria": 1.00,
    "Echinodermata": 0.85,
    "Bryozoa": 0.78,
    "Mollusca": 0.75,
    "Porifera": 0.70,
    "Rhodophyta": 0.66,
    "Ochrophyta": 0.64,
    "Brachiopoda": 0.62,
    "Foraminifera": 0.60,
    "Arthropoda": 0.55,
    "Chlorophyta": 0.52,
    "Annelida": 0.45,
    "Chordata": 0.42,
    "Nematoda": 0.38,
    "Ciliophora": 0.35,
    "Tracheophyta": 0.55,
}
DEFAULT_SENSITIVITY: Final[float] = 0.50

#: Range over which weighted assemblage sensitivity is stretched to 0-100.
SENSITIVITY_FLOOR: Final[float] = 0.35
SENSITIVITY_CEILING: Final[float] = 0.95

#: Decadal SST trend (°C/decade) treated as the top of the trend scale.
TREND_SATURATION_C: Final[float] = 0.60

#: Seamark density (features per 1 000 km²) at which the density term reaches
#: ~63 % of full scale, per the exponential saturation below.
DENSITY_SCALE: Final[float] = 80.0

#: Share of sampled seamarks marking routing/berthing at which that term saturates.
TRAFFIC_SHARE_SATURATION: Final[float] = 0.40

BUOY_FRESHNESS_HOURS: Final[float] = 6.0


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _logistic(x: float, midpoint: float, steepness: float) -> float:
    """Logistic mapped to 0-100, guarded against overflow."""
    exponent = -steepness * (x - midpoint)
    if exponent > 60:
        return 0.0
    if exponent < -60:
        return 100.0
    return 100.0 / (1.0 + math.exp(exponent))


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
class ComponentScore(BaseModel):
    """One scored axis of the index."""

    key: str
    label: str
    score: float | None = None
    weight: float = 0.0
    available: bool = False
    quality: float = 0.0
    detail: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    unavailable_reason: str | None = None


class StressAssessment(BaseModel):
    """Final blended assessment for a region."""

    region_code: str
    region_name: str
    score: float | None = None
    band: str = "NO DATA"
    confidence: float = 0.0
    components: list[ComponentScore] = Field(default_factory=list)
    effective_weights: dict[str, float] = Field(default_factory=dict)
    degradations: list[str] = Field(default_factory=list)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Thermal context surfaced on the front panel.
    current_sst_c: float | None = None
    sst_source: str | None = None
    sst_cross_check_delta_c: float | None = None
    baseline_mean_c: float | None = None
    anomaly_c: float | None = None
    anomaly_sigma: float | None = None
    trend_c_per_decade: float | None = None
    trend_stderr_c_per_decade: float | None = None
    trend_r2: float | None = None
    trend_scored_c_per_decade: float | None = None
    marine_heatwave: bool = False

    def component(self, key: str) -> ComponentScore | None:
        return next((c for c in self.components if c.key == key), None)


def _band_for(score: float) -> str:
    if score >= 70:
        return "CRITICAL"
    if score >= 55:
        return "ELEVATED"
    if score >= 40:
        return "MODERATE"
    if score >= 30:
        return "GUARDED"
    return "LOW"


# --------------------------------------------------------------------------- #
# Component 1 — thermal anomaly
# --------------------------------------------------------------------------- #
def _select_current_sst(
    buoy: BuoySnapshot | None, sea_state: SeaStateSnapshot | None
) -> tuple[float | None, str | None, float | None, list[str]]:
    """Choose the authoritative present-day SST.

    In-situ buoy water temperature wins when fresh, because it is a direct
    measurement; the Open-Meteo model field is the fallback and, when both are
    present, a cross-check whose divergence is reported.
    """
    notes: list[str] = []
    buoy_temp = buoy.water_temp_c if buoy and buoy.is_usable else None
    model_temp = sea_state.current_sst_c if sea_state else None

    delta: float | None = None
    if buoy_temp is not None and model_temp is not None:
        delta = buoy_temp - model_temp
        notes.append(
            f"in-situ vs model divergence {delta:+.2f} °C"
        )

    if buoy_temp is not None and buoy is not None:
        label = f"NDBC {buoy.station} in-situ"
        if buoy.age_hours is not None:
            label += f" ({buoy.age_hours:.1f}h old)"
        return buoy_temp, label, delta, notes

    if buoy is not None and buoy.status != "live":
        notes.append(f"buoy unavailable ({buoy.status}); using model SST")
    if model_temp is not None:
        return model_temp, "Open-Meteo marine model", delta, notes

    return None, None, delta, notes


def _score_thermal(
    climatology: ClimatologySnapshot | None,
    buoy: BuoySnapshot | None,
    sea_state: SeaStateSnapshot | None,
) -> tuple[ComponentScore, dict[str, Any]]:
    """Score present SST against the ten-year day-of-year baseline."""
    component = ComponentScore(
        key="thermal", label="THERMAL ANOMALY", weight=NOMINAL_WEIGHTS["thermal"]
    )
    context: dict[str, Any] = {}

    try:
        current, source, delta, notes = _select_current_sst(buoy, sea_state)
        component.notes.extend(notes)
        context["current_sst_c"] = current
        context["sst_source"] = source
        context["sst_cross_check_delta_c"] = delta

        if current is None:
            component.unavailable_reason = "no present-day SST from buoy or model"
            return component, context
        if climatology is None or climatology.baseline_mean is None:
            component.unavailable_reason = "OISST baseline unavailable"
            return component, context

        baseline = climatology.baseline_mean
        anomaly = current - baseline
        sigma = climatology.baseline_std or 0.0
        z = anomaly / sigma if sigma > 1e-9 else 0.0

        # Warm-side stress dominates; cold anomalies register more weakly.
        warm = _logistic(z, midpoint=1.0, steepness=1.2)
        cold = _logistic(-z, midpoint=1.8, steepness=1.0)
        anomaly_score = max(warm, cold)

        # Score the lower confidence bound, not the point estimate: a ten-point
        # regression on annual means carries enough error that the raw slope
        # would otherwise saturate this term on noise alone.
        trend = climatology.trend_c_per_decade
        trend_used = climatology.trend_lower_c_per_decade
        trend_score = (
            _clamp(trend_used / TREND_SATURATION_C * 100.0)
            if trend_used is not None
            else 0.0
        )
        if trend is None:
            component.notes.append("decadal trend undetermined; anomaly-only scoring")
        elif trend_used is not None and trend_used <= 0.0:
            component.notes.append(
                f"decadal trend {trend:+.2f} °C not separable from interannual "
                f"variability (±{(climatology.trend_stderr_c_per_decade or 0):.2f}); "
                "trend term scored zero"
            )

        score = 0.75 * anomaly_score + 0.25 * trend_score
        heatwave = (
            climatology.baseline_p90 is not None and current > climatology.baseline_p90
        )
        if heatwave:
            component.notes.append("current SST exceeds 10-year 90th percentile")

        component.score = _clamp(score)
        component.available = True
        component.quality = _clamp(
            (climatology.years_covered / max(1, climatology.years_requested))
            * (1.0 if buoy and buoy.is_usable else 0.82),
            0.0,
            1.0,
        )
        component.detail = {
            "current_sst_c": round(current, 2),
            "baseline_mean_c": round(baseline, 2),
            "baseline_std_c": round(sigma, 3),
            "anomaly_c": round(anomaly, 2),
            "anomaly_sigma": round(z, 2),
            "trend_c_per_decade": round(trend, 3) if trend is not None else None,
            "trend_stderr_c_per_decade": (
                round(climatology.trend_stderr_c_per_decade, 3)
                if climatology.trend_stderr_c_per_decade is not None
                else None
            ),
            "trend_r2": (
                round(climatology.trend_r2, 3) if climatology.trend_r2 is not None else None
            ),
            "trend_scored_c_per_decade": (
                round(trend_used, 3) if trend_used is not None else None
            ),
            "anomaly_term": round(anomaly_score, 1),
            "trend_term": round(trend_score, 1),
            "baseline_years": climatology.years_covered,
            "baseline_observations": climatology.observations,
            "marine_heatwave": heatwave,
        }
        context.update(
            baseline_mean_c=baseline,
            anomaly_c=anomaly,
            anomaly_sigma=z,
            trend_c_per_decade=trend,
            trend_stderr_c_per_decade=climatology.trend_stderr_c_per_decade,
            trend_r2=climatology.trend_r2,
            trend_scored_c_per_decade=trend_used,
            marine_heatwave=heatwave,
        )
        if climatology.failed_years:
            component.notes.append(
                f"baseline years missing: {', '.join(map(str, climatology.failed_years))}"
            )
    except Exception as exc:  # defensive: never let one axis abort the index
        component.available = False
        component.score = None
        component.unavailable_reason = f"{type(exc).__name__}: {exc}"

    return component, context


# --------------------------------------------------------------------------- #
# Component 2 — accumulated thermal stress (Degree Heating Weeks)
# --------------------------------------------------------------------------- #
def _score_thermal_stress(
    stress: ThermalStressSnapshot | None, latitude: float
) -> ComponentScore:
    """Score accumulated heat stress, NOAA Coral Reef Watch Degree Heating Weeks.

    This is the only component with an *outcome-calibrated* scale: NOAA fixes
    4 °C-weeks as "significant bleaching likely" and 8 as "severe bleaching
    with mortality", both validated against observed reef mortality. The score
    is anchored on exactly those two points — 4 maps to 70, the bottom of this
    dashboard's CRITICAL band, and 8 maps to 100 — so the number the panel
    shows and the number the published scale means are the same number.

    The *bands* are coral-specific; the underlying quantity is not. Accumulated
    warm anomaly is real stress in a kelp forest too (the 2014-16 north-east
    Pacific event was measured this way), so it is scored everywhere and only
    the bleaching wording is gated on latitude.
    """
    component = ComponentScore(
        key="thermal_stress",
        label="ACCUMULATED HEAT STRESS",
        weight=NOMINAL_WEIGHTS["thermal_stress"],
    )
    try:
        if stress is None:
            component.unavailable_reason = "OISST thermal-stress fetch failed"
            return component
        if stress.mmm_c is None:
            component.unavailable_reason = (
                "no fixed-baseline climatology for this location"
            )
            return component
        if stress.dhw_c_weeks is None or stress.observations == 0:
            component.unavailable_reason = "no recent SST in the accumulation window"
            return component

        dhw = stress.dhw_c_weeks
        if dhw <= DHW_BLEACHING_LIKELY:
            score = dhw / DHW_BLEACHING_LIKELY * 70.0
        else:
            span = DHW_SEVERE - DHW_BLEACHING_LIKELY
            score = 70.0 + (dhw - DHW_BLEACHING_LIKELY) / span * 30.0

        component.score = _clamp(score)
        component.available = True
        # Coverage of the window is the honest quality signal: a partly-filled
        # window under-accumulates and would read as calm rather than unknown.
        component.quality = _clamp(
            stress.observations / max(1, stress.window_days), 0.0, 1.0
        )

        if is_reef_latitude(latitude):
            if dhw >= DHW_SEVERE:
                component.notes.append(
                    f"{dhw:.1f} °C-weeks — NOAA severe bleaching with mortality "
                    f"(>= {DHW_SEVERE:.0f})"
                )
            elif dhw >= DHW_BLEACHING_LIKELY:
                component.notes.append(
                    f"{dhw:.1f} °C-weeks — NOAA significant bleaching likely "
                    f"(>= {DHW_BLEACHING_LIKELY:.0f})"
                )
        else:
            # The °C-weeks are a real physical quantity at any latitude; the
            # *scale* they are scored on is derived from coral outcomes, which
            # is worth admitting rather than glossing.
            component.notes.append(
                "outside reef latitudes: the °C-weeks are real accumulated warm "
                "anomaly, but the scale is coral-derived and NOAA's bleaching "
                "bands are not applied here"
            )

        # The window is the limitation, so it travels with the number.
        component.notes.append(
            f"{stress.window_days // 7}-week rolling window: recent accumulation, "
            "not a record of past damage"
        )
        if stress.lag_days is not None and stress.lag_days > 0:
            component.notes.append(
                f"newest OISST observation is {stress.lag_days} d old "
                "(product publishes ~2 weeks behind)"
            )

        component.detail = {
            "dhw_c_weeks": round(dhw, 2),
            "mmm_c": round(stress.mmm_c, 2),
            "mmm_month": stress.mmm_month,
            "mmm_source": stress.mmm_source,
            "latest_sst_c": (
                round(stress.latest_sst_c, 2)
                if stress.latest_sst_c is not None else None
            ),
            "hotspot_c": (
                round(stress.hotspot_c, 2) if stress.hotspot_c is not None else None
            ),
            "observations": stress.observations,
            "window_days": stress.window_days,
            "reef_latitude": is_reef_latitude(latitude),
        }
    except Exception as exc:  # noqa: BLE001 - assess_region has no outer catch
        component.available = False
        component.unavailable_reason = f"scoring failed ({type(exc).__name__})"
    return component


# --------------------------------------------------------------------------- #
# Component 3 — taxonomic exposure
# --------------------------------------------------------------------------- #
def _score_taxonomic(obis: ObisSnapshot | None) -> ComponentScore:
    """Weight the observed assemblage by phylum-level sensitivity and evenness."""
    component = ComponentScore(
        key="taxonomic", label="TAXONOMIC EXPOSURE", weight=NOMINAL_WEIGHTS["taxonomic"]
    )
    try:
        if obis is None:
            component.unavailable_reason = "OBIS fetch failed"
            return component

        total = obis.total_classified_records
        if total <= 0 or not obis.phylum_records:
            component.unavailable_reason = "no classified occurrence records returned"
            return component

        shares = {p: c / total for p, c in obis.phylum_records.items()}

        weighted_sensitivity = sum(
            share * PHYLUM_SENSITIVITY.get(phylum, DEFAULT_SENSITIVITY)
            for phylum, share in shares.items()
        )
        sensitivity_score = _clamp(
            (weighted_sensitivity - SENSITIVITY_FLOOR)
            / (SENSITIVITY_CEILING - SENSITIVITY_FLOOR)
            * 100.0
        )

        # Shannon evenness: a assemblage dominated by one phylum is more fragile.
        entropy = -sum(s * math.log(s) for s in shares.values() if s > 0)
        richness = len(shares)
        evenness = entropy / math.log(richness) if richness > 1 else 0.0
        fragility_score = _clamp((1.0 - evenness) * 100.0)

        score = 0.60 * sensitivity_score + 0.40 * fragility_score

        # Quality: enough records, and recent enough to describe the present.
        record_adequacy = _clamp(math.log10(max(obis.records, 1)) / 5.0, 0.0, 1.0)
        recency = 1.0
        if obis.year_max is not None:
            lag = datetime.now(timezone.utc).year - obis.year_max
            if lag > 2:
                recency = _clamp(1.0 - (lag - 2) / 20.0, 0.2, 1.0)
                component.notes.append(f"latest OBIS record is {lag} years old")

        dominant = max(shares.items(), key=lambda kv: kv[1])
        component.score = _clamp(score)
        component.available = True
        component.quality = _clamp(record_adequacy * recency, 0.0, 1.0)
        component.detail = {
            "weighted_sensitivity": round(weighted_sensitivity, 3),
            "sensitivity_term": round(sensitivity_score, 1),
            "evenness": round(evenness, 3),
            "fragility_term": round(fragility_score, 1),
            "phyla_observed": richness,
            "dominant_phylum": dominant[0],
            "dominant_share": round(dominant[1], 3),
            "records": obis.records,
            "species": obis.species,
            "datasets": obis.datasets,
            "checklist_sampled": obis.checklist_sampled,
            "year_range": [obis.year_min, obis.year_max],
        }
        component.notes.append(
            f"assemblage weighted from top {obis.checklist_sampled} taxa "
            f"({total:,} classified records)"
        )
    except Exception as exc:
        component.available = False
        component.score = None
        component.unavailable_reason = f"{type(exc).__name__}: {exc}"

    return component


# --------------------------------------------------------------------------- #
# Component 4 — anthropogenic pressure
# --------------------------------------------------------------------------- #
def _score_pressure(infra: InfrastructureSnapshot | None) -> ComponentScore:
    """Score vessel-activity pressure from seamark density and composition."""
    component = ComponentScore(
        key="pressure", label="ANTHROPOGENIC PRESSURE", weight=NOMINAL_WEIGHTS["pressure"]
    )
    try:
        if infra is None:
            component.unavailable_reason = "Overpass fetch failed"
            return component
        if infra.area_km2 <= 0:
            component.unavailable_reason = "region area undefined"
            return component

        density = infra.density_per_1000km2
        density_score = _clamp(100.0 * (1.0 - math.exp(-density / DENSITY_SCALE)))

        if infra.sampled_elements > 0:
            traffic_share = infra.traffic_share
            traffic_score = _clamp(traffic_share / TRAFFIC_SHARE_SATURATION * 100.0)
            score = 0.65 * density_score + 0.35 * traffic_score
            quality = 1.0
        elif infra.total_count > 0:
            # Features exist but their tags could not be sampled: density only,
            # at reduced quality because the composition term is missing.
            traffic_share = 0.0
            traffic_score = 0.0
            score = density_score
            quality = 0.6
            component.notes.append("tag sample unavailable; density-only scoring")
        else:
            # Nothing charted here, measured successfully. A genuinely empty
            # bounding box is a real reading (zero pressure), not missing data:
            # claiming "tag sample unavailable" would be false, and docking
            # quality would understate a result we are fully confident in.
            traffic_share = 0.0
            traffic_score = 0.0
            score = density_score
            quality = 1.0

        component.score = _clamp(score)
        component.available = True
        component.quality = quality
        component.detail = {
            "total_features": infra.total_count,
            "nodes": infra.node_count,
            "ways": infra.way_count,
            "area_km2": round(infra.area_km2, 1),
            "density_per_1000km2": round(density, 2),
            "density_term": round(density_score, 1),
            "traffic_features": infra.traffic_features,
            "traffic_share": round(traffic_share, 3),
            "traffic_term": round(traffic_score, 1),
            "sampled_elements": infra.sampled_elements,
            "distinct_types": len(infra.type_breakdown),
        }
        if infra.total_count == 0:
            component.notes.append("no charted seamarks in bounding box")
    except Exception as exc:
        component.available = False
        component.score = None
        component.unavailable_reason = f"{type(exc).__name__}: {exc}"

    return component


# --------------------------------------------------------------------------- #
# Blend
# --------------------------------------------------------------------------- #
def assess_region(snapshot: RegionSnapshot) -> StressAssessment:
    """Blend the three components into a single 0-100 Ecological Stress Score.

    Missing components are dropped and their weight redistributed
    proportionally across those that remain, so the result stays on a
    comparable scale. ``confidence`` reflects both how much nominal weight
    survived and the data quality behind each surviving component.
    """
    assessment = StressAssessment(
        region_code=snapshot.region.code, region_name=snapshot.region.name
    )

    thermal, thermal_context = _score_thermal(
        snapshot.climatology, snapshot.buoy, snapshot.sea_state
    )
    thermal_stress = _score_thermal_stress(
        snapshot.thermal_stress, snapshot.region.centroid[0]
    )
    taxonomic = _score_taxonomic(snapshot.obis)
    pressure = _score_pressure(snapshot.infrastructure)
    assessment.components = [thermal, thermal_stress, taxonomic, pressure]

    assessment.current_sst_c = thermal_context.get("current_sst_c")
    assessment.sst_source = thermal_context.get("sst_source")
    assessment.sst_cross_check_delta_c = thermal_context.get("sst_cross_check_delta_c")
    assessment.baseline_mean_c = thermal_context.get("baseline_mean_c")
    assessment.anomaly_c = thermal_context.get("anomaly_c")
    assessment.anomaly_sigma = thermal_context.get("anomaly_sigma")
    assessment.trend_c_per_decade = thermal_context.get("trend_c_per_decade")
    assessment.trend_stderr_c_per_decade = thermal_context.get("trend_stderr_c_per_decade")
    assessment.trend_r2 = thermal_context.get("trend_r2")
    assessment.trend_scored_c_per_decade = thermal_context.get("trend_scored_c_per_decade")
    assessment.marine_heatwave = bool(thermal_context.get("marine_heatwave", False))

    for name, message in snapshot.errors.items():
        assessment.degradations.append(f"{name}: {message}")
    for component in assessment.components:
        if not component.available and component.unavailable_reason:
            assessment.degradations.append(
                f"{component.key} excluded — {component.unavailable_reason}"
            )

    live = [c for c in assessment.components if c.available and c.score is not None]
    if not live:
        assessment.score = None
        assessment.band = "NO DATA"
        assessment.confidence = 0.0
        return assessment

    weight_sum = sum(c.weight for c in live)
    effective = {c.key: c.weight / weight_sum for c in live}
    assessment.effective_weights = {k: round(v, 4) for k, v in effective.items()}
    assessment.score = round(
        sum(effective[c.key] * float(c.score) for c in live), 1
    )
    assessment.band = _band_for(assessment.score)

    nominal_total = sum(NOMINAL_WEIGHTS.values())
    assessment.confidence = round(
        sum(c.weight * c.quality for c in live) / nominal_total, 3
    )
    return assessment


if __name__ == "__main__":  # pragma: no cover - manual check
    import asyncio
    import sys

    from api_clients import REGIONS, REGIONS_BY_CODE, fetch_region_snapshot

    code = sys.argv[1].upper() if len(sys.argv) > 1 else "MTBY"
    region = REGIONS_BY_CODE.get(code, REGIONS[0])

    snap = asyncio.run(fetch_region_snapshot(region))
    result = assess_region(snap)

    print(f"\n{result.region_name}  [{result.region_code}]")
    print(f"  ECOLOGICAL STRESS SCORE : {result.score}  ({result.band})")
    print(f"  confidence              : {result.confidence:.0%}")
    print(f"  SST                     : {result.current_sst_c} °C via {result.sst_source}")
    print(f"  baseline / anomaly      : {result.baseline_mean_c} °C / "
          f"{result.anomaly_c} °C ({result.anomaly_sigma} sigma)")
    print(f"  decadal trend           : {result.trend_c_per_decade} "
          f"+/- {result.trend_stderr_c_per_decade} °C/decade "
          f"(r2={result.trend_r2}); scored {result.trend_scored_c_per_decade}")
    print(f"  marine heatwave         : {result.marine_heatwave}")
    for c in result.components:
        state = f"{c.score:5.1f}" if c.score is not None else "  n/a"
        print(f"    {c.label:24s} {state}  w={c.weight:.2f} q={c.quality:.2f}")
        for note in c.notes:
            print(f"        - {note}")
        if c.unavailable_reason:
            print(f"        ! {c.unavailable_reason}")
    for degradation in result.degradations:
        print(f"  DEGRADED: {degradation}")
