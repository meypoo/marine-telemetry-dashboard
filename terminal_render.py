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
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from analyzer import StressAssessment
from api_clients import (
    DHW_BLEACHING_LIKELY, DHW_SEVERE, BuoySnapshot, Region, RegionSnapshot,
    is_reef_latitude,
)
from ui import (
    BORDER_STRONG, CANOPY, INK, LINE, MIST, PANEL, PAPER, PAPER_DIM, SIGNAL,
    SOURCE_COLOURS, TerminalConfig, bidi_isolate, esc, fmt, gloss_attr,
    stress_accent,
)

__all__ = [
    "render_topbar_left", "location_subtitle", "render_body", "render_footer",
    "render_alert_banner", "render_comparison",
]
SOURCES_LINE = (
    "SOURCES: NOAA ERDDAP coastwatch.pfeg.noaa.gov (cwwcNDBCMet in-situ buoy, "
    "ncdcOisst21Agg OISST v2.1 day-of-year baseline, and 1985-2012 fixed "
    "climatology for Degree Heating Weeks after NOAA Coral Reef Watch) · "
    "Open-Meteo Marine "
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
        f'<div class="tm-row"><span class="{key_class}"{gloss_attr(key)}>'
        f'{esc(key)}</span>'
        f'<span class="{value_class}" style="color:{colour}">{esc(value)}</span></div>'
    )


def _section(label: str, rows: Iterable[str], *, first: bool = False) -> str:
    divider = "" if first else " tm-divider"
    return (
        f'<div class="{divider.strip() or ""}">'
        f'<div class="tm-seclabel">{esc(label)}</div>{"".join(rows)}</div>'
    )


def _legend_swatch(colour: str, dash: str = "") -> str:
    """A short inline-SVG line for a chart legend.

    Drawn rather than typed: the box-drawing glyphs (``━`` vs ``╌``) fall back to
    visually identical short dashes in Space Mono, so a typed legend cannot say
    which series is solid and which is dashed. An SVG swatch reproduces the
    exact stroke the chart uses.
    """
    attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        '<svg width="20" height="8" viewBox="0 0 20 8" '
        'style="vertical-align:middle;margin-right:4px">'
        f'<line x1="0" y1="4" x2="20" y2="4" stroke="{colour}" '
        f'stroke-width="2"{attr}/></svg>'
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


# --------------------------------------------------------------------------- #
# SVG charts
# --------------------------------------------------------------------------- #
def _sparkline(values: Sequence[float], width: int = 300, height: int = 30) -> str:
    points = [float(v) for v in _downsample([v for v in values if v is not None], 13)]
    if len(points) < 2:
        return (
            f'<svg class="tm-spark" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" preserveAspectRatio="none"></svg>'
        )
    low, high = min(points), max(points)
    span = (high - low) or 1.0
    coords = " ".join(
        f"{i / (len(points) - 1) * width:.1f},"
        f"{height - (v - low) / span * (height - 4) - 2:.1f}"
        for i, v in enumerate(points)
    )
    return (
        f'<svg class="tm-spark" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" '
        f'preserveAspectRatio="none">'
        f'<polyline points="{coords}" fill="none" stroke="{MIST}" stroke-width="1.5"/>'
        "</svg>"
    )


def _line_chart(
    series: list[tuple[str, str, list[float | None]]],
    baseline: float | None,
    width: int = 1000,
    height: int = 220,
    x_fractions: list[list[float] | None] | None = None,
    dashed: Sequence[bool] | None = None,
) -> tuple[str, list[float]]:
    """Multi-series line chart. Returns (svg, y_tick_values).

    ``x_fractions`` optionally gives each series its own horizontal positions on
    0-1 instead of even index spacing. Two series only share an x-axis honestly
    when they are positioned on a common domain — evenly spacing series of
    different time spans silently stretches the shorter one across the longer
    one's window.

    ``dashed`` marks a series as a dashed stroke, so two lines stay
    distinguishable without introducing a third accent colour.
    """
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
        # MIST, not BORDER_STRONG: this line is the interpretive anchor of the
        # chart and BORDER_STRONG sits at 1.9:1 on INK, below the 3:1 minimum
        # for meaningful non-text marks — it was effectively invisible.
        # Dotted (2,3), distinct from the model series' dash (6,4) — two dashed
        # MIST lines on one chart are otherwise hard to tell apart. The legend
        # swatches mirror these patterns exactly.
        parts.append(
            f'<line x1="0" y1="{y_of(baseline):.1f}" x2="{width}" '
            f'y2="{y_of(baseline):.1f}" stroke="{MIST}" stroke-width="1" '
            f'stroke-dasharray="2,3"/>'
        )
    for index, (_, colour, values) in enumerate(series):
        xs = None
        if x_fractions is not None and index < len(x_fractions):
            xs = x_fractions[index]
        coords = []
        for i, v in enumerate(values):
            if v is None:
                continue
            if xs is not None and i < len(xs):
                x = xs[i] * width
            else:
                x = i / max(1, len(values) - 1) * width
            coords.append(f"{x:.1f},{y_of(float(v)):.1f}")
        if len(coords) >= 2:
            stroke_dash = ""
            if dashed is not None and index < len(dashed) and dashed[index]:
                stroke_dash = ' stroke-dasharray="6,4"'
            parts.append(
                f'<polyline points="{" ".join(coords)}" fill="none" stroke="{colour}" '
                f'stroke-width="2"{stroke_dash}/>'
            )
    parts.append("</svg>")

    ticks = [high - (high - low) * i / 5 for i in range(6)]
    return "".join(parts), ticks


