"""Generate validation fixtures with KNOWN ground truth.

These fixtures are how the analysis engine's answers are *checked*, not merely
eyeballed. The known-truth CSV planted a +0.30 degC/year trend under a strong
annual cycle; a naive linear fit read it as -0.72 (the seasonal-confounding
bug). The regression test asserts the corrected engine recovers +0.30, so this
generator and the CSVs it produces must stay in version control — losing them
loses the guard.

The generator is deterministic (fixed seed), so the committed CSVs and a fresh
run are identical. Run from the project root:

    python tests/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "fixtures"

# --- ground truth, asserted by test_ml_analysis.py -------------------------- #
TRUE_TEMP_TREND = 0.30      # degC / year
TRUE_PH_TREND = -0.02       # /year
TRUE_PERIOD = 365.25        # days
STEP_ROW = 700
STEP_SIZE = 1.5             # PSU
SPIKE_ROWS = np.array([50, 130, 260, 315, 400, 480, 555, 610, 720, 800, 875, 940, 1010, 1070])


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)

    n = 1095  # 3 years, daily
    t = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    years = np.arange(n) / 365.25

    seasonal = 5.0 * np.sin(2 * np.pi * np.arange(n) / TRUE_PERIOD)
    temp = 15.0 + TRUE_TEMP_TREND * years + seasonal + rng.normal(0, 0.35, n)
    temp[SPIKE_ROWS] += rng.choice([-1, 1], SPIKE_ROWS.size) * rng.uniform(4.5, 7.0, SPIKE_ROWS.size)

    ph = 8.10 + TRUE_PH_TREND * years - 0.010 * (temp - temp.mean()) + rng.normal(0, 0.020, n)
    do = 9.0 - 0.22 * (temp - temp.mean()) + rng.normal(0, 0.25, n)

    salinity = np.full(n, 33.5) + rng.normal(0, 0.12, n)
    salinity[STEP_ROW:] += STEP_SIZE

    frame = pd.DataFrame(
        {
            "timestamp": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "temperature_c": np.round(temp, 3),
            "ph": np.round(ph, 4),
            "do_mg_l": np.round(do, 3),
            "salinity_psu": np.round(salinity, 3),
            "site": "STATION-A",
        }
    )
    frame.loc[rng.choice(n, 40, replace=False), "do_mg_l"] = np.nan  # sensor dropout
    frame.to_csv(OUT / "fixture_known.csv", index=False)

    # Edge cases.
    pd.DataFrame(
        {"reading": [1.0, 2.0, 3.0, 4.0], "constant": [5, 5, 5, 5], "blank": [None] * 4}
    ).to_csv(OUT / "fixture_tiny.csv", index=False)

    pd.DataFrame(
        {"a": rng.normal(10, 1, 60), "b": rng.normal(3, 0.4, 60), "c": np.full(60, 7.0)}
    ).to_csv(OUT / "fixture_notime.csv", index=False)

    # Non-marine columns: proves the Data Lab is not restricted to known parameters.
    m = 300
    tm = pd.date_range("2024-01-01", periods=m, freq="h", tz="UTC")
    pd.DataFrame(
        {
            "timestamp": tm.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reactor_pressure_kpa": 101.0 + 0.01 * np.arange(m) + rng.normal(0, 0.5, m),
            "widget_throughput": 50 + 8 * np.sin(2 * np.pi * np.arange(m) / 24)
            + rng.normal(0, 1, m),
            "gizmo_index": rng.normal(7, 0.3, m),
        }
    ).to_csv(OUT / "fixture_nonmarine.csv", index=False)

    # Semicolon-delimited export, to exercise the delimiter sniffer.
    frame.head(200).to_csv(OUT / "fixture_semicolon.csv", index=False, sep=";")

    print(f"fixtures written to {OUT}")
    print(f"  temp trend {TRUE_TEMP_TREND:+.3f} degC/yr · ph trend {TRUE_PH_TREND:+.3f}/yr")
    print(f"  period {TRUE_PERIOD} d · step +{STEP_SIZE} PSU at row {STEP_ROW} · "
          f"{SPIKE_ROWS.size} spikes")


if __name__ == "__main__":
    build()
