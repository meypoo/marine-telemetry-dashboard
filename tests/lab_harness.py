"""Streamlit script that drives the Data Lab rendering with a real fixture.

AppTest cannot drive st.file_uploader, so this bypasses it: it reads a fixture
from LAB_FIXTURE, computes a report, and calls every render_* function. Run
under AppTest (see test_app.py), not directly.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from lab_render import (
    render_anomalies, render_export, render_footer, render_inventory,
    render_parameter, render_structure, render_summary,
)
from ml_analysis import analyze_dataset, profile_dataset, read_uploaded
from ui import TerminalConfig, inject_base_css, panel_title

inject_base_css(TerminalConfig())

path = os.environ["LAB_FIXTURE"]
focus_env = os.environ.get("LAB_FOCUS", "")

with open(path, "rb") as handle:
    frame = read_uploaded(path, handle.read())

profile = profile_dataset(frame, path)
selected = profile.numeric_columns[:8]
report = analyze_dataset(frame, profile, selected_columns=selected)

render_summary(profile, report, selected)

panel_title("PARAMETER ANALYSIS")
focus = focus_env if focus_env in selected else (selected[0] if selected else None)

times_series = None
if profile.timestamp_column and profile.timestamp_column in frame.columns:
    parsed = pd.to_datetime(
        frame[profile.timestamp_column], errors="coerce", utc=True, format="mixed"
    )
    if parsed.notna().any():
        times_series = parsed

if focus:
    render_parameter(frame, profile, report, focus, times_series)
render_structure(report)
render_anomalies(report)
render_inventory(profile)
render_export(profile, report)
render_footer(profile, report)

st.markdown("HARNESS-COMPLETE")