def _bar_chart(years: list[int], values: list[float], threshold: float | None) -> str:
    """Vertical bars in a 130px track with an overlaid amber threshold line.

    The scale is anchored just below the smallest value rather than at zero:
    yearly SST means differ by tenths of a degree, and on a zero-based scale
    every bar renders at ~90% height and the differences vanish.
    """
    if not values:
        return f'<div class="tm-mist" style="font-size:11.5px">baseline unavailable</div>'

    bounds = [*values, *( [threshold] if threshold is not None else [] )]
    scale_min = min(bounds) - 0.5
    scale_max = max(bounds) + 0.5
    span = scale_max - scale_min

    bars = "".join(
        f'<div class="tm-bar" '
        f'style="height:{max(2.0, (v - scale_min) / span * 130):.1f}px" '
        f'title="{y}: {v:.2f} degC"></div>'
        for y, v in zip(years, values)
    )
    line = ""
    if threshold is not None:
        offset = min(130.0, max(0.0, (threshold - scale_min) / span * 130))
        line = f'<div class="tm-threshold" style="bottom:{offset:.1f}px"></div>'

    ticks = "".join(
        f"<div>{scale_min + span * i / 4:.1f}</div>" for i in range(4, -1, -1)
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
            f'<div class="tm-hlabel" style="width:{label_width}px" '
            f'title="{esc(label)}">{esc(label)}</div>'
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
    shatter it across several lines; overflow-hidden + ellipsis stop it bleeding
    into the neighbouring search column when its own column gets narrower than
    the text."""
    return (
        '<div class="tm-topleft">'
        '<div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
        '<span class="tm-live"></span>'
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
    # Isolated: a searched RTL name would otherwise drag the separators and the
    # coverage tail around it into the wrong visual order.
    return (
        f"Live environmental risk telemetry · {bidi_isolate(region.name)}{tail}"
    )


#: Meteorological seasons by month index, northern hemisphere. The southern
#: hemisphere is the same sequence offset by six months.
_SEASONS = ("winter", "winter", "spring", "spring", "spring", "summer",
            "summer", "summer", "autumn", "autumn", "autumn", "winter")

#: Which half of the year a season sits in, for the framing note. Spring and
#: autumn are named transitional rather than forced into warm/cool.
_SEASON_PHASE = {
    "summer": "warm season", "winter": "cool season",
    "spring": "transitional", "autumn": "transitional",
}


def season_for(latitude: float, at: datetime) -> tuple[str, str]:
    """(hemisphere, season) at a latitude and date, meteorological convention.

    Within 10° of the equator the temperate season names describe nothing — the
    year there divides into wet and dry, not warm and cool — so the season is
    reported as "tropical" instead of asserting a warm/cool half-year that does
    not exist at that latitude.
    """
    if abs(latitude) < 10.0:
        return "equatorial", "tropical"
    if latitude >= 0:
        return "northern", _SEASONS[at.month - 1]
    return "southern", _SEASONS[(at.month + 5) % 12]


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
    thermal = assessment.component("thermal")
    thermal_available = bool(thermal and thermal.available)

    status = f"{assessment.band} · {'STALE' if stale else 'LIVE'}"
    # The anomaly is measured against the *same calendar date* in the baseline
    # decade. That date-matching is what stops the index reporting "it is
    # winter" as stress — and is exactly why it cannot see damage done in an
    # earlier warm season: a reef cooked last summer sits at a genuinely normal
    # temperature in its winter and scores clean. The season is stated on the
    # face so a cool-season reading is not misread as an all-clear.
    latitude, _ = snapshot.region.centroid
    hemisphere, season = season_for(latitude, snapshot.fetched_at)
    phase = _SEASON_PHASE.get(season)
    season_line = f"{hemisphere} hemisphere · {season}".upper()
    if phase:
        season_line += f" ({phase})".upper()
    season_block = (
        f'<div style="font-size:10px;letter-spacing:0.08em;margin-top:8px;'
        f'color:{SIGNAL if season == "winter" else MIST}">{esc(season_line)}</div>'
        '<div class="tm-mist" style="font-size:10px;line-height:1.5;margin-top:4px">'
        "Anomaly is vs the same calendar date in the baseline decade — a "
        "present-conditions reading, with no memory of heat stress accumulated "
        "in earlier warm seasons.</div>"
    )

    hero = (
        '<div>'
        + _chip("STRESS SCORE")
        + f'<div class="tm-hero-num" style="color:{stress_accent(score)}">'
        f'{fmt(score, ".1f")}</div>'
        # The product is a "Health Index" but this number is stress, so the
        # scale's direction is stated on the face rather than left to be
        # inferred from the band words (a bare "LOW" reads equally as low
        # health). Confidence sits here too: a 0.0 from two dead components
        # would otherwise be indistinguishable from a pristine reading.
        + f'<div class="tm-mist" style="font-size:10px;letter-spacing:0.08em;'
        f'margin:-4px 0 8px 0">0 = UNDISTURBED · 100 = SEVERE · HIGHER IS WORSE'
        f'</div>'
        + _chip(status, large=True)
        + f'<div class="tm-mist" style="font-size:10.5px;margin-top:6px">'
        f"confidence {assessment.confidence:.0%}</div>"
        + season_block
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
            # Say *why* on the face of the panel. The reason previously lived
            # only inside a collapsed drawer, leaving "excluded" and a reduced
            # confidence figure with nothing connecting them.
            if component.unavailable_reason:
                derivation.append(
                    '<div class="tm-mist" style="font-size:10.5px;'
                    'margin:-2px 0 6px 0;padding-left:2px">'
                    f"{esc(component.unavailable_reason)}</div>"
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
        # Amber alone would carry the "elevated" meaning for a reader who does
        # not perceive the colour; a text marker rides alongside it.
        _row("Anomaly vs 10y baseline",
             fmt(anomaly, "+.2f", " °C") + (" ▲" if (anomaly or 0) > 0.5 else ""),
             colour=SIGNAL if (anomaly or 0) > 0.5 else PAPER,
             small_key=True, medium_value=True),
        _row("Anomaly (sigma)",
             fmt(sigma, "+.2f") + (" ▲" if (sigma or 0) >= 1.29 else ""),
             colour=SIGNAL if (sigma or 0) >= 1.29 else PAPER,
             small_key=True, medium_value=True),
        # The point estimate must never appear without its uncertainty: a
        # ten-point regression routinely produces physically implausible slopes,
        # which is exactly why scoring uses the lower bound instead.
        _row("Decadal trend",
             (
                 f"{assessment.trend_c_per_decade:+.2f} "
                 f"± {assessment.trend_stderr_c_per_decade:.2f} °C/dec"
                 if assessment.trend_c_per_decade is not None
                 and assessment.trend_stderr_c_per_decade is not None
                 else fmt(assessment.trend_c_per_decade, "+.2f", " °C/dec")
             ),
             small_key=True, medium_value=True),
        _row("Trend scored", fmt(assessment.trend_scored_c_per_decade, ".2f", " °C/dec"),
             small_key=True, medium_value=True),
        # "None" would assert a determination that was never made when thermal
        # scoring was excluded; only claim it when the baseline supported it.
        _row("Marine heatwave",
             ("ACTIVE" if assessment.marine_heatwave else "None")
             if thermal_available else "--",
             colour=SIGNAL if assessment.marine_heatwave else PAPER,
             small_key=True, medium_value=True),
        _row("Baseline mean", fmt(assessment.baseline_mean_c, ".2f", " °C"),
             small_key=True, medium_value=True),
        _row("Seamark density",
             f"{infra.density_per_1000km2:.1f} /1000km²" if infra else "--",
             small_key=True, medium_value=True),
        _row("Species observed", f"{obis.species:,}" if obis else "--",
             small_key=True, medium_value=True),
        _row("Confidence",
             f"{assessment.confidence:.0%}"
             + ("" if assessment.confidence >= 0.75 else " reduced"),
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
            _row("Observation age",
                 fmt(buoy.age_hours, ".1f", " h")
                 + (" stale" if (buoy.age_hours or 0) >= 6 else ""),
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

    # The two series cover different windows — the model returns ~6 days, the
    # buoy history 48 h — so they must be positioned on a SHARED time domain.
    # Spacing both evenly across the panel would stretch the 48 h buoy trace
    # across the model's six days, making the in-situ/model cross-check (the
    # entire point of this panel) a comparison of unlike instants.
    model_values = _downsample(
        [v for v in (sea_state.sst_c if sea_state else [])], 24
    )
    model_times = _downsample(list(sea_state.times) if sea_state else [], 24)
    in_situ_values = _downsample(list(buoy.history_wtmp_c) if buoy else [], 24)
    in_situ_times = _downsample(list(buoy.history_times) if buoy else [], 24)

    stamps = [t for t in (*model_times, *in_situ_times) if t is not None]
    domain_start = min(stamps) if stamps else None
    domain_end = max(stamps) if stamps else None
    total_seconds = (
        (domain_end - domain_start).total_seconds()
        if domain_start is not None and domain_end is not None
        else 0.0
    )

    def fractions_for(times: list) -> list[float] | None:
        """Map timestamps onto 0-1 of the shared domain."""
        if not times or total_seconds <= 0 or domain_start is None:
            return None
        return [
            (t - domain_start).total_seconds() / total_seconds
            if t is not None else 0.0
            for t in times
        ]

    series: list[tuple[str, str, list[float | None]]] = []
    x_fractions: list[list[float] | None] = []
    dashed: list[bool] = []
    if any(v is not None for v in in_situ_values):
        series.append(("in-situ", CANOPY, in_situ_values))
        # Only trust the buoy's own timestamps when they pair with the values;
        # otherwise fall back to even spacing rather than inventing instants.
        x_fractions.append(
            fractions_for(in_situ_times)
            if len(in_situ_times) == len(in_situ_values) else None
        )
        dashed.append(False)
    if any(v is not None for v in model_values):
        series.append(("model", MIST, model_values))
        x_fractions.append(
            fractions_for(model_times)
            if len(model_times) == len(model_values) else None
        )
        # Dashed so the two green-family lines stay separable without adding a
        # third accent to a deliberately two-accent palette.
        dashed.append(True)

    svg, ticks = _line_chart(
        series, assessment.baseline_mean_c,
        x_fractions=x_fractions, dashed=dashed,
    )
    tick_html = "".join(f"<div>{t:.1f}</div>" for t in ticks)

    # Labels are spaced evenly by flexbox, so they must mark evenly spaced
    # *times* across the shared domain, not sampled instants.
    label_count = 6
    if domain_start is not None and total_seconds > 0:
        labels = [
            (domain_start + timedelta(seconds=total_seconds * i / (label_count - 1)))
            .strftime("%m-%d %Hh")
            for i in range(label_count)
        ]
    elif domain_start is not None:
        labels = [domain_start.strftime("%m-%d %Hh")]
    else:
        labels = []
    label_html = "".join(f"<div>{esc(l)}</div>" for l in labels)

    station = buoy.station if buoy and buoy.station else "—"
    in_situ_span = ""
    if in_situ_times and total_seconds > 0:
        covered = (max(in_situ_times) - min(in_situ_times)).total_seconds() / 3600.0
        in_situ_span = f" · {covered:.0f}h window"
    legend = (
        '<div class="tm-legend">'
        f'<span style="color:{CANOPY}">{_legend_swatch(CANOPY)}'
        f"NDBC {esc(station)} in-situ{esc(in_situ_span)}</span>"
        f'<span style="color:{MIST}">{_legend_swatch(MIST, "6,4")}'
        "Open-Meteo model</span>"
        f'<span style="color:{MIST}">{_legend_swatch(MIST, "2,3")}'
        "10y baseline</span>"
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
        f'{_chip(f"SEA SURFACE TEMPERATURE — {bidi_isolate(region.name.upper())}")}'
        f"{legend}</div>"
        f"{body}{drawer}</section>"
    )


def _baseline_panel(snapshot: RegionSnapshot, assessment: StressAssessment) -> str:
    climatology = snapshot.climatology
    if climatology is None or not climatology.yearly_means:
        # Distinguish a failed fetch from a successful one that returned no
        # data — the same distinction marine_coverage draws between "unknown"
        # and "none". Reporting a retrieval failure for a landlocked point is
        # simply untrue, and sends the reader looking for an outage.
        if climatology is None:
            reason = "NOAA OISST baseline could not be retrieved (fetch failed)"
        else:
            reason = (
                "NOAA OISST returned no sea-surface temperature for this point "
                "in any of the baseline years — expected on land or outside "
                "ocean coverage, not a fetch failure"
            )
        return (
            "<section>"
            + _chip("OISST DAY-OF-YEAR BASELINE — UNAVAILABLE", block=True)
            + '<div class="tm-mist" style="font-size:11.5px;margin-top:8px">'
            + esc(reason)
            + "</div></section>"
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

    # The amber rule across the bars is the present-day SST. It had no label
    # anywhere, leaving an unexplained line over ten green bars; name it, and
    # give its value, the same way the SST panel labels its series.
    current = assessment.current_sst_c
    legend = (
        '<div class="tm-legend">'
        + f'<span style="color:{CANOPY}">{_legend_swatch(CANOPY)}'
        + "annual window mean</span>"
        + (
            f'<span style="color:{SIGNAL}">{_legend_swatch(SIGNAL)}'
            f"current SST {current:.2f} °C</span>"
            if current is not None
            else ""
        )
        + "</div>"
    )
    return (
        "<section>"
        + f'<div class="tm-panelhead"><div>{_chip(label)}</div>{legend}</div>'
        + '<div style="margin-top:10px">'
        + _bar_chart(years, values, current)
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
        '<div class="tm-duo">'
        '<section style="flex:1;min-width:0">'
        # Named as a sample, because that is what it is: the shares below are
        # computed over the top-N taxa checklist, not over the region's full
        # occurrence record count shown in the stats strip.
        + _chip("OBIS BIODIVERSITY — PHYLUM MIX (TOP-TAXA SAMPLE)", block=True)
        + f'<div style="margin-top:10px">{_hbars(bio_items, 120, bio_max)}</div>'
        + obis_drawer
        + "</section>"
        '<section style="flex:1;min-width:0">'
        + _chip("OPENSEAMAP MARITIME INFRASTRUCTURE", block=True)
        + f'<div style="margin-top:10px">{_hbars(infra_items, 150, infra_max)}</div>'
        + infra_drawer
        + "</section></div>"
    )


def _traffic_summary(infra) -> str:
    """Routing/berthing share, distinguishing 'none here' from 'not sampled'."""
    if infra is None:
        return "--"
    if infra.sampled_elements:
        return f"{infra.traffic_features:,} of {infra.sampled_elements:,}"
    if infra.total_count:
        return "tag sample unavailable"
    return "none charted here"


def _stats_strip(snapshot: RegionSnapshot, assessment: StressAssessment,
                 region: Region) -> str:
    obis = snapshot.obis
    infra = snapshot.infrastructure
    climatology = snapshot.climatology
    taxonomic = assessment.component("taxonomic")
    detail = (taxonomic.detail if taxonomic else {}) or {}

    def stat(key: str, value: str) -> str:
        return (
            f'<div class="tm-statrow"><span class="k"{gloss_attr(key)}>'
            f'{esc(key)}</span>'
            f'<span class="v">{esc(value)}</span></div>'
        )

    left = [
        stat("Occurrence records", f"{obis.records:,}" if obis else "--"),
        stat("Species / taxa", f"{obis.species:,} / {obis.taxa:,}" if obis else "--"),
        stat("Contributing datasets", f"{obis.datasets:,}" if obis else "--"),
        stat("Temporal range",
             f"{obis.year_min}–{obis.year_max}" if obis and obis.year_min else "--"),
        stat("Taxa sampled for mix",
             f"{obis.checklist_sampled:,} of {obis.taxa:,}" if obis else "--"),
        stat("Weighted sensitivity", fmt(detail.get("weighted_sensitivity"), ".3f")),
        stat("Assemblage evenness", fmt(detail.get("evenness"), ".3f")),
        stat("Dominant phylum (in sample)",
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
        # Three distinct states, and they must not be conflated: a real count,
        # a genuinely empty box (measured, zero features), and features that
        # exist but whose tags could not be sampled (composition unknown).
        stat("Routing / berthing", _traffic_summary(infra)),
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


#: Full scale of the DHW gauge, in °C-weeks. Chosen so NOAA's two calibrated
#: thresholds (4 and 8) land at a third and two thirds of the track rather than
#: at its very end, which would make a severe reading indistinguishable from an
#: off-scale one.
_DHW_GAUGE_MAX = 12.0


def _thermal_stress_panel(snapshot: RegionSnapshot,
                          assessment: StressAssessment) -> str:
    """Degree Heating Weeks against NOAA's two calibrated thresholds.

    The gauge exists because this is the only component whose number means
    something outside this dashboard: 4 °C-weeks is "bleaching likely" and 8 is
    "severe with mortality" in NOAA's published calibration, so the reading is
    drawn against those marks rather than against an arbitrary 0-100.
    """
    stress = snapshot.thermal_stress
    component = assessment.component("thermal_stress")
    chip = _chip("ACCUMULATED HEAT STRESS — DEGREE HEATING WEEKS", block=True)

    if stress is None or stress.dhw_c_weeks is None or stress.mmm_c is None:
        reason = (
            component.unavailable_reason
            if component is not None and component.unavailable_reason
            else "thermal-stress feed unavailable"
        )
        return (
            '<section>' + chip
            + f'<div class="tm-mist" style="font-size:11.5px;margin-top:10px">'
            f"{esc(reason)}</div></section>"
        )

    dhw = stress.dhw_c_weeks
    latitude = snapshot.region.centroid[0]
    reef = is_reef_latitude(latitude)
    fill_pct = min(100.0, dhw / _DHW_GAUGE_MAX * 100.0)
    accent = SIGNAL if (reef and dhw >= DHW_BLEACHING_LIKELY) else CANOPY

    def mark(value: float, label: str) -> str:
        left = value / _DHW_GAUGE_MAX * 100.0
        return (
            f'<div style="position:absolute;left:{left:.1f}%;top:0;bottom:0;'
            f'width:1px;background:{BORDER_STRONG}"></div>'
            f'<div style="position:absolute;left:{left:.1f}%;top:18px;'
            f'font-size:9.5px;color:{MIST};transform:translateX(-50%);'
            f'white-space:nowrap">{esc(label)}</div>'
        )

    gauge = (
        '<div style="margin-top:12px">'
        f'<div style="position:relative;height:16px;background:{PANEL}">'
        f'<div style="width:{fill_pct:.1f}%;height:16px;background:{accent}"></div>'
        + mark(DHW_BLEACHING_LIKELY, f"{DHW_BLEACHING_LIKELY:.0f} bleaching likely")
        + mark(DHW_SEVERE, f"{DHW_SEVERE:.0f} severe")
        + '</div>'
        f'<div style="height:26px"></div>'
        '</div>'
    )

    # State the interpretation in words, because the gauge marks are coral
    # calibration and a reader outside the tropics should not apply them.
    if not reef:
        verdict = (
            "outside reef latitudes — real accumulated warm anomaly, but NOAA's "
            "bleaching bands do not apply here"
        )
    elif dhw >= DHW_SEVERE:
        verdict = "at or above NOAA severe: bleaching with mortality expected"
    elif dhw >= DHW_BLEACHING_LIKELY:
        verdict = "at or above NOAA threshold: significant bleaching likely"
    else:
        verdict = "below NOAA's bleaching threshold in the current window"

    rows = [
        _row("Degree heating weeks", f"{dhw:.2f} °C-weeks",
             colour=accent, small_key=True, medium_value=True),
        _row("Warmest-month mean (MMM)",
             f"{stress.mmm_c:.2f} °C"
             + (f" · month {stress.mmm_month}" if stress.mmm_month else ""),
             small_key=True, medium_value=True),
        _row("Latest SST", fmt(stress.latest_sst_c, ".2f", " °C"),
             small_key=True, medium_value=True),
        _row("HotSpot vs MMM",
             fmt(stress.hotspot_c, "+.2f", " °C")
             + (" ▲" if (stress.hotspot_c or 0) >= 1.0 else ""),
             colour=SIGNAL if (stress.hotspot_c or 0) >= 1.0 else PAPER,
             small_key=True, medium_value=True),
        _row("Accumulation window",
             f"{stress.window_days // 7} weeks · {stress.observations} daily obs",
             small_key=True, medium_value=True),
    ]

    detail_pairs = [
        ("MMM source", stress.mmm_source or "--"),
        ("MMM (°C)", f"{stress.mmm_c:.2f}"),
        ("warmest month", str(stress.mmm_month or "--")),
        ("latest observation", str(stress.latest_day or "--")),
        ("observation lag (days)", str(stress.lag_days if stress.lag_days is not None else "--")),
        ("daily observations", str(stress.observations)),
        ("window (days)", str(stress.window_days)),
        ("grid cell", f"{stress.latitude}, {stress.longitude}"),
        ("reef latitude", "yes" if reef else "no"),
    ]
    notes = "".join(
        f'<div class="tm-mist" style="font-size:10.5px;line-height:1.5;'
        f'margin-top:4px">· {esc(n)}</div>'
        for n in (component.notes if component is not None else [])
    )
    drawer = _drawer(
        "DEGREE HEATING WEEK DERIVATION", _detail_rows(detail_pairs) + notes
    )

    return (
        '<section>' + chip + gauge + "".join(rows)
        + f'<div class="tm-mist" style="font-size:10.5px;line-height:1.5;'
        f'margin-top:8px">{esc(verdict)}</div>'
        + drawer
        + "</section>"
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

    # WALL CLOCK is this cycle's live-tier fetch; the request/latency/payload
    # tiles span the merged telemetry of both tiers, so a cached context feed
    # from hours ago is counted there but not in the wall clock. The captions
    # say which is which so "REQUESTS 14 / WALL CLOCK 1.5s" is not read as a
    # contradiction.
    tiles = [
        ("REQUESTS", str(len(events)), f"both tiers · {failed} failed"),
        ("WALL CLOCK", f"{snapshot.duration_ms / 1000:.2f}s", "live tier this cycle"),
        ("LATENCY P50", f"{pct(50):.0f}ms", "across all feeds"),
        ("LATENCY P95", f"{pct(95):.0f}ms", "across all feeds"),
        ("PAYLOAD", f"{total_bytes / 1024:.1f} KB", "all feeds decoded"),
        ("FETCHED", f"{snapshot.fetched_at:%H:%M:%S}Z", "live tier, UTC"),
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
# Score history
# --------------------------------------------------------------------------- #
def _history_panel(history: Sequence[dict]) -> str:
    """Recorded stress score over time, with its full log in a drawer.

    Unlike every other panel this one is not derived from the current fetch: it
    reads the append-only history the cache keeps, so it is the only view that
    shows whether a region is getting worse rather than how it is right now.
    """
    parsed: list[tuple[datetime, float]] = []
    for entry in history:
        try:
            parsed.append(
                (datetime.fromisoformat(entry["at"]), float(entry["score"]))
            )
        except (KeyError, TypeError, ValueError):
            continue
    # Sorted because the chart maps time to horizontal position; an out-of-order
    # line (or a negative window) would otherwise be drawn as fact.
    parsed.sort(key=lambda pair: pair[0])
    stamps = [p[0] for p in parsed]
    points = [p[1] for p in parsed]

    head = (
        f'<div class="tm-panelhead"><div>{_chip("STRESS SCORE HISTORY")}</div>'
        '<div class="tm-legend">'
        f'<span style="color:{MIST}">● one point per scored load, at most one '
        "per 30 min</span>"
        "</div></div>"
    )

    if len(points) < 2:
        return (
            f"<section>{head}"
            '<div class="tm-mist" style="font-size:11.5px">'
            "collecting — a trend appears once this location has been scored at "
            "least twice (roughly one point per half hour of running)."
            "</div></section>"
        )

    first, last = points[0], points[-1]
    delta = last - first
    span_hours = (stamps[-1] - stamps[0]).total_seconds() / 3600.0
    span = f"{span_hours:.1f} h" if span_hours < 48 else f"{span_hours / 24:.1f} d"

    # Position each point by *when* it was recorded, not by its ordinal. History
    # accrues irregularly (only while a tab is open), so index spacing would draw
    # an hour-long jump and a week-long drift as the same slope.
    origin = stamps[0].timestamp()
    total = stamps[-1].timestamp() - origin
    fractions = (
        [(s.timestamp() - origin) / total for s in stamps]
        if total > 0
        else [i / max(1, len(stamps) - 1) for i in range(len(stamps))]
    )
    svg, ticks = _line_chart(
        [("score", CANOPY, [*points])], None, height=120, x_fractions=[fractions]
    )
    tick_html = "".join(f"<div>{t:.0f}</div>" for t in ticks)
    # The label row is spaced evenly by flexbox, so its labels must be evenly
    # spaced in *time* to sit under the right part of the line — the sampled
    # instants are irregular and would not line up.
    label_count = 4
    label_html = "".join(
        f'<div>{esc(datetime.fromtimestamp(origin + total * i / (label_count - 1), tz=timezone.utc).strftime("%m-%d %Hh"))}</div>'
        for i in range(label_count)
    )

    summary = "".join(
        [
            _row("Points recorded", f"{len(points):,}", small_key=True),
            _row("Window", span, small_key=True),
            _row("First / latest", f"{first:.1f} → {last:.1f}", small_key=True),
            _row("Change", f"{delta:+.1f}",
                 colour=SIGNAL if delta > 0 else CANOPY, small_key=True),
            _row("Peak / floor", f"{max(points):.1f} / {min(points):.1f}",
                 small_key=True),
        ]
    )

    log = _mini_table(
        [("recorded (UTC)", False), ("score", True), ("band", False),
         ("confidence", True)],
        [
            [
                entry.get("at", "")[:19].replace("T", " "),
                fmt(entry.get("score"), ".1f"),
                str(entry.get("band", "")),
                fmt(entry.get("confidence"), ".0%"),
            ]
            for entry in list(history)[-40:][::-1]
        ],
    )

    return (
        f"<section>{head}"
        '<div class="tm-chartrow"><div class="tm-yaxis" style="height:120px">'
        f"{tick_html}</div>{svg}</div>"
        f'<div class="tm-xaxis">{label_html}</div>'
        f'<div style="margin-top:10px">{summary}</div>'
        + _drawer("FULL RECORDED HISTORY", log)
        + "</section>"
    )


def render_alert_banner(assessment: StressAssessment, threshold: float,
                        previous: float | None) -> str:
    """Amber alert row shown when the score is at or above ``threshold``.

    Uses the one amber accent like every other warning — there is no alert-red
    in this palette. Returns an empty string when nothing has crossed.
    """
    score = assessment.score
    if score is None or score < threshold:
        return ""
    if previous is None:
        movement = "no previous reading recorded"
    else:
        delta = score - previous
        if abs(delta) < 0.05:
            movement = f"unchanged from the previous reading ({previous:.1f})"
        else:
            direction = "up" if delta > 0 else "down"
            movement = (
                f"{direction} {abs(delta):.1f} from the previous reading "
                f"({previous:.1f})"
            )
    # Threshold is user-settable via ?alert=, so it may carry a decimal; :g
    # keeps 70 as "70" without rounding 72.5 down to "72".
    return (
        '<div class="tm-root"><div style="padding:8px 32px;'
        f'border-bottom:1px solid {LINE};font-size:11.5px;color:{SIGNAL}">'
        f"ALERT: stress score {score:.1f} ({esc(assessment.band)}) is at or above "
        f"the {threshold:g} threshold — {esc(movement)}. "
        "Higher scores mean more stress."
        "</div></div>"
    )


def render_comparison(
    primary: StressAssessment,
    other: StressAssessment,
    *,
    other_stale: bool = False,
) -> str:
    """Compact A/B strip comparing two regions' scores side by side."""
    def cell(assessment: StressAssessment, tag: str) -> str:
        score = assessment.score
        colour = stress_accent(score)  # the 70/30 rule lives in ui, not here
        value = fmt(score, ".1f")
        return (
            '<div class="tm-statcol">'
            f'<div class="tm-seclabel">{esc(tag)}</div>'
            f'<div style="font-size:26px;font-weight:700;color:{colour};'
            f'line-height:1.2">{esc(value)}</div>'
            f'<div class="tm-mist" style="font-size:11.5px">'
            f"{esc(bidi_isolate(assessment.region_name))} · "
            f"{esc(assessment.band)} · "
            f"confidence {assessment.confidence:.0%}</div>"
            "</div>"
        )

    if primary.score is not None and other.score is not None:
        delta = primary.score - other.score
        verdict = (
            f"{esc(bidi_isolate(primary.region_name))} is {abs(delta):.1f} points "
            f"{'more' if delta > 0 else 'less'} stressed"
            if abs(delta) >= 0.05
            else "both regions score the same"
        )
        delta_colour = SIGNAL if delta > 0 else CANOPY
    else:
        delta = None
        verdict = "one region has no score, so the two are not comparable"
        delta_colour = MIST

    stale_note = " · comparison from cached data" if other_stale else ""
    return (
        '<section><div class="tm-panelhead">'
        f'<div>{_chip("REGION COMPARISON")}</div>'
        f'<div class="tm-legend"><span style="color:{MIST}">'
        f"{esc(verdict)}{esc(stale_note)}</span></div></div>"
        '<div class="tm-stats" style="border-top:none;padding-top:0">'
        + cell(primary, "THIS LOCATION")
        + cell(other, "COMPARED WITH")
        + '<div class="tm-statcol">'
        + '<div class="tm-seclabel">DIFFERENCE</div>'
        + f'<div style="font-size:26px;font-weight:700;color:{delta_colour};'
        + f'line-height:1.2">{esc(fmt(delta, "+.1f"))}</div>'
        + '<div class="tm-mist" style="font-size:11.5px">stress points</div>'
        + "</div></div></section>"
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
    history: Sequence[dict] | None = None,
    comparison: str = "",
) -> str:
    """Full dashboard body: sidebar plus main column."""
    region = snapshot.region
    main_sections = [
        _sst_panel(snapshot, assessment, region),
        _thermal_stress_panel(snapshot, assessment),
        _baseline_panel(snapshot, assessment),
        _composition_panels(snapshot),
    ]
    if comparison:
        main_sections.append(comparison)
    if history is not None:
        main_sections.append(_history_panel(history))
    main_sections.append(_stats_strip(snapshot, assessment, region))
    if config.show_console:
        main_sections.append(_console(snapshot))

    return (
        '<div class="tm-root"><div class="tm-body">'
        + _sidebar(snapshot, assessment, stale)
        + f'<main class="tm-main">{"".join(main_sections)}</main>'
        + "</div></div>"
    )
