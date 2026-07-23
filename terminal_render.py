"""HTML/SVG rendering of the Live Index terminal.

The handoff specifies inline SVG and flex-based bars with no external chart
library, so the whole dashboard body is generated here as one markup string and
injected in a single pass. That also keeps the fixed 1920px layout intact:
Streamlit's own layout primitives cannot express a 360px sidebar beside a
flexible main column at a fixed page width.

Every value rendered here comes from a live ``RegionSnapshot`` and its
``StressAssessment``. Where a source is unavailable the panel says so rather
than substituting a placeholder.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable, Sequence

from analyzer import StressAssessment
from api_clients import BuoySnapshot, Region, RegionSnapshot
from ui import (
    BORDER_STRONG, CANOPY, INK, LINE, MIST, PAPER, PAPER_DIM, SIGNAL,
    SOURCE_COLOURS, TerminalConfig, esc, fmt,
)

__all__ = ["render_topbar_left", "location_subtitle", "render_body", "render_footer"]
SOURCES_LINE = (
    "SOURCES: NOAA ERDDAP coastwatch.pfeg.noaa.gov (cwwcNDBCMet in-situ buoy, "
    "ncdcOisst21Agg OISST v2.1 day-of-year baseline) · Open-Meteo Marine "
    "marine-api.open-meteo.com (model SST) · OBIS api.obis.org/v3 (occurrence "
    "statistics and taxonomic checklist) · OpenSeaMap via Overpass "
    "overpass-api.de (charted seamarks). Stress score is computed from these "
    "responses only; unavailable sources are excluded and their weight "
    "redistributed across the remainder."
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _downsample(values: Sequence, n: int) -> list:
    """Evenly spaced subsample, preserving first and last."""
    values = list(values)
    if not values or n <= 0:
        return []
    if len(values) <= n:
        return values
    step = (len(values) - 1) / (n - 1)
    return [values[min(len(values) - 1, round(i * step))] for i in range(n)]


def _chip(text: str, *, large: bool = False, block: bool = False) -> str:
    classes = "tm-chip" + (" lg" if large else "") + (" bar" if block else "")
    return f'<div class="{classes}">{esc(text)}</div>'


def _row(key: str, value: str, *, colour: str = PAPER, small_key: bool = False,
         medium_value: bool = False) -> str:
    key_class = "k sm" if small_key else "k"
    value_class = "v md" if medium_value else "v"
    return (
        f'<div class="tm-row"><span class="{key_class}">{esc(key)}</span>'
        f'<span class="{value_class}" style="color:{colour}">{esc(value)}</span></div>'
    )


def _section(label: str, rows: Iterable[str], *, first: bool = False) -> str:
    divider = "" if first else " tm-divider"
    return (
        f'<div class="{divider.strip() or ""}">'
        f'<div class="tm-seclabel">{esc(label)}</div>{"".join(rows)}</div>'
    )


def _drawer(label: str, body: str, *, open_: bool = False) -> str:
    """A click-to-expand detail drawer (native <details>, no JavaScript).

    The summary is always visible; the body is revealed on click. Note that the
    open/closed state is DOM state and resets when the page re-renders on the
    auto-refresh cycle — acceptable at a multi-minute cadence.
    """
    if not body:
        return ""
    attr = " open" if open_ else ""
    return (
        f'<details class="tm-drawer"{attr}><summary>{esc(label)}</summary>'
        f'<div class="tm-drawer-body">{body}</div></details>'
    )


def _mini_table(headers: Sequence[tuple[str, bool]], rows: Sequence[Sequence]) -> str:
    """Compact table. ``headers`` is (text, is_numeric); each cell in ``rows`` is
    a value or a ``(value, css_class)`` pair."""
    head = "".join(
        f'<th class="num">{esc(h)}</th>' if num else f"<th>{esc(h)}</th>"
        for h, num in headers
    )
    body_rows = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            cls = "num" if headers[i][1] else ""
            if isinstance(cell, tuple):
                value, extra = cell
                cls = f"{cls} {extra}".strip()
            else:
                value = cell
            cells.append(f'<td class="{cls}">{esc(value)}</td>' if cls else f"<td>{esc(value)}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f'<table class="tm-mini"><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table>'
    )


def _detail_rows(pairs: Sequence[tuple[str, str]]) -> str:
    """Key/value detail block for a drawer body."""
    out = ['<div class="mx-kv" style="font-size:10.5px">']
    for key, value in pairs:
        out.append(
            f'<div><span class="k" style="min-width:210px;color:{MIST}">{esc(key)}</span>'
            f'<span style="color:{PAPER}">{esc(value)}</span></div>'
        )
    out.append("</div>")
    return "".join(out)


def _nice_ceiling(value: float, divisions: int = 4) -> float:
    """Round a scale maximum up to something that divides cleanly."""
    if value <= 0:
        return float(divisions)
    magnitude = 10 ** math.floor(math.log10(value))
    for multiplier in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        candidate = magnitude * multiplier
        if candidate >= value:
            return float(candidate)
    return float(magnitude * 10)


# --------------------------------------------------------------------------- #
# SVG charts
# --------------------------------------------------------------------------- #
def _sparkline(values: Sequence[float], width: int = 300, height: int = 30) -> str:
    points = [float(v) for v in _downsample([v for v in values if v is not None], 13)]
    if len(points) < 2:
        return f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}"></svg>'
    low, high = min(points), max(points)
    span = (high - low) or 1.0
    coords = " ".join(
        f"{i / (len(points) - 1) * width:.1f},"
        f"{height - (v - low) / span * (height - 4) - 2:.1f}"
        for i, v in enumerate(points)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'preserveAspectRatio="none">'
        f'<polyline points="{coords}" fill="none" stroke="{MIST}" stroke-width="1.5"/>'
        "</svg>"
    )


def _line_chart(
    series: list[tuple[str, str, list[float | None]]],
    baseline: float | None,
    width: int = 1000,
    height: int = 220,
) -> tuple[str, list[float]]:
    """Multi-series line chart. Returns (svg, y_tick_values)."""
    finite = [v for _, _, values in series for v in values if v is not None]
    if not finite:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}"></svg>',
            [],
        )

    low, high = min(finite), max(finite)
    if baseline is not None:
        low, high = min(low, baseline), max(high, baseline)
    pad = (high - low) * 0.12 or 0.5
    low, high = low - pad, high + pad
    span = (high - low) or 1.0

    def y_of(value: float) -> float:
        return height - (value - low) / span * height

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="none">'
    ]
    if baseline is not None:
        parts.append(
            f'<line x1="0" y1="{y_of(baseline):.1f}" x2="{width}" '
            f'y2="{y_of(baseline):.1f}" stroke="{BORDER_STRONG}" stroke-width="1" '
            f'stroke-dasharray="5,5"/>'
        )
    for _, colour, values in series:
        coords = [
            f"{i / max(1, len(values) - 1) * width:.1f},{y_of(float(v)):.1f}"
            for i, v in enumerate(values)
            if v is not None
        ]
        if len(coords) >= 2:
            parts.append(
                f'<polyline points="{" ".join(coords)}" fill="none" stroke="{colour}" '
                f'stroke-width="2"/>'
            )
    parts.append("</svg>")

    ticks = [high - (high - low) * i / 5 for i in range(6)]
    return "".join(parts), ticks


def _bar_chart(years: list[int], values: list[float], threshold: float | None) -> str:
    """Vertical bars in a 130px track with an overlaid amber threshold line."""
    if not values:
        return f'<div class="tm-mist" style="font-size:11.5px">baseline unavailable</div>'

    scale_max = _nice_ceiling(max([*values, threshold or 0]) * 1.05)
    bars = "".join(
        f'<div class="tm-bar" style="height:{max(2.0, v / scale_max * 130):.1f}px" '
        f'title="{y}: {v:.2f} degC"></div>'
        for y, v in zip(years, values)
    )
    line = ""
    if threshold is not None:
        offset = min(130.0, max(0.0, threshold / scale_max * 130))
        line = f'<div class="tm-threshold" style="bottom:{offset:.1f}px"></div>'

    ticks = "".join(
        f"<div>{scale_max * i / 4:.0f}</div>" for i in range(4, -1, -1)
    )
    labels = "".join(f"<div>{y}</div>" for y in years)
    return (
        '<div class="tm-chartrow">'
        f'<div class="tm-yaxis" style="height:130px">{ticks}</div>'
        f'<div class="tm-bars">{bars}{line}</div>'
        "</div>"
        f'<div class="tm-xaxis">{labels}</div>'
    )


def _hbars(
    items: list[tuple[str, float, str]], label_width: int, max_value: float
) -> str:
    """Horizontal bars: (label, magnitude, display_value)."""
    if not items:
        return '<div class="tm-mist" style="font-size:11.5px">no data returned</div>'  # noqa: E501
    rows = []
    for label, magnitude, display in items:
        pct = 0.0 if max_value <= 0 else min(100.0, magnitude / max_value * 100.0)
        rows.append(
            '<div class="tm-hrow">'
            f'<div class="tm-hlabel" style="width:{label_width}px">{esc(label)}</div>'
            f'<div class="tm-htrack"><div class="tm-hfill" style="width:{pct:.1f}%"></div></div>'
            f'<div class="tm-hvalue">{esc(display)}</div>'
            "</div>"
        )
    return "".join(rows)


# --------------------------------------------------------------------------- #
# Top bar / footer
# --------------------------------------------------------------------------- #
def render_topbar_left(config: TerminalConfig, subtitle: str, stamp: str) -> str:
    """Title block: fixed product name over a location-specific subtitle and the
    UTC timestamp. The title never wraps (nowrap) so a narrow column cannot
    shatter it across several lines."""
    return (
        f'<div style="padding:{config.pad_outer}px 0 16px 32px">'
        '<div style="white-space:nowrap"><span class="tm-live"></span>'
        '<span class="tm-title">MARINE ECOSYSTEM HEALTH INDEX</span></div>'
        f'<div class="tm-subtitle">{esc(subtitle)}</div>'
        f'<div class="tm-stamp" style="margin-top:3px;line-height:1.4">{esc(stamp)}</div>'
        "</div>"
    )


def location_subtitle(region: Region, coverage: str) -> str:
    """Subtitle reflecting the current location and its data coverage."""
    tail = {
        "none": " · no marine data here",
        "model_only": " · model-only SST",
    }.get(coverage, "")
    return f"Live environmental risk telemetry · {region.name}{tail}"


def render_footer() -> str:
    return f'<div class="tm-root"><div class="tm-footer">{esc(SOURCES_LINE)}</div></div>'


# --------------------------------------------------------------------------- #
# Detail-drawer content builders (all from already-fetched live data)
# --------------------------------------------------------------------------- #
def _component_detail(assessment: StressAssessment) -> str:
    """Full derivation of every score component: weights, quality, the complete
    detail dict each component computed, and its notes."""
    blocks: list[str] = []
    for component in assessment.components:
        state = (
            f"score {component.score:.1f}"
            if component.available and component.score is not None
            else "EXCLUDED"
        )
        blocks.append(
            f'<div style="color:{SIGNAL};font-size:10px;letter-spacing:0.1em;'
            f'margin:8px 0 2px 0">{esc(component.label)} — {esc(state)}</div>'
        )
        pairs: list[tuple[str, str]] = []
        if not component.available:
            pairs.append(("excluded because", component.unavailable_reason or "unavailable"))
        else:
            pairs.append(
                ("effective weight",
                 f"{assessment.effective_weights.get(component.key, 0.0):.3f}")
            )
            pairs.append(("nominal weight", f"{component.weight:.2f}"))
            pairs.append(("data quality", f"{component.quality:.0%}"))
            for key, value in (component.detail or {}).items():
                pairs.append((key.replace("_", " "), str(value)))
        blocks.append(_detail_rows(pairs))
        if component.notes:
            blocks.append(_detail_rows([("note", n) for n in component.notes]))

    tail: list[tuple[str, str]] = [
        ("effective weights",
         "  ".join(f"{k}={v:.3f}" for k, v in assessment.effective_weights.items())),
        ("confidence", f"{assessment.confidence:.0%}"),
        ("computed at", assessment.computed_at.strftime("%Y-%m-%d %H:%M:%SZ")),
    ]
    blocks.append(
        f'<div style="color:{SIGNAL};font-size:10px;margin:8px 0 2px 0">BLEND</div>'
        + _detail_rows(tail)
    )
    if assessment.degradations:
        blocks.append(
            f'<div style="color:{SIGNAL};font-size:10px;margin:8px 0 2px 0">'
            "DEGRADATIONS</div>"
            + _detail_rows([("!", d) for d in assessment.degradations])
        )
    return "".join(blocks)


def _buoy_detail(buoy: BuoySnapshot) -> str:
    """Full in-situ station provenance: candidates tried, cadence, and notes."""
    times = buoy.history_times or []
    span = ""
    if len(times) >= 2:
        span = f"{times[0]:%Y-%m-%d %H:%MZ} → {times[-1]:%Y-%m-%d %H:%MZ}"
    non_null = sum(1 for v in buoy.history_wtmp_c if v is not None)
    pairs = [
        ("candidate stations tried", ", ".join(buoy.stations_tried) or "--"),
        ("selected station", buoy.station or "none"),
        ("station position",
         f"{buoy.latitude:.3f}, {buoy.longitude:.3f}"
         if buoy.latitude is not None else "--"),
        ("readings in 48h window", str(len(times))),
        ("with water temperature", str(non_null)),
        ("observation span", span or "--"),
    ]
    body = _detail_rows(pairs)
    if buoy.notes:
        body += _detail_rows([("note", n) for n in buoy.notes])
    return body


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
def _sidebar(
    snapshot: RegionSnapshot, assessment: StressAssessment, stale: bool
) -> str:
    score = assessment.score
    sea_state = snapshot.sea_state
    spark_values = [v for v in (sea_state.sst_c if sea_state else []) if v is not None]

    status = f"{assessment.band} · {'STALE' if stale else 'LIVE'}"
    hero = (
        '<div>'
        + _chip("STRESS SCORE")
        + f'<div class="tm-hero-num">{fmt(score, ".1f")}</div>'
        + _chip(status, large=True)
        + f'<div style="margin-top:12px">{_sparkline(spark_values)}</div>'
        "</div>"
    )

    # --- score derivation ------------------------------------------------- #
    derivation: list[str] = []
    for component in assessment.components:
        weight = assessment.effective_weights.get(component.key, 0.0)
        if component.available and component.score is not None:
            derivation.append(
                _row(
                    component.label.title(),
                    f"{component.score:.1f} × {weight:.2f} = {component.score * weight:.1f}",
                )
            )
        else:
            derivation.append(
                _row(component.label.title(), "excluded", colour=SIGNAL)
            )
    derivation.append(
        _row("Ecological Stress Score", fmt(score, ".1f"), colour=SIGNAL)
    )

    # --- key metrics ------------------------------------------------------- #
    anomaly = assessment.anomaly_c
    sigma = assessment.anomaly_sigma
    infra = snapshot.infrastructure
    obis = snapshot.obis
    climatology = snapshot.climatology
    metrics = [
        _row("Sea surface temp", fmt(assessment.current_sst_c, ".2f", " °C"),
             small_key=True, medium_value=True),
        _row("Anomaly vs 10y baseline",
             fmt(anomaly, "+.2f", " °C"),
             colour=SIGNAL if (anomaly or 0) > 0.5 else PAPER,
             small_key=True, medium_value=True),
        _row("Anomaly (sigma)", fmt(sigma, "+.2f"),
             colour=SIGNAL if (sigma or 0) >= 1.29 else PAPER,
             small_key=True, medium_value=True),
        _row("Decadal trend", fmt(assessment.trend_c_per_decade, "+.2f", " °C/dec"),
             small_key=True, medium_value=True),
        _row("Trend scored", fmt(assessment.trend_scored_c_per_decade, ".2f", " °C/dec"),
             small_key=True, medium_value=True),
        _row("Marine heatwave", "ACTIVE" if assessment.marine_heatwave else "None",
             colour=SIGNAL if assessment.marine_heatwave else PAPER,
             small_key=True, medium_value=True),
        _row("Baseline mean", fmt(assessment.baseline_mean_c, ".2f", " °C"),
             small_key=True, medium_value=True),
        _row("Seamark density",
             f"{infra.density_per_1000km2:.1f} /1000km²" if infra else "--",
             small_key=True, medium_value=True),
        _row("Species observed", f"{obis.species:,}" if obis else "--",
             small_key=True, medium_value=True),
        _row("Confidence", f"{assessment.confidence:.0%}",
             colour=PAPER if assessment.confidence >= 0.75 else SIGNAL,
             small_key=True, medium_value=True),
    ]

    # --- buoy -------------------------------------------------------------- #
    buoy = snapshot.buoy
    if buoy is None:
        buoy_label = "IN-SITU BUOY"
        buoy_rows = [_row("Status", "ERDDAP fetch failed", colour=SIGNAL, small_key=True)]
    else:
        buoy_label = f"IN-SITU BUOY {buoy.station or '—'}"
        live = buoy.status == "live"
        buoy_rows = [
            _row("Status", buoy.status.upper(),
                 colour=PAPER if live else SIGNAL, small_key=True),
            _row("Water temperature", fmt(buoy.water_temp_c, ".1f", " °C"), small_key=True),
            _row("Air temperature", fmt(buoy.air_temp_c, ".1f", " °C"), small_key=True),
            _row("Wind speed", fmt(buoy.wind_speed_ms, ".1f", " m/s"), small_key=True),
            _row("Wave height", fmt(buoy.wave_height_m, ".1f", " m"), small_key=True),
            _row("Observation age", fmt(buoy.age_hours, ".1f", " h"),
                 colour=PAPER if (buoy.age_hours or 0) < 6 else SIGNAL, small_key=True),
            _row("Distance from centroid", fmt(buoy.distance_km, ".0f", " km"),
                 small_key=True),
            _row("Stations tried", ", ".join(buoy.stations_tried) or "--", small_key=True),
        ]

    score_drawer = _drawer("METHODOLOGY & COMPONENT DETAIL", _component_detail(assessment))
    buoy_drawer = _drawer("ALL STATIONS & NOTES", _buoy_detail(buoy)) if buoy else ""

    return (
        '<aside class="tm-side">'
        + hero
        + _section("SCORE DERIVATION", derivation)
        + score_drawer
        + _section("KEY METRICS", metrics)
        + _section(buoy_label, buoy_rows)
        + buoy_drawer
        + "</aside>"
    )


# --------------------------------------------------------------------------- #
# Main column
# --------------------------------------------------------------------------- #
def _sst_panel(snapshot: RegionSnapshot, assessment: StressAssessment,
               region: Region) -> str:
    sea_state = snapshot.sea_state
    buoy = snapshot.buoy

    model = _downsample([v for v in (sea_state.sst_c if sea_state else [])], 24)
    in_situ = _downsample(list(buoy.history_wtmp_c) if buoy else [], 24)

    times = _downsample(list(sea_state.times) if sea_state else [], 24)
    labels = [t.strftime("%m-%d %Hh") for t in times] if times else []

    series: list[tuple[str, str, list[float | None]]] = []
    if any(v is not None for v in in_situ):
        series.append(("in-situ", CANOPY, in_situ))
    if any(v is not None for v in model):
        series.append(("model", MIST, model))

    svg, ticks = _line_chart(series, assessment.baseline_mean_c)
    tick_html = "".join(f"<div>{t:.1f}</div>" for t in ticks)
    label_html = "".join(f"<div>{esc(l)}</div>" for l in labels)

    station = buoy.station if buoy and buoy.station else "—"
    legend = (
        '<div class="tm-legend">'
        f'<span style="color:{CANOPY}">● NDBC {esc(station)} in-situ</span>'
        f'<span style="color:{MIST}">● Open-Meteo model</span>'
        "</div>"
    )
    body = (
        f'<div class="tm-chartrow"><div class="tm-yaxis" style="height:220px">'
        f"{tick_html}</div><div style='flex:1;min-width:0'>{svg}</div></div>"
        f'<div class="tm-xaxis">{label_html}</div>'
        if series
        else '<div class="tm-mist" style="font-size:11.5px">no SST series available</div>'
    )

    thermal = assessment.component("thermal")
    tdetail = (thermal.detail if thermal else {}) or {}
    thermal_pairs = [
        ("current SST", fmt(assessment.current_sst_c, ".2f", " °C")),
        ("SST source", assessment.sst_source or "--"),
        ("in-situ vs model divergence", fmt(assessment.sst_cross_check_delta_c, "+.2f", " °C")),
        ("10-year baseline mean", fmt(assessment.baseline_mean_c, ".3f", " °C")),
        ("baseline std deviation", fmt(tdetail.get("baseline_std_c"), ".3f", " °C")),
        ("anomaly", fmt(assessment.anomaly_c, "+.3f", " °C")),
        ("anomaly (standard deviations)", fmt(assessment.anomaly_sigma, "+.3f")),
        ("decadal trend (point estimate)",
         fmt(assessment.trend_c_per_decade, "+.3f", " °C/decade")),
        ("trend standard error", fmt(assessment.trend_stderr_c_per_decade, ".3f")),
        ("trend r squared", fmt(assessment.trend_r2, ".3f")),
        ("trend scored (95% lower bound)",
         fmt(assessment.trend_scored_c_per_decade, ".3f", " °C/decade")),
        ("marine heatwave (> 10y p90)", "ACTIVE" if assessment.marine_heatwave else "none"),
        ("anomaly term / trend term",
         f"{tdetail.get('anomaly_term', '--')} / {tdetail.get('trend_term', '--')}"),
    ]
    if sea_state is not None:
        thermal_pairs.append(("model SST coverage", f"{sea_state.coverage:.0%}"))
        thermal_pairs.append(
            ("current wave height", fmt(sea_state.current_wave_height_m, ".2f", " m"))
        )
    drawer = _drawer("THERMAL DETAIL & TREND STATISTICS", _detail_rows(thermal_pairs))

    return (
        "<section>"
        f'<div class="tm-panelhead">'
        f'{_chip(f"SEA SURFACE TEMPERATURE — {region.name.upper()}")}{legend}</div>'
        f"{body}{drawer}</section>"
    )


def _baseline_panel(snapshot: RegionSnapshot, assessment: StressAssessment) -> str:
    climatology = snapshot.climatology
    if climatology is None or not climatology.yearly_means:
        return (
            "<section>"
            + _chip("OISST DAY-OF-YEAR BASELINE — UNAVAILABLE", block=True)
            + '<div class="tm-mist" style="font-size:11.5px;margin-top:8px">'
            "NOAA OISST baseline could not be retrieved</div></section>"
        )
    years = list(climatology.yearly_means)
    values = list(climatology.yearly_means.values())
    label = (
        f"OISST DAY-OF-YEAR BASELINE — {climatology.anchor_month:02d}-"
        f"{climatology.anchor_day:02d} ±{climatology.window_days}D, "
        f"{climatology.years_covered} YEARS"
    )

    per_year = _mini_table(
        [("Year", False), ("Window mean °C", True)],
        [[str(y), (f"{m:.3f}", "num")] for y, m in climatology.yearly_means.items()],
    )
    base_pairs = [
        ("baseline mean", fmt(climatology.baseline_mean, ".3f", " °C")),
        ("baseline std deviation", fmt(climatology.baseline_std, ".3f", " °C")),
        ("90th percentile (heatwave line)", fmt(climatology.baseline_p90, ".3f", " °C")),
        ("daily observations pooled", f"{climatology.observations:,}"),
        ("years covered", str(climatology.years_covered)),
        ("years that failed to fetch",
         ", ".join(map(str, climatology.failed_years)) or "none"),
        ("day-of-year window",
         f"±{climatology.window_days} d around {climatology.anchor_month:02d}-"
         f"{climatology.anchor_day:02d}"),
        ("OISST grid point",
         f"{climatology.latitude:.3f}, {climatology.longitude:.3f}"
         if climatology.latitude is not None else "--"),
    ]
    drawer = _drawer("PER-YEAR BASELINE & STATISTICS", per_year + _detail_rows(base_pairs))

    return (
        "<section>"
        + _chip(label, block=True)
        + '<div style="margin-top:10px">'
        + _bar_chart(years, values, assessment.current_sst_c)
        + "</div>"
        + drawer
        + "</section>"
    )


def _composition_panels(snapshot: RegionSnapshot) -> str:
    obis = snapshot.obis
    infra = snapshot.infrastructure

    bio_items: list[tuple[str, float, str]] = []
    if obis and obis.phylum_records:
        total = obis.total_classified_records or 1
        top = sorted(obis.phylum_records.items(), key=lambda kv: kv[1], reverse=True)[:6]
        bio_items = [(name, count / total * 100.0, f"{count / total * 100:.1f}%")
                     for name, count in top]
    bio_max = max((m for _, m, _ in bio_items), default=1.0)

    infra_items: list[tuple[str, float, str]] = []
    if infra and infra.type_breakdown:
        top = list(infra.type_breakdown.items())[:6]
        infra_items = [(name.replace("_", " "), float(count), f"{count:,}")
                       for name, count in top]
    infra_max = max((m for _, m, _ in infra_items), default=1.0)

    # --- OBIS drawer: top taxa + the full phylum breakdown ---------------- #
    obis_drawer = ""
    if obis and (obis.top_taxa or obis.phylum_records):
        total = obis.total_classified_records or 1
        parts = []
        if obis.top_taxa:
            # Three columns keep the table inside the half-width panel; class and
            # rank are dropped (rank is almost always "Species").
            parts.append(_mini_table(
                [("Scientific name", False), ("Phylum", False), ("Records", True)],
                [[(t.get("scientificName") or "—", "name"), t.get("phylum") or "—",
                  (f"{t.get('records', 0):,}", "num")]
                 for t in obis.top_taxa[:25]],
            ))
        if obis.phylum_records:
            ordered = sorted(obis.phylum_records.items(), key=lambda kv: kv[1], reverse=True)
            parts.append('<div style="height:8px"></div>')
            parts.append(_mini_table(
                [("Phylum (all)", False), ("Records", True), ("Share", True)],
                [[p, (f"{c:,}", "num"), (f"{c / total:.1%}", "num")] for p, c in ordered],
            ))
        obis_drawer = _drawer(
            f"TOP TAXA & FULL COMPOSITION ({obis.checklist_sampled} taxa sampled)",
            "".join(parts),
        )

    # --- infrastructure drawer: every charted seamark type ---------------- #
    infra_drawer = ""
    if infra and infra.type_breakdown:
        types_table = _mini_table(
            [("Seamark type", False), ("Count", True)],
            [[t.replace("_", " "), (f"{c:,}", "num")]
             for t, c in infra.type_breakdown.items()],
        )
        infra_pairs = [
            ("charted features (total)", f"{infra.total_count:,}"),
            ("nodes / ways / relations",
             f"{infra.node_count:,} / {infra.way_count:,} / {infra.relation_count:,}"),
            ("bounding-box area", f"{infra.area_km2:,.0f} km²"),
            ("density", f"{infra.density_per_1000km2:.2f} per 1000 km²"),
            ("routing/berthing (sampled)",
             f"{infra.traffic_features:,} of {infra.sampled_elements:,}"),
            ("traffic share", f"{infra.traffic_share:.1%}"),
            ("distinct seamark types", str(len(infra.type_breakdown))),
        ]
        infra_drawer = _drawer(
            "FULL SEAMARK BREAKDOWN", types_table + _detail_rows(infra_pairs)
        )

    return (
        '<div style="display:flex;gap:24px">'
        '<section style="flex:1;min-width:0">'
        + _chip("OBIS BIODIVERSITY — PHYLUM COMPOSITION", block=True)
        + f'<div style="margin-top:10px">{_hbars(bio_items, 120, bio_max)}</div>'
        + obis_drawer
        + "</section>"
        '<section style="flex:1;min-width:0">'
        + _chip("OPENSEAMAP MARITIME INFRASTRUCTURE", block=True)
        + f'<div style="margin-top:10px">{_hbars(infra_items, 150, infra_max)}</div>'
        + infra_drawer
        + "</section></div>"
    )


def _stats_strip(snapshot: RegionSnapshot, assessment: StressAssessment,
                 region: Region) -> str:
    obis = snapshot.obis
    infra = snapshot.infrastructure
    climatology = snapshot.climatology
    taxonomic = assessment.component("taxonomic")
    detail = (taxonomic.detail if taxonomic else {}) or {}

    def stat(key: str, value: str) -> str:
        return (
            f'<div class="tm-statrow"><span class="k">{esc(key)}</span>'
            f'<span class="v">{esc(value)}</span></div>'
        )

    left = [
        stat("Occurrence records", f"{obis.records:,}" if obis else "--"),
        stat("Species / taxa", f"{obis.species:,} / {obis.taxa:,}" if obis else "--"),
        stat("Contributing datasets", f"{obis.datasets:,}" if obis else "--"),
        stat("Temporal range",
             f"{obis.year_min}–{obis.year_max}" if obis and obis.year_min else "--"),
        stat("Weighted sensitivity", fmt(detail.get("weighted_sensitivity"), ".3f")),
        stat("Assemblage evenness", fmt(detail.get("evenness"), ".3f")),
        stat("Dominant phylum",
             f"{detail.get('dominant_phylum', '--')} "
             f"({fmt(detail.get('dominant_share'), '.1%')})"),
    ]
    right = [
        stat("Charted seamarks", f"{infra.total_count:,}" if infra else "--"),
        stat("Nodes / ways",
             f"{infra.node_count:,} / {infra.way_count:,}" if infra else "--"),
        stat("Bounding-box area", f"{region.area_km2:,.0f} km²"),
        stat("Seamark density",
             f"{infra.density_per_1000km2:.2f} / 1000 km²" if infra else "--"),
        stat("Routing / berthing",
             f"{infra.traffic_features:,} of {infra.sampled_elements:,}" if infra else "--"),
        stat("Baseline years", str(climatology.years_covered) if climatology else "--"),
        stat("Baseline observations",
             f"{climatology.observations:,}" if climatology else "--"),
        stat("Baseline sigma",
             fmt(climatology.baseline_std if climatology else None, ".3f", " °C")),
    ]
    return (
        '<div class="tm-stats">'
        f'<div class="tm-statcol">{"".join(left)}</div>'
        f'<div class="tm-statcol">{"".join(right)}</div>'
        "</div>"
    )


def _console(snapshot: RegionSnapshot) -> str:
    events = snapshot.telemetry
    latencies = sorted(e.latency_ms for e in events)

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        idx = max(0, min(len(latencies) - 1, math.ceil(p / 100 * len(latencies)) - 1))
        return latencies[idx]

    total_bytes = sum(e.payload_bytes for e in events)
    failed = sum(1 for e in events if not e.ok)

    tiles = [
        ("REQUESTS", str(len(events)), f"{failed} failed"),
        ("WALL CLOCK", f"{snapshot.duration_ms / 1000:.2f}s", "concurrent fetch"),
        ("LATENCY P50", f"{pct(50):.0f}ms", "per request"),
        ("LATENCY P95", f"{pct(95):.0f}ms", "per request"),
        ("PAYLOAD", f"{total_bytes / 1024:.1f} KB", "decoded bytes"),
        ("FETCHED", f"{snapshot.fetched_at:%H:%M:%S}Z", "UTC"),
    ]
    tile_html = "".join(
        f'<div class="tm-tile"><div class="l">{esc(l)}</div>'
        f'<div class="v">{esc(v)}</div><div class="c">{esc(c)}</div></div>'
        for l, v, c in tiles
    )

    header = (
        '<div class="tm-log head"><div>TIME</div><div>SOURCE</div><div>ENDPOINT</div>'
        "<div>STATUS</div><div>LATENCY</div><div>PAYLOAD</div><div></div></div>"
    )
    rows = []
    for event in events[-16:]:
        colour = SOURCE_COLOURS.get(event.source, PAPER_DIM)
        status_colour = CANOPY if event.ok else SIGNAL
        size = (
            f"{event.payload_bytes} B"
            if event.payload_bytes < 1024
            else f"{event.payload_bytes / 1024:.1f} KB"
        )
        stamp = event.started_at.strftime("%H:%M:%S.%f")[:-3] + "Z"
        rows.append(
            '<div class="tm-log">'
            f'<div class="tm-mist">{stamp}</div>'
            f'<div style="color:{colour}">{esc(event.source)}</div>'
            f'<div class="tm-dim">{esc(event.label)}</div>'
            f'<div style="color:{status_colour}">{event.status or "ERR"}</div>'
            f'<div class="tm-dim">{event.latency_ms:.0f}ms</div>'
            f'<div class="tm-dim">{esc(size)}</div>'
            f'<div class="tm-mist">×{event.attempts}</div>'
            "</div>"
        )
    if not rows:
        rows.append('<div class="tm-log"><div class="tm-amber">no transport events</div></div>')

    return (
        '<div class="tm-console">'
        f'<div class="tm-tiles">{tile_html}</div>'
        '<div class="tm-seclabel">CONSOLE — LIVE API TRANSPORT LOG</div>'
        f"{header}{''.join(rows)}</div>"
    )


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def render_body(
    snapshot: RegionSnapshot,
    assessment: StressAssessment,
    config: TerminalConfig,
    *,
    stale: bool = False,
) -> str:
    """Full dashboard body: sidebar plus main column."""
    region = snapshot.region
    main_sections = [
        _sst_panel(snapshot, assessment, region),
        _baseline_panel(snapshot, assessment),
        _composition_panels(snapshot),
        _stats_strip(snapshot, assessment, region),
    ]
    if config.show_console:
        main_sections.append(_console(snapshot))

    return (
        '<div class="tm-root"><div class="tm-body">'
        + _sidebar(snapshot, assessment, stale)
        + f'<main class="tm-main">{"".join(main_sections)}</main>'
        + "</div></div>"
    )
