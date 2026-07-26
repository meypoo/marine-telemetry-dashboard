"""Rendering for Data Lab results.

Kept separate from `page_lab.py` so the presentation of a report can be
exercised with a real dataset without going through a file-upload widget —
Streamlit's AppTest harness cannot drive `st.file_uploader`, and the rendering
is where most of the surface area lives.

Every function takes an already-computed :class:`~ml_analysis.AnalysisReport`
and renders it; none of them compute statistics.

Colour use follows the project palette: CANOPY for primary marks, SIGNAL
(amber) for anything flagged. There is deliberately no alert-red hue.
"""

from __future__ import annotations

from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from ml_analysis import AnalysisReport, DatasetProfile
from ui import (
    CANOPY, LINE, MIST, PANEL, PAPER, SIGNAL,
    fmt, kv_rows, metric_box, panel_title, style_chart,
)

__all__ = [
    "render_summary",
    "render_parameter",
    "render_structure",
    "render_anomalies",
    "render_inventory",
    "render_export",
    "render_footer",
]


def _slope_units(unit: str | None, per: str) -> str:
    """Label for a rate. Without a known unit the label must still read as a
    rate: ``"/year".strip("/")`` produced the bare word "year", which names the
    denominator as if it were the quantity being measured."""
    return f"{unit}/{per}" if unit else f"per {per}"


def _parameter_table(report: AnalysisReport) -> pd.DataFrame:
    """One row per analysed parameter — the report's headline numbers, flat.

    This is the shape people actually want in a spreadsheet; the JSON export
    carries the full nested report for anyone who needs everything.
    """
    rows = []
    for p in report.parameters:
        trend, season, changes = p.trend, p.seasonality, p.changepoints
        rows.append(
            {
                "column": p.column,
                "parameter": p.canonical_parameter or "",
                "unit": p.unit or "",
                "n": p.n,
                "mean": p.mean,
                "median": p.median,
                "std": p.std,
                "min": p.minimum,
                "max": p.maximum,
                "trend_available": trend.available,
                "trend_direction": trend.direction,
                "trend_significant": trend.significant,
                "theil_sen_slope": trend.theil_sen_slope,
                "theil_sen_low": trend.theil_sen_low,
                "theil_sen_high": trend.theil_sen_high,
                "slope_units": _slope_units(p.unit, trend.per),
                "ols_slope": trend.ols_slope,
                "ols_stderr": trend.ols_stderr,
                "ols_p": trend.ols_p,
                "ols_r2": trend.ols_r2,
                "mk_tau": trend.mk_tau,
                "mk_p_adjusted": trend.mk_p_adjusted,
                "effective_n": trend.effective_n,
                "seasonally_adjusted": trend.seasonally_adjusted,
                "raw_ols_slope": trend.raw_ols_slope,
                "total_change": trend.total_change,
                "seasonality_detected": season.detected,
                "period_days": season.period_used_days,
                "period_label": season.period_label or "",
                "changepoints": len(changes.changepoint_indices),
                "segment_means": " -> ".join(f"{m:.6g}" for m in changes.segment_means),
                "robust_outliers": p.robust_outliers,
                "robust_outlier_fraction": p.robust_outlier_fraction,
                "unavailable_reason": trend.reason or "",
            }
        )
    return pd.DataFrame(rows)


def render_export(profile: DatasetProfile, report: AnalysisReport) -> None:
    """Download controls for the computed report.

    Everything offered here is already computed and on screen — the exports are
    the same numbers, not a re-derivation.
    """
    panel_title("EXPORT RESULTS")
    stem = Path(profile.filename or "dataset").stem[:48] or "dataset"
    stamp = report.computed_at.strftime("%Y%m%dT%H%M%SZ")

    table = _parameter_table(report)
    left, middle, right = st.columns(3)
    with left:
        st.download_button(
            "PARAMETER RESULTS (CSV)",
            data=table.to_csv(index=False),
            file_name=f"{stem}_parameters_{stamp}.csv",
            mime="text/csv",
            disabled=table.empty,
            use_container_width=True,
        )
    with middle:
        st.download_button(
            "FULL REPORT (JSON)",
            data=report.model_dump_json(indent=2),
            file_name=f"{stem}_report_{stamp}.json",
            mime="application/json",
            use_container_width=True,
        )
    with right:
        anomalies = report.anomalies
        anomaly_csv = (
            pd.DataFrame(anomalies.top_anomalies).to_csv(index=False)
            if anomalies.top_anomalies
            else ""
        )
        st.download_button(
            "FLAGGED ANOMALIES (CSV)",
            data=anomaly_csv,
            file_name=f"{stem}_anomalies_{stamp}.csv",
            mime="text/csv",
            disabled=not anomaly_csv,
            use_container_width=True,
        )


