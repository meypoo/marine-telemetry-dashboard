"""Data Lab page — upload your own observations and have them analysed.

Accepts a CSV/TSV/Excel table of water-quality readings, infers its time base
and parameter columns, then reports trends, periodicity, regime shifts,
anomalies and covariance structure via ``ml_analysis``.

This module handles input and controls only; everything downstream of a
computed report is rendered by ``lab_render``. The reporting rule matches the
Live Index: a result is shown when the data supports it, and is replaced by the
reason it could not be computed when it does not.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from lab_render import (
    render_anomalies,
    render_export,
    render_footer,
    render_inventory,
    render_parameter,
    render_structure,
    render_summary,
)
from ml_analysis import AnalysisReport, analyze_dataset, profile_dataset, read_uploaded
from ui import SIGNAL, command_bar, kv_rows, metric_box, panel_title, safe_page_link

nav_col, _ = st.columns([0.16, 0.84])
with nav_col:
    safe_page_link("page_live.py", "← LIVE INDEX")

command_bar("DATA LAB — UPLOAD & PATTERN ANALYSIS", "PARSED LOCALLY · NOT TRANSMITTED")

upload_col, ts_col = st.columns([0.62, 0.38])
with upload_col:
    uploaded = st.file_uploader(
        "OBSERVATION FILE",
        type=["csv", "tsv", "txt", "xlsx", "xls"],
        label_visibility="collapsed",
        help=(
            "A table of readings: one timestamp column plus one or more numeric "
            "parameters (temperature, pH, salinity, dissolved oxygen, ...). "
            "Comma, tab and semicolon delimiters are detected automatically."
        ),
    )

if uploaded is None:
    st.markdown(
        kv_rows(
            [
                ("expected shape", "one row per observation", "c"),
                ("timestamp column", "auto-detected; any ISO-like format", "v"),
                ("parameter columns", "any numeric columns are analysed", "v"),
                ("recognised names",
                 "temperature, pH, salinity, dissolved oxygen, turbidity, "
                 "chlorophyll, conductivity, nitrate, phosphate, depth, waves", "v"),
                ("row limit", "500,000", "v"),
                ("privacy",
                 "parsed in this session's memory; never sent to any external service",
                 "c"),
            ]
        ),
        unsafe_allow_html=True,
    )
    panel_title("WHAT GETS COMPUTED")
    st.markdown(
        kv_rows(
            [
                ("trend", "Theil-Sen slope with 95% CI, OLS for comparison, and a "
                          "Mann-Kendall test adjusted for serial correlation", "v"),
                ("seasonality", "autocorrelation beyond the first zero crossing; a "
                                "detected cycle is removed before the trend is fitted", "v"),
                ("regime shifts", "binary segmentation on mean level with a "
                                  "BIC-style acceptance penalty", "v"),
                ("anomalies", "Isolation Forest across all parameters jointly, plus a "
                              "MAD-based robust z-score against the local level", "v"),
                ("structure", "Pearson/Spearman correlation, PCA, and k-means regimes "
                              "with k chosen by silhouette score", "v"),
            ]
        ),
        unsafe_allow_html=True,
    )
    st.stop()

# --------------------------------------------------------------------------- #
# Parse
# --------------------------------------------------------------------------- #
payload = uploaded.getvalue()


@st.cache_data(show_spinner=False, max_entries=8)
def _parse(name: str, blob: bytes) -> pd.DataFrame:
    return read_uploaded(name, blob)


try:
    frame = _parse(uploaded.name, payload)
except ValueError as exc:
    st.markdown(
        metric_box("FILE REJECTED", "PARSE ERROR", str(exc)[:300], accent=SIGNAL, border=SIGNAL),
        unsafe_allow_html=True,
    )
    st.stop()

base_profile = profile_dataset(frame, uploaded.name)

with ts_col:
    candidates = [
        c.name for c in base_profile.column_profiles
        if c.role in {"timestamp", "categorical"}
    ] or list(frame.columns)
    ts_choice = st.selectbox(
        "TIMESTAMP COLUMN", options=["(auto-detect)"] + candidates,
        index=0, label_visibility="collapsed",
    )

profile = (
    base_profile
    if ts_choice == "(auto-detect)"
    else profile_dataset(frame, uploaded.name, timestamp_override=ts_choice)
)

if not profile.numeric_columns:
    st.markdown(
        metric_box(
            "NOTHING TO ANALYSE", "NO NUMERIC COLUMNS",
            "the file parsed, but no column held enough numeric values",
            accent=SIGNAL, border=SIGNAL,
        ),
        unsafe_allow_html=True,
    )
    render_inventory(profile)
    st.stop()

sel_col, sens_col = st.columns([0.72, 0.28])
with sel_col:
    selected = st.multiselect(
        "PARAMETERS", options=profile.numeric_columns,
        default=profile.numeric_columns[:8], label_visibility="collapsed",
    )
with sens_col:
    sensitivity = st.select_slider(
        "ANOMALY SENSITIVITY", options=["auto", "1%", "2%", "5%", "10%"],
        value="auto", label_visibility="collapsed",
    )

if not selected:
    st.markdown(kv_rows([("!", "select at least one parameter", "w")]),
                unsafe_allow_html=True)
    st.stop()

contamination: float | str = (
    "auto" if sensitivity == "auto" else float(sensitivity.rstrip("%")) / 100.0
)


@st.cache_data(show_spinner=False, max_entries=8)
def _analyze(
    _frame: pd.DataFrame,
    _profile,
    cache_key: tuple[str, int, tuple[str, ...], str | None, float | str],
) -> AnalysisReport:
    """Cached analysis. Leading underscores keep the unhashable frame/profile out
    of the cache key; ``cache_key`` carries everything that actually varies."""
    return analyze_dataset(
        _frame, _profile, selected_columns=list(cache_key[2]), contamination=cache_key[4]
    )


with st.spinner("fitting trends, detecting cycles, isolating anomalies ..."):
    report = _analyze(
        frame,
        profile,
        (uploaded.name, len(payload), tuple(selected), profile.timestamp_column, contamination),
    )

# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
render_summary(profile, report, selected)

panel_title("PARAMETER ANALYSIS")
focus = st.selectbox("FOCUS PARAMETER", options=selected, index=0,
                     label_visibility="collapsed")

times_series: pd.Series | None = None
if profile.timestamp_column and profile.timestamp_column in frame.columns:
    parsed = pd.to_datetime(
        frame[profile.timestamp_column], errors="coerce", utc=True, format="mixed"
    )
    if parsed.notna().any():
        times_series = parsed

render_parameter(frame, profile, report, focus, times_series)
render_structure(report)
render_anomalies(report)
render_inventory(profile)
render_export(profile, report)
render_footer(profile, report)