def render_summary(
    profile: DatasetProfile, report: AnalysisReport, selected: list[str]
) -> None:
    """Top strip: shape, time base, and headline counts."""
    h1, h2, h3, h4, h5, h6 = st.columns(6)
    with h1:
        st.markdown(
            metric_box("ROWS", f"{profile.rows:,}", f"{profile.columns} columns",
                       accent=PAPER),
            unsafe_allow_html=True,
        )
    with h2:
        st.markdown(
            metric_box(
                "PARAMETERS", str(len(selected)),
                f"{len(profile.numeric_columns)} numeric detected", accent=PAPER,
            ),
            unsafe_allow_html=True,
        )
    with h3:
        st.markdown(
            metric_box(
                "TIME SPAN",
                f"{profile.span_days:,.0f} d" if profile.span_days is not None else "N/A",
                (
                    f"{profile.time_start:%Y-%m-%d} to {profile.time_end:%Y-%m-%d}"
                    if profile.time_start and profile.time_end
                    else "no timestamp column"
                ),
                accent=PAPER if profile.span_days else MIST,
            ),
            unsafe_allow_html=True,
        )
    with h4:
        st.markdown(
            metric_box(
                "SAMPLING",
                f"{profile.median_interval_hours:,.2f} h"
                if profile.median_interval_hours else "--",
                f"largest gap {profile.largest_gap_hours:,.1f} h"
                if profile.largest_gap_hours else "interval undetermined",
                accent=PAPER,
            ),
            unsafe_allow_html=True,
        )
    with h5:
        trending = sum(1 for p in report.parameters if p.trend.significant)
        st.markdown(
            metric_box(
                "SIGNIFICANT TRENDS", f"{trending}/{len(report.parameters)}",
                "Mann-Kendall p<0.05, autocorr-adjusted",
                accent=SIGNAL if trending else PAPER,
            ),
            unsafe_allow_html=True,
        )
    with h6:
        anomalies = report.anomalies
        st.markdown(
            metric_box(
                "ANOMALIES",
                f"{anomalies.n_anomalies:,}" if anomalies.available else "N/A",
                (
                    f"{anomalies.fraction:.1%} of {anomalies.samples_used:,} rows"
                    if anomalies.available
                    else (anomalies.reason or "unavailable")[:44]
                ),
                accent=SIGNAL if anomalies.n_anomalies else PAPER,
            ),
            unsafe_allow_html=True,
        )

    if profile.warnings or report.warnings:
        panel_title("DATA QUALITY NOTES")
        st.markdown(
            kv_rows([("!", w, "w") for w in (*profile.warnings, *report.warnings)][:10]),
            unsafe_allow_html=True,
        )


def render_parameter(
    frame: pd.DataFrame,
    profile: DatasetProfile,
    report: AnalysisReport,
    focus: str,
    times_series: pd.Series | None,
) -> None:
    """Series chart with fitted trend, regime shifts and outliers, plus statistics."""
    analysis = report.parameter(focus)
    if analysis is None:
        st.markdown(kv_rows([("parameter", f"{focus} not analysed", "e")]),
                    unsafe_allow_html=True)
        return

    chart_col, stat_col = st.columns([0.60, 0.40])

    with chart_col:
        values = pd.to_numeric(frame[focus], errors="coerce")
        plot = pd.DataFrame(
            {
                "x": times_series if times_series is not None else np.arange(len(frame)),
                "value": values,
            }
        ).dropna(subset=["value"])

        if plot.empty:
            st.markdown(kv_rows([("series", "no finite values to plot", "e")]),
                        unsafe_allow_html=True)
        else:
            temporal = times_series is not None
            x_kind = "x:T" if temporal else "x:Q"
            layers = (
                alt.Chart(plot)
                .mark_line(strokeWidth=1.2, color=CANOPY)
                .encode(
                    x=alt.X(x_kind, title=None),
                    y=alt.Y("value:Q", title=analysis.unit or focus,
                            scale=alt.Scale(zero=False)),
                    tooltip=[x_kind, "value:Q"],
                )
                .properties(height=210)
            )

            trend = analysis.trend
            if temporal and trend.available and trend.theil_sen_slope is not None:
                x_min, x_max = plot["x"].min(), plot["x"].max()
                span_years = (x_max - x_min).total_seconds() / (365.25 * 86400.0)
                centre = float(plot["value"].median())
                half = trend.theil_sen_slope * span_years / 2.0
                layers = layers + (
                    alt.Chart(
                        pd.DataFrame(
                            {"x": [x_min, x_max], "value": [centre - half, centre + half]}
                        )
                    )
                    .mark_line(color=SIGNAL, strokeDash=[5, 3], strokeWidth=1.5)
                    .encode(x="x:T", y="value:Q")
                )

            changepoints = analysis.changepoints
            if temporal and changepoints.changepoints:
                layers = layers + (
                    alt.Chart(pd.DataFrame({"x": changepoints.changepoints}))
                    .mark_rule(color=SIGNAL, strokeWidth=1.2, strokeDash=[3, 2])
                    .encode(x="x:T")
                )
                # Mean level of each regime segment, drawn between its
                # boundaries so a shift reads as a step, not just a rule.
                if len(changepoints.segment_means) == len(changepoints.changepoints) + 1:
                    bounds = [plot["x"].min(), *changepoints.changepoints, plot["x"].max()]
                    seg_df = pd.DataFrame(
                        {
                            "x": bounds[:-1],
                            "x2": bounds[1:],
                            "value": changepoints.segment_means,
                        }
                    )
                    layers = layers + (
                        alt.Chart(seg_df)
                        .mark_rule(color=PAPER, strokeWidth=1.0, opacity=0.7)
                        .encode(x="x:T", x2="x2:T", y="value:Q")
                    )

            if temporal and analysis.outlier_timestamps:
                merged = pd.DataFrame({"x": analysis.outlier_timestamps}).merge(
                    plot, on="x", how="inner"
                )
                if not merged.empty:
                    layers = layers + (
                        alt.Chart(merged)
                        .mark_point(color=SIGNAL, size=28, filled=True)
                        .encode(x="x:T", y="value:Q")
                    )

            st.altair_chart(style_chart(layers), use_container_width=True)

            # The chart carries up to four mark types in two accent colours,
            # which no single Altair legend expresses cleanly — spell the key
            # out beneath it, showing only the marks actually drawn.
            key_parts = [f'<span style="color:{CANOPY}">━ series</span>']
            if temporal and trend.available and trend.theil_sen_slope is not None:
                key_parts.append(f'<span style="color:{SIGNAL}">╌ trend</span>')
            if temporal and analysis.changepoints.changepoints:
                key_parts.append(
                    f'<span style="color:{SIGNAL}">┊ regime shift</span>'
                )
                if analysis.changepoints.segment_means:
                    key_parts.append(
                        f'<span style="color:{PAPER}">─ segment mean</span>'
                    )
            if temporal and analysis.outlier_timestamps:
                key_parts.append(f'<span style="color:{SIGNAL}">● outlier</span>')
            st.markdown(
                '<div style="font-size:10px;color:' + MIST + ';display:flex;'
                'gap:16px;flex-wrap:wrap;margin:-6px 0 4px 2px">'
                + "".join(key_parts)
                + "</div>",
                unsafe_allow_html=True,
            )

    with stat_col:
        trend = analysis.trend
        rows: list[tuple[str, str, str]] = [
            ("observations", f"{analysis.n:,}", "v"),
            ("mean / median",
             f"{fmt(analysis.mean, '.4g')} / {fmt(analysis.median, '.4g')}", "v"),
            ("std dev", fmt(analysis.std, ".4g"), "v"),
            ("range",
             f"{fmt(analysis.minimum, '.4g')} to {fmt(analysis.maximum, '.4g')}", "v"),
        ]
        if analysis.unit:
            rows.append(("unit (assumed)", analysis.unit, "c"))

        if trend.available:
            unit_label = _slope_units(analysis.unit, trend.per)
            rows.extend(
                [
                    ("trend direction", trend.direction.upper(),
                     "w" if trend.significant else "v"),
                    ("Theil-Sen slope",
                     f"{fmt(trend.theil_sen_slope, '+.5g')} {unit_label}", "c"),
                    ("  95% CI",
                     f"[{fmt(trend.theil_sen_low, '+.5g')}, "
                     f"{fmt(trend.theil_sen_high, '+.5g')}]", "v"),
                    ("OLS slope", f"{fmt(trend.ols_slope, '+.5g')} {unit_label}", "v"),
                    ("  std error", fmt(trend.ols_stderr, ".3g"), "v"),
                    ("  p", fmt(trend.ols_p, ".3g"), "v"),
                    ("  r squared", fmt(trend.ols_r2, ".4f"), "v"),
                    ("Mann-Kendall tau", fmt(trend.mk_tau, ".4f"), "v"),
                    ("  p (raw)", fmt(trend.mk_p, ".3g"), "v"),
                    ("  p (autocorr-adjusted)", fmt(trend.mk_p_adjusted, ".3g"),
                     "w" if trend.significant else "v"),
                    ("lag-1 autocorrelation", fmt(trend.lag1_autocorrelation, ".3f"), "v"),
                    ("effective sample size", fmt(trend.effective_n, ".0f"), "v"),
                    ("seasonally adjusted",
                     "YES" if trend.seasonally_adjusted else "NO",
                     "c" if trend.seasonally_adjusted else "v"),
                    ("unadjusted OLS slope", fmt(trend.raw_ols_slope, "+.5g"), "v"),
                    ("total change over record", fmt(trend.total_change, "+.5g"), "v"),
                ]
            )
        else:
            rows.append(("trend", trend.reason or "unavailable", "e"))

        season = analysis.seasonality
        if season.available:
            rows.extend(
                [
                    ("periodicity", "DETECTED" if season.detected else "none resolved",
                     "c" if season.detected else "v"),
                    *(
                        [("  cycle identified", season.period_label, "c")]
                        if season.period_label
                        else []
                    ),
                    ("dominant period", f"{fmt(season.dominant_period_days, '.2f')} d", "v"),
                    ("  period used", f"{fmt(season.period_used_days, '.2f')} d", "v"),
                    ("  ACF peak / threshold",
                     f"{fmt(season.acf_peak, '.3f')} / "
                     f"{fmt(season.significance_threshold, '.3f')}", "v"),
                    ("  cycles observed", fmt(season.cycles_observed, ".1f"), "v"),
                ]
            )
        else:
            rows.append(("periodicity", season.reason or "unavailable", "e"))

        rows.extend(
            [
                ("regime shifts",
                 str(len(analysis.changepoints.changepoint_indices))
                 if analysis.changepoints.available
                 else (analysis.changepoints.reason or "unavailable"),
                 "w" if analysis.changepoints.changepoint_indices else "v"),
                *(
                    [("  segment means",
                      " -> ".join(f"{m:.4g}" for m in analysis.changepoints.segment_means),
                      "v")]
                    if analysis.changepoints.segment_means
                    else []
                ),
                ("robust outliers",
                 f"{analysis.robust_outliers:,} ({analysis.robust_outlier_fraction:.2%})",
                 "w" if analysis.robust_outliers else "v"),
            ]
        )
        st.markdown(kv_rows(rows), unsafe_allow_html=True)

        notes = [*trend.notes, *season.notes, *analysis.changepoints.notes]
        if notes:
            st.markdown(kv_rows([("note", n, "w") for n in notes[:6]]),
                        unsafe_allow_html=True)


def render_structure(report: AnalysisReport) -> None:
    """Correlation heat-map, PCA scree and k-means regime summary."""
    corr_col, pca_col = st.columns(2)

    with corr_col:
        panel_title("CORRELATION STRUCTURE")
        correlation = report.correlation
        if not correlation.available:
            st.markdown(
                kv_rows([("correlation", correlation.reason or "unavailable", "e")]),
                unsafe_allow_html=True,
            )
        else:
            cells = [
                {"a": a, "b": b, "rho": correlation.spearman[i][j]}
                for i, a in enumerate(correlation.columns)
                for j, b in enumerate(correlation.columns)
                if correlation.spearman
            ]
            heat_df = pd.DataFrame(cells).dropna(subset=["rho"]) if cells else pd.DataFrame()
            if not heat_df.empty:
                # Height must scale with the matrix, not be fixed: at a fixed
                # 190px an 8-parameter matrix collapses into hairline stripes
                # and Vega silently drops most of the y-axis labels. ~26px per
                # row keeps every label readable and the cells square-ish.
                n_cols = max(1, len(correlation.columns))
                heat = (
                    alt.Chart(heat_df)
                    .mark_rect(stroke=LINE, strokeWidth=1)
                    .encode(
                        x=alt.X("a:N", title=None,
                                axis=alt.Axis(labelAngle=-45)),
                        y=alt.Y("b:N", title=None,
                                axis=alt.Axis(labelLimit=140)),
                        color=alt.Color(
                            "rho:Q", title="Spearman",
                            # Amber for negative, canopy for positive: the same
                            # two accents used everywhere else.
                            scale=alt.Scale(domain=[-1, 0, 1],
                                            range=[SIGNAL, PANEL, CANOPY]),
                        ),
                        tooltip=["a:N", "b:N", alt.Tooltip("rho:Q", format=".3f")],
                    )
                    .properties(height=max(190, 26 * n_cols))
                )
                st.altair_chart(style_chart(heat), use_container_width=True)

            st.markdown(
                kv_rows(
                    [
                        (
                            f"{p['a']} ~ {p['b']}",
                            f"rho {p['spearman']:+.3f}  r {fmt(p['pearson'], '+.3f')}  "
                            f"n={p['n']}  p={fmt(p['p'], '.2g')}"
                            + ("  SIGNIFICANT" if p["significant"] else ""),
                            "c" if p["significant"] else "v",
                        )
                        for p in correlation.strongest[:6]
                    ]
                ),
                unsafe_allow_html=True,
            )

    with pca_col:
        panel_title("PRINCIPAL COMPONENTS & REGIMES")
        pca = report.pca
        if not pca.available:
            st.markdown(kv_rows([("pca", pca.reason or "unavailable", "e")]),
                        unsafe_allow_html=True)
        else:
            variance_df = pd.DataFrame(
                {
                    "component": [f"PC{i + 1}" for i in range(pca.n_components)],
                    "explained": pca.explained_variance_ratio,
                }
            )
            st.altair_chart(
                style_chart(
                    alt.Chart(variance_df)
                    .mark_bar(color=CANOPY, size=18)
                    .encode(
                        x=alt.X("component:N", title=None),
                        y=alt.Y("explained:Q", title="variance explained",
                                axis=alt.Axis(format="%")),
                        tooltip=["component:N", alt.Tooltip("explained:Q", format=".1%")],
                    )
                    .properties(height=150)
                ),
                use_container_width=True,
            )
            st.markdown(
                kv_rows(
                    [
                        ("samples used", f"{pca.samples_used:,}", "v"),
                        ("cumulative variance",
                         "  ".join(f"{v:.1%}" for v in pca.cumulative_variance[:4]), "c"),
                        *[
                            (
                                column,
                                "  ".join(
                                    f"PC{i + 1} {v:+.3f}"
                                    for i, v in enumerate(loads[: min(3, len(loads))])
                                ),
                                "v",
                            )
                            for column, loads in list(pca.loadings.items())[:8]
                        ],
                    ]
                ),
                unsafe_allow_html=True,
            )

        clusters = report.clusters
        if clusters.available:
            if clusters.labels:
                # Regime membership over sample order: where the dataset sits
                # in each k-means regime and when it switches. Sample order is
                # used (not timestamps) because labels are aligned to the rows
                # that survived the complete-case filter.
                regime_df = pd.DataFrame(
                    {
                        "sample": np.arange(len(clusters.labels)),
                        "regime": [f"R{label + 1}" for label in clusters.labels],
                    }
                )
                # One lane per regime, so membership is separable by *position*
                # and not by colour alone — CANOPY and MIST sit too close to
                # carry the distinction by themselves. Colour is kept as a
                # redundant cue, reordered so the regimes seen first (k is
                # usually 2-3) get the most distinct hues.
                n_regimes = max(1, len(set(clusters.labels)))
                # Its own titled block: the bordered heading separates the strip
                # from the PCA loadings above it (they collided without it) and
                # names what R1..Rn are, which the lanes alone do not say.
                panel_title("REGIME MEMBERSHIP OVER SAMPLE ORDER")
                strip = (
                    alt.Chart(regime_df)
                    .mark_tick(thickness=2, size=14)
                    .encode(
                        x=alt.X("sample:Q", title="sample order"),
                        y=alt.Y("regime:N", title=None, sort="ascending",
                                # Every lane must be labelled; Vega silently
                                # drops alternating labels when bands are tight.
                                axis=alt.Axis(labelOverlap=False, labelPadding=6)),
                        color=alt.Color(
                            "regime:N", title="regime",
                            scale=alt.Scale(range=[SIGNAL, CANOPY, PAPER, MIST]),
                            legend=None,
                        ),
                        tooltip=["sample:Q", "regime:N"],
                    )
                    .properties(height=26 * n_regimes + 30)
                )
                st.altair_chart(style_chart(strip), use_container_width=True)
            st.markdown(
                kv_rows(
                    [
                        ("k-means regimes",
                         f"k={clusters.k} (silhouette {clusters.silhouette:.3f})", "c"),
                        ("cluster sizes", ", ".join(f"{s:,}" for s in clusters.sizes), "v"),
                        ("k candidates",
                         "  ".join(
                             f"k{k}={v:.3f}" for k, v in sorted(clusters.candidates.items())
                         ), "v"),
                        *[
                            (f"  median {column}", "  ".join(f"{v:.4g}" for v in values), "v")
                            for column, values in list(clusters.centroids.items())[:6]
                        ],
                    ]
                ),
                unsafe_allow_html=True,
            )
        else:
            st.markdown(kv_rows([("clusters", clusters.reason or "unavailable", "e")]),
                        unsafe_allow_html=True)


def render_anomalies(report: AnalysisReport) -> None:
    panel_title("MOST ANOMALOUS OBSERVATIONS — ISOLATION FOREST")
    anomalies = report.anomalies
    if not anomalies.available:
        st.markdown(kv_rows([("anomalies", anomalies.reason or "unavailable", "e")]),
                    unsafe_allow_html=True)
        return
    if not anomalies.top_anomalies:
        st.markdown(kv_rows([("anomalies", "none isolated", "v")]), unsafe_allow_html=True)
        return

    table = pd.DataFrame(anomalies.top_anomalies)
    ordered = [c for c in ("row", "timestamp", "score", "flagged") if c in table.columns]
    ordered += [c for c in table.columns if c not in ordered]
    st.dataframe(table[ordered], use_container_width=True, height=260, hide_index=True)

    if anomalies.scores and len(anomalies.flagged_mask) == len(anomalies.scores):
        # Full score distribution, not just the top rows: shows whether the
        # flagged set is a distinct tail or an arbitrary cut through the bulk.
        # Pair each score with flagged_mask, NOT flagged_indices — the latter
        # indexes the original frame, so positions diverge as soon as the
        # complete-case filter drops a row.
        score_df = pd.DataFrame({"score": anomalies.scores})
        score_df["status"] = [
            "flagged" if f else "normal" for f in anomalies.flagged_mask
        ]
        hist = (
            alt.Chart(score_df)
            .mark_bar()
            .encode(
                x=alt.X("score:Q", bin=alt.Bin(maxbins=40), title="isolation score"),
                y=alt.Y("count():Q", title="rows"),
                color=alt.Color(
                    "status:N", title=None,
                    scale=alt.Scale(domain=["normal", "flagged"],
                                    range=[CANOPY, SIGNAL]),
                ),
            )
            .properties(height=120)
        )
        st.altair_chart(style_chart(hist), use_container_width=True)
    st.markdown(
        kv_rows(
            [
                ("method", anomalies.method, "v"),
                ("flagged",
                 f"{anomalies.n_anomalies:,} of {anomalies.samples_used:,} "
                 f"({anomalies.fraction:.2%})", "w"),
                ("interpretation",
                 "lower score = more isolated from the bulk of the data; flagged rows "
                 "are those the forest separates fastest", "v"),
            ]
        ),
        unsafe_allow_html=True,
    )


def render_inventory(profile: DatasetProfile) -> None:
    panel_title("COLUMN INVENTORY")
    inventory = pd.DataFrame(
        [
            {
                "column": c.name,
                "role": c.role,
                "dtype": c.dtype,
                "non_null": c.non_null,
                "null_%": round(c.null_fraction * 100, 2),
                "parameter": c.canonical_parameter or "",
                "unit": c.unit or "",
                "min": c.minimum,
                "median": c.median,
                "max": c.maximum,
            }
            for c in profile.column_profiles
        ]
    )
    st.dataframe(
        inventory, use_container_width=True,
        height=min(320, 40 + 28 * max(1, len(inventory))), hide_index=True,
    )


def render_footer(profile: DatasetProfile, report: AnalysisReport) -> None:
    st.markdown(
        f'<div class="mx-footnote" style="padding-top:6px">'
        f"ANALYSIS: Theil-Sen + Mann-Kendall (serial-correlation adjusted) · "
        f"autocorrelation periodicity with seasonal adjustment · binary-segmentation "
        f"changepoints · IsolationForest · PCA · k-means (silhouette-selected k). "
        f"Computed in {report.duration_ms:,.0f} ms over {profile.rows:,} rows. "
        f"File parsed in session memory and not transmitted anywhere."
        f"</div>",
        unsafe_allow_html=True,
    )
