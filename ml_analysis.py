"""Pattern, trend and anomaly analysis for user-supplied water-quality series.

This module is the analytical half of the Data Lab. It takes an uploaded table
of observations (temperature, pH, salinity, dissolved oxygen, ...) and returns a
structured report describing what is actually in the data: monotonic trends with
their uncertainty, periodic structure, anomalous observations, abrupt regime
shifts, and the covariance structure between parameters.

It follows the same evidential rule as the rest of the project: a result is
either supported by the data or explicitly absent. Every test carries its
sample size and significance, tests that cannot be run on the supplied series
report *why*, and nothing is extrapolated or imputed into the output.

Two properties of environmental monitoring data drive the method choices:

* **Outliers are routine.** Sensors foul, drift and drop out. Trends are
  therefore estimated with Theil-Sen (median of pairwise slopes, ~29 % breakdown
  point) and tested with Mann-Kendall, both rank-based, alongside ordinary least
  squares for comparison. Where OLS and Theil-Sen disagree materially, that
  disagreement is reported rather than hidden.
* **Observations are serially correlated.** Consecutive readings are not
  independent, so Mann-Kendall p-values are optimistic. Effective sample size is
  estimated from lag-1 autocorrelation and the p-value is adjusted; both the raw
  and adjusted values are reported.

The module has no Streamlit dependency and can be exercised directly:

    python ml_analysis.py path/to/observations.csv
"""

from __future__ import annotations

import io
import math
import re
from datetime import datetime, timezone
from typing import Any, Final, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from scipy import signal, stats
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

__all__ = [
    "read_uploaded",
    "profile_dataset",
    "analyze_dataset",
    "AnalysisReport",
    "DatasetProfile",
    "ParameterAnalysis",
    "PARAMETER_HINTS",
]

# --------------------------------------------------------------------------- #
# Limits and thresholds
# --------------------------------------------------------------------------- #
MAX_ROWS: Final[int] = 500_000
#: Hard byte cap enforced before parsing, so an oversized upload is rejected
#: without first being materialised into memory.
MAX_UPLOAD_BYTES: Final[int] = 75 * 1_048_576  # 75 MB
MIN_POINTS_TREND: Final[int] = 8
MIN_POINTS_SEASONALITY: Final[int] = 24
MIN_POINTS_MULTIVARIATE: Final[int] = 20
MIN_SEGMENT: Final[int] = 8
ALPHA: Final[float] = 0.05
#: Iglewicz-Hoaglin cut-off for the modified (MAD-based) z-score.
ROBUST_Z_CUTOFF: Final[float] = 3.5

#: Canonical marine parameters recognised from column names, with the unit the
#: column is *assumed* to already be in (values are never converted).
PARAMETER_HINTS: Final[dict[str, tuple[str, str]]] = {
    r"\b(sea[_\s-]?surface[_\s-]?temp|sst|wtmp|water[_\s-]?temp|temp)\w*": ("temperature", "degC"),
    r"\bp\.?h\b": ("pH", "pH"),
    r"\b(sal|salinity|psu|practical[_\s-]?salinity)\w*": ("salinity", "PSU"),
    r"\b(do|dissolved[_\s-]?oxygen|oxygen|o2)\w*": ("dissolved oxygen", "mg/L"),
    r"\b(turb|turbidity|ntu)\w*": ("turbidity", "NTU"),
    r"\b(chl|chlorophyll|chla)\w*": ("chlorophyll-a", "ug/L"),
    r"\b(cond|conductivity)\w*": ("conductivity", "uS/cm"),
    r"\b(nitrate|no3)\w*": ("nitrate", "umol/L"),
    r"\b(phosphate|po4)\w*": ("phosphate", "umol/L"),
    r"\b(ammoni|nh4|nh3)\w*": ("ammonium", "umol/L"),
    r"\b(depth|pres|pressure|dbar)\w*": ("depth/pressure", "m"),
    r"\b(wave|wvht|swh)\w*": ("wave height", "m"),
    r"\b(wind|wspd)\w*": ("wind speed", "m/s"),
}

TIMESTAMP_NAME_HINT: Final[re.Pattern[str]] = re.compile(
    r"(time|date|timestamp|datetime|utc|observed|sampled)", re.IGNORECASE
)


def _canonical_parameter(column: str) -> tuple[str | None, str | None]:
    """Best-effort mapping from a column name to a known parameter and unit."""
    lowered = column.strip().lower().replace("(", " ").replace(")", " ")
    for pattern, (name, unit) in PARAMETER_HINTS.items():
        if re.search(pattern, lowered, re.IGNORECASE):
            return name, unit
    return None, None


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
class ColumnProfile(BaseModel):
    """Per-column summary produced during ingestion."""

    name: str
    dtype: str
    role: Literal["timestamp", "numeric", "categorical", "empty"]
    non_null: int
    null_fraction: float
    canonical_parameter: str | None = None
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    median: float | None = None
    std: float | None = None


class DatasetProfile(BaseModel):
    """Shape, time base and column inventory of an uploaded table."""

    filename: str
    rows: int
    columns: int
    timestamp_column: str | None = None
    numeric_columns: list[str] = Field(default_factory=list)
    time_start: datetime | None = None
    time_end: datetime | None = None
    span_days: float | None = None
    median_interval_hours: float | None = None
    duplicate_timestamps: int = 0
    unsorted_timestamps: bool = False
    largest_gap_hours: float | None = None
    column_profiles: list[ColumnProfile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def read_uploaded(filename: str, payload: bytes) -> pd.DataFrame:
    """Parse an uploaded CSV/TSV/Excel payload into a DataFrame.

    Raises ValueError with an actionable message rather than letting a parser
    exception surface, since the caller renders these directly to the user.
    """
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if not payload:
        raise ValueError("uploaded file is empty")

    # Guard on raw size *before* parsing, so an oversized file is rejected
    # rather than being fully materialised into memory first. Streamlit's
    # maxUploadSize (config.toml) is the outer bound; this is the analysis bound.
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"file is {len(payload) / 1_048_576:.0f} MB; the limit is "
            f"{MAX_UPLOAD_BYTES // 1_048_576} MB. Aggregate or subset before uploading."
        )

    # Read at most MAX_ROWS + 1 so the over-limit check below still fires while
    # never materialising an unbounded number of rows.
    if suffix in {"xlsx", "xls"}:
        try:
            frame = pd.read_excel(io.BytesIO(payload), nrows=MAX_ROWS + 1)
        except ImportError as exc:
            raise ValueError(
                "Excel support needs the 'openpyxl' package (pip install openpyxl); "
                "alternatively export the sheet as CSV"
            ) from exc
        except Exception as exc:
            raise ValueError(f"could not read spreadsheet: {exc}") from exc
    else:
        text = payload.decode("utf-8-sig", errors="replace")
        try:
            # sep=None lets the sniffer handle comma/tab/semicolon exports.
            frame = pd.read_csv(
                io.StringIO(text), sep=None, engine="python", nrows=MAX_ROWS + 1
            )
        except Exception as exc:
            raise ValueError(f"could not parse delimited text: {exc}") from exc

    if frame.empty:
        raise ValueError("file parsed but contains no rows")
    if len(frame) > MAX_ROWS:
        raise ValueError(
            f"file exceeds the {MAX_ROWS:,}-row limit. "
            "Aggregate or subset before uploading."
        )
    frame.columns = [str(c).strip() for c in frame.columns]
    return frame


def _parse_timestamps(series: pd.Series) -> tuple[pd.Series | None, float]:
    """Attempt datetime parsing; return (parsed, success_fraction)."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce", utc=True), 1.0
    if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
        return None, 0.0
    with pd.option_context("mode.chained_assignment", None):
        parsed = pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
    non_null = series.notna().sum()
    if non_null == 0:
        return None, 0.0
    return parsed, float(parsed.notna().sum()) / float(non_null)


def profile_dataset(
    frame: pd.DataFrame,
    filename: str,
    *,
    timestamp_override: str | None = None,
) -> DatasetProfile:
    """Classify columns, locate the time base and summarise data quality."""
    warnings: list[str] = []

    # --- locate the timestamp column ------------------------------------- #
    timestamp_column: str | None = None
    parsed_time: pd.Series | None = None
    if timestamp_override and timestamp_override in frame.columns:
        parsed, ratio = _parse_timestamps(frame[timestamp_override])
        if parsed is not None and ratio > 0.5:
            timestamp_column, parsed_time = timestamp_override, parsed
        else:
            warnings.append(
                f"column '{timestamp_override}' could not be parsed as timestamps"
            )
    if timestamp_column is None:
        best_score = 0.0
        for column in frame.columns:
            parsed, ratio = _parse_timestamps(frame[column])
            if parsed is None or ratio < 0.8:
                continue
            score = ratio + (0.5 if TIMESTAMP_NAME_HINT.search(column) else 0.0)
            if score > best_score:
                best_score, timestamp_column, parsed_time = score, column, parsed

    # --- classify remaining columns --------------------------------------- #
    profiles: list[ColumnProfile] = []
    numeric_columns: list[str] = []
    for column in frame.columns:
        series = frame[column]
        non_null = int(series.notna().sum())
        null_fraction = 1.0 - (non_null / len(frame)) if len(frame) else 1.0

        if column == timestamp_column:
            profiles.append(
                ColumnProfile(
                    name=column, dtype=str(series.dtype), role="timestamp",
                    non_null=non_null, null_fraction=round(null_fraction, 4),
                )
            )
            continue
        if non_null == 0:
            profiles.append(
                ColumnProfile(
                    name=column, dtype=str(series.dtype), role="empty",
                    non_null=0, null_fraction=1.0,
                )
            )
            continue

        numeric = pd.to_numeric(series, errors="coerce")
        if numeric.notna().sum() >= max(3, 0.6 * non_null):
            canonical, unit = _canonical_parameter(column)
            values = numeric.dropna()
            profiles.append(
                ColumnProfile(
                    name=column, dtype=str(series.dtype), role="numeric",
                    non_null=int(numeric.notna().sum()),
                    null_fraction=round(1.0 - numeric.notna().sum() / len(frame), 4),
                    canonical_parameter=canonical, unit=unit,
                    minimum=float(values.min()), maximum=float(values.max()),
                    mean=float(values.mean()), median=float(values.median()),
                    std=float(values.std()) if len(values) > 1 else None,
                )
            )
            numeric_columns.append(column)
        else:
            profiles.append(
                ColumnProfile(
                    name=column, dtype=str(series.dtype), role="categorical",
                    non_null=non_null, null_fraction=round(null_fraction, 4),
                )
            )

    profile = DatasetProfile(
        filename=filename,
        rows=len(frame),
        columns=len(frame.columns),
        timestamp_column=timestamp_column,
        numeric_columns=numeric_columns,
        column_profiles=profiles,
        warnings=warnings,
    )

    # --- time-base quality -------------------------------------------------- #
    if parsed_time is not None:
        valid = parsed_time.dropna()
        if not valid.empty:
            profile.time_start = valid.min().to_pydatetime()
            profile.time_end = valid.max().to_pydatetime()
            profile.span_days = float((valid.max() - valid.min()).total_seconds() / 86400.0)
            profile.duplicate_timestamps = int(valid.duplicated().sum())
            profile.unsorted_timestamps = not valid.is_monotonic_increasing
            deltas = valid.sort_values().diff().dropna()
            if not deltas.empty:
                profile.median_interval_hours = float(
                    deltas.median().total_seconds() / 3600.0
                )
                profile.largest_gap_hours = float(deltas.max().total_seconds() / 3600.0)
        unparsed = int(parsed_time.isna().sum())
        if unparsed:
            profile.warnings.append(f"{unparsed:,} row(s) have an unparseable timestamp")
    else:
        profile.warnings.append(
            "no timestamp column detected — trends will be computed against row "
            "order and seasonality cannot be assessed"
        )
    if not numeric_columns:
        profile.warnings.append("no numeric parameter columns detected")
    return profile


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
class TrendResult(BaseModel):
    available: bool = False
    reason: str | None = None
    n: int = 0
    unit: str | None = None
    per: str = "year"
    ols_slope: float | None = None
    ols_stderr: float | None = None
    ols_r2: float | None = None
    ols_p: float | None = None
    theil_sen_slope: float | None = None
    theil_sen_low: float | None = None
    theil_sen_high: float | None = None
    mk_tau: float | None = None
    mk_p: float | None = None
    mk_p_adjusted: float | None = None
    lag1_autocorrelation: float | None = None
    effective_n: float | None = None
    significant: bool = False
    direction: str = "undetermined"
    total_change: float | None = None
    #: True when the cycle detected by the seasonality step was removed before
    #: fitting. ``raw_ols_slope`` is then the confounded slope, kept for contrast.
    seasonally_adjusted: bool = False
    raw_ols_slope: float | None = None
    notes: list[str] = Field(default_factory=list)


class SeasonalityResult(BaseModel):
    available: bool = False
    reason: str | None = None
    detected: bool = False
    dominant_period_days: float | None = None
    #: ``dominant_period_days`` snapped to a known physical cycle when close;
    #: this is the period actually used for seasonal adjustment.
    period_used_days: float | None = None
    period_label: str | None = None
    acf_peak: float | None = None
    significance_threshold: float | None = None
    cycles_observed: float | None = None
    notes: list[str] = Field(default_factory=list)


class ChangepointResult(BaseModel):
    available: bool = False
    reason: str | None = None
    changepoints: list[datetime] = Field(default_factory=list)
    changepoint_indices: list[int] = Field(default_factory=list)
    segment_means: list[float] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ParameterAnalysis(BaseModel):
    """Everything derived for one uploaded parameter column."""

    column: str
    canonical_parameter: str | None = None
    unit: str | None = None
    n: int = 0
    mean: float | None = None
    median: float | None = None
    std: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    robust_outliers: int = 0
    robust_outlier_fraction: float = 0.0
    outlier_timestamps: list[datetime] = Field(default_factory=list)
    trend: TrendResult = Field(default_factory=TrendResult)
    seasonality: SeasonalityResult = Field(default_factory=SeasonalityResult)
    changepoints: ChangepointResult = Field(default_factory=ChangepointResult)


class CorrelationResult(BaseModel):
    available: bool = False
    reason: str | None = None
    columns: list[str] = Field(default_factory=list)
    pearson: list[list[float | None]] = Field(default_factory=list)
    spearman: list[list[float | None]] = Field(default_factory=list)
    strongest: list[dict[str, Any]] = Field(default_factory=list)


class PCAResult(BaseModel):
    available: bool = False
    reason: str | None = None
    columns: list[str] = Field(default_factory=list)
    explained_variance_ratio: list[float] = Field(default_factory=list)
    cumulative_variance: list[float] = Field(default_factory=list)
    loadings: dict[str, list[float]] = Field(default_factory=dict)
    n_components: int = 0
    samples_used: int = 0


class ClusterResult(BaseModel):
    available: bool = False
    reason: str | None = None
    k: int = 0
    silhouette: float | None = None
    sizes: list[int] = Field(default_factory=list)
    centroids: dict[str, list[float]] = Field(default_factory=dict)
    labels: list[int] = Field(default_factory=list)
    candidates: dict[int, float] = Field(default_factory=dict)
    samples_used: int = 0


class AnomalyResult(BaseModel):
    available: bool = False
    reason: str | None = None
    method: str = "IsolationForest"
    columns: list[str] = Field(default_factory=list)
    n_anomalies: int = 0
    fraction: float = 0.0
    samples_used: int = 0
    scores: list[float] = Field(default_factory=list)
    flagged_indices: list[int] = Field(default_factory=list)
    top_anomalies: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    """Complete Data Lab result for one uploaded dataset."""

    profile: DatasetProfile
    parameters: list[ParameterAnalysis] = Field(default_factory=list)
    correlation: CorrelationResult = Field(default_factory=CorrelationResult)
    pca: PCAResult = Field(default_factory=PCAResult)
    clusters: ClusterResult = Field(default_factory=ClusterResult)
    anomalies: AnomalyResult = Field(default_factory=AnomalyResult)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: float = 0.0
    warnings: list[str] = Field(default_factory=list)

    def parameter(self, column: str) -> ParameterAnalysis | None:
        return next((p for p in self.parameters if p.column == column), None)


# --------------------------------------------------------------------------- #
# Univariate analysis
# --------------------------------------------------------------------------- #
def _lag1_autocorrelation(values: np.ndarray) -> float | None:
    if values.size < 3:
        return None
    centred = values - values.mean()
    denominator = float(np.dot(centred, centred))
    if denominator <= 0:
        return None
    return float(np.dot(centred[:-1], centred[1:]) / denominator)


def _analyze_trend(
    x_years: np.ndarray, values: np.ndarray, unit: str | None, per: str
) -> TrendResult:
    """Estimate and test a monotonic trend.

    Reports OLS and Theil-Sen slopes side by side and tests significance with
    Mann-Kendall, adjusting the p-value for serial correlation via an effective
    sample size. A trend is called significant only when the adjusted
    Mann-Kendall test clears alpha *and* the Theil-Sen confidence interval
    excludes zero.
    """
    result = TrendResult(unit=unit, per=per, n=int(values.size))
    if values.size < MIN_POINTS_TREND:
        result.reason = f"needs at least {MIN_POINTS_TREND} points, got {values.size}"
        return result
    if np.allclose(values, values[0]):
        result.reason = "series is constant"
        return result
    if np.ptp(x_years) <= 0:
        result.reason = "all observations share one timestamp"
        return result

    try:
        regression = stats.linregress(x_years, values)
        result.ols_slope = float(regression.slope)
        result.ols_stderr = float(regression.stderr)
        result.ols_r2 = float(regression.rvalue**2)
        result.ols_p = float(regression.pvalue)
    except Exception as exc:
        result.notes.append(f"OLS failed: {type(exc).__name__}")

    try:
        slope, _, low, high = stats.theilslopes(values, x_years, alpha=0.95)
        result.theil_sen_slope = float(slope)
        result.theil_sen_low = float(low)
        result.theil_sen_high = float(high)
    except Exception as exc:
        result.notes.append(f"Theil-Sen failed: {type(exc).__name__}")

    try:
        tau, p_value = stats.kendalltau(x_years, values)
        result.mk_tau = float(tau) if tau == tau else None
        result.mk_p = float(p_value) if p_value == p_value else None
    except Exception as exc:
        result.notes.append(f"Mann-Kendall failed: {type(exc).__name__}")

    # Serial correlation inflates significance; widen the p-value accordingly.
    rho = _lag1_autocorrelation(values)
    result.lag1_autocorrelation = round(rho, 4) if rho is not None else None
    if rho is not None and -0.999 < rho < 0.999 and result.mk_p is not None:
        effective_n = values.size * (1.0 - rho) / (1.0 + rho)
        effective_n = float(max(3.0, min(float(values.size), effective_n)))
        result.effective_n = round(effective_n, 1)
        if result.mk_tau is not None and effective_n < values.size:
            # Re-test tau at the effective sample size.
            z = 3.0 * result.mk_tau * math.sqrt(effective_n * (effective_n - 1.0)) / math.sqrt(
                2.0 * (2.0 * effective_n + 5.0)
            )
            result.mk_p_adjusted = float(2.0 * (1.0 - stats.norm.cdf(abs(z))))
            if rho > 0.3:
                result.notes.append(
                    f"lag-1 autocorrelation {rho:.2f}: effective sample size "
                    f"{effective_n:.0f} of {values.size}, p-value adjusted upward"
                )
        else:
            result.mk_p_adjusted = result.mk_p
    else:
        result.mk_p_adjusted = result.mk_p

    effective_p = result.mk_p_adjusted if result.mk_p_adjusted is not None else result.mk_p
    ci_excludes_zero = (
        result.theil_sen_low is not None
        and result.theil_sen_high is not None
        and (result.theil_sen_low > 0 or result.theil_sen_high < 0)
    )
    result.significant = bool(
        effective_p is not None and effective_p < ALPHA and ci_excludes_zero
    )

    slope = result.theil_sen_slope if result.theil_sen_slope is not None else result.ols_slope
    if not result.significant:
        result.direction = "no significant trend"
    elif slope is not None and slope > 0:
        result.direction = "increasing"
    elif slope is not None and slope < 0:
        result.direction = "decreasing"

    if slope is not None:
        result.total_change = float(slope * float(np.ptp(x_years)))

    # Flag divergence between the robust and least-squares estimates.
    if (
        result.ols_slope is not None
        and result.theil_sen_slope is not None
        and result.ols_stderr
        and abs(result.ols_slope - result.theil_sen_slope) > 2 * result.ols_stderr
    ):
        result.notes.append(
            "OLS and Theil-Sen slopes differ by more than 2 standard errors — "
            "outliers are influencing the least-squares fit"
        )

    result.available = True
    return result


def _analyze_seasonality(
    times: pd.Series | None, values: np.ndarray, interval_hours: float | None
) -> SeasonalityResult:
    """Detect periodic structure via the autocorrelation of the detrended series."""
    result = SeasonalityResult()
    if times is None:
        result.reason = "no timestamp column; periodicity is undefined"
        return result
    if values.size < MIN_POINTS_SEASONALITY:
        result.reason = f"needs at least {MIN_POINTS_SEASONALITY} points, got {values.size}"
        return result
    if not interval_hours or interval_hours <= 0:
        result.reason = "sampling interval could not be established"
        return result

    try:
        index = np.arange(values.size, dtype=float)
        fit = np.polyfit(index, values, 1)
        residual = values - np.polyval(fit, index)
        if np.allclose(residual, 0):
            result.reason = "series is perfectly linear; no residual structure"
            return result

        centred = residual - residual.mean()
        denominator = float(np.dot(centred, centred))
        if denominator <= 0:
            result.reason = "zero variance after detrending"
            return result

        max_lag = min(values.size // 2, 2000)
        acf = np.array(
            [float(np.dot(centred[:-k], centred[k:]) / denominator) for k in range(1, max_lag)]
        )
        if acf.size < 3:
            result.reason = "series too short for autocorrelation"
            return result

        threshold = 2.0 / math.sqrt(values.size)
        result.significance_threshold = round(threshold, 4)

        # The ACF of any smooth series decays from ~1 at lag 1, so the largest
        # local maximum is otherwise always a few samples in — which reports a
        # "3-day cycle" for what is really an annual one. Genuine periodicity
        # appears as a peak *after* the ACF first crosses zero; search there.
        non_positive = np.flatnonzero(acf <= 0)
        if non_positive.size == 0:
            result.available = True
            result.notes.append(
                "autocorrelation never crosses zero within half the record — the "
                "series is dominated by a trend, or by a period longer than the "
                "record itself"
            )
            return result

        first_zero = int(non_positive[0])
        tail = acf[first_zero:]
        peaks, _ = signal.find_peaks(tail)
        if peaks.size == 0:
            result.available = True
            result.notes.append("no autocorrelation peak beyond the first zero crossing")
            return result

        best = first_zero + int(peaks[int(np.argmax(tail[peaks]))])
        peak_value = float(acf[best])
        period_days = (best + 1) * interval_hours / 24.0

        snapped, label = _snap_period(period_days)
        result.available = True
        result.acf_peak = round(peak_value, 4)
        result.dominant_period_days = round(period_days, 4)
        result.period_used_days = round(snapped, 4)
        result.period_label = label
        result.cycles_observed = round(values.size / (best + 1), 2)
        result.detected = bool(
            peak_value > threshold and result.cycles_observed is not None
            and result.cycles_observed >= 2.0
        )
        if not result.detected:
            if peak_value <= threshold:
                result.notes.append(
                    f"strongest peak {peak_value:.3f} is within the noise band "
                    f"(+/-{threshold:.3f})"
                )
            else:
                result.notes.append(
                    f"only {result.cycles_observed:.1f} cycles observed; at least 2 "
                    "are needed to call a period"
                )
        elif label:
            result.notes.append(
                f"period is consistent with {label}; snapped from "
                f"{period_days:.2f} d to the exact {snapped:.2f} d for adjustment"
            )
    except Exception as exc:
        result.reason = f"{type(exc).__name__}: {exc}"
    return result


def _deseasonalize(
    values: np.ndarray, day_numbers: np.ndarray, period_days: float, n_bins: int = 12
) -> tuple[np.ndarray, int]:
    """Remove a periodic cycle by subtracting phase-bin medians.

    Fitting a straight line through a seasonal series without this step gives a
    slope that is an artefact of where in the cycle the record happens to start
    and end: a clean sine sampled over three cycles carries a linear slope of
    roughly -1 unit/year purely from its own phase. Environmental series are
    seasonal almost by definition, so trends are estimated on the adjusted
    residual and the raw slope is reported alongside for comparison.

    Binning by phase rather than by calendar month keeps this valid for
    diurnal, tidal and annual cycles alike. Returns the adjusted series (with
    the original level restored) and the number of bins actually populated.
    """
    if period_days <= 0 or values.size == 0:
        return values.astype(float).copy(), 0
    phase = np.mod(day_numbers - day_numbers[0], period_days) / period_days
    bins = np.clip((phase * n_bins).astype(int), 0, n_bins - 1)

    adjusted = values.astype(float).copy()
    populated = 0
    for index in range(n_bins):
        selector = bins == index
        if int(selector.sum()) >= 3:
            adjusted[selector] -= float(np.median(values[selector]))
            populated += 1
    if populated:
        adjusted += float(np.median(values))
    return adjusted, populated


#: Physical cycles with exactly known periods, as (days, tolerance, label).
KNOWN_PERIODS: Final[tuple[tuple[float, float, str], ...]] = (
    (0.5, 0.08, "a semi-diurnal tidal cycle"),
    (1.0, 0.12, "a diurnal cycle"),
    (14.765, 1.5, "the spring-neap cycle"),
    (29.531, 3.0, "a lunar month"),
    (365.25, 30.0, "an annual cycle"),
)


def _snap_period(days: float) -> tuple[float, str | None]:
    """Snap a measured period to a known physical cycle when it is close.

    Autocorrelation resolves a period only to the nearest sample, so a yearly
    cycle in daily data lands anywhere in the high 350s to high 360s. Using the
    exact physical period for phase binning avoids drift accumulating across
    the record, which otherwise leaks part of the trend into the seasonal
    adjustment.
    """
    for centre, tolerance, label in KNOWN_PERIODS:
        if abs(days - centre) <= tolerance:
            return centre, label
    return days, None


def _describe_period(days: float) -> str | None:
    return _snap_period(days)[1]


def _find_changepoints(
    values: np.ndarray, times: pd.Series | None, max_changepoints: int = 5
) -> ChangepointResult:
    """Binary segmentation on mean shifts with a BIC-style acceptance penalty."""
    result = ChangepointResult()
    if values.size < 2 * MIN_SEGMENT:
        result.reason = f"needs at least {2 * MIN_SEGMENT} points, got {values.size}"
        return result

    try:
        variance = float(np.var(values))
        if variance <= 0:
            result.reason = "series is constant"
            return result
        penalty = 2.0 * math.log(values.size) * variance

        def best_split(start: int, end: int) -> tuple[int, float] | None:
            segment = values[start:end]
            if segment.size < 2 * MIN_SEGMENT:
                return None
            total_sse = float(((segment - segment.mean()) ** 2).sum())
            cumulative = np.cumsum(segment)
            cumulative_sq = np.cumsum(segment**2)
            best_index, best_gain = -1, 0.0
            for k in range(MIN_SEGMENT, segment.size - MIN_SEGMENT):
                left_n, right_n = k, segment.size - k
                left_sum, right_sum = cumulative[k - 1], cumulative[-1] - cumulative[k - 1]
                left_sq = cumulative_sq[k - 1]
                right_sq = cumulative_sq[-1] - cumulative_sq[k - 1]
                sse = (left_sq - left_sum**2 / left_n) + (right_sq - right_sum**2 / right_n)
                gain = total_sse - sse
                if gain > best_gain:
                    best_index, best_gain = start + k, gain
            if best_index < 0 or best_gain < penalty:
                return None
            return best_index, best_gain

        found: list[int] = []
        queue: list[tuple[int, int]] = [(0, values.size)]
        while queue and len(found) < max_changepoints:
            start, end = queue.pop(0)
            split = best_split(start, end)
            if split is None:
                continue
            index, _ = split
            found.append(index)
            queue.append((start, index))
            queue.append((index, end))

        found.sort()
        result.available = True
        result.changepoint_indices = [int(i) for i in found]
        if times is not None and found:
            stamps = []
            for i in found:
                if 0 <= i < len(times):
                    value = times.iloc[i]
                    stamps.append(
                        value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
                    )
            result.changepoints = stamps

        bounds = [0, *found, values.size]
        result.segment_means = [
            round(float(values[bounds[i] : bounds[i + 1]].mean()), 6)
            for i in range(len(bounds) - 1)
            if bounds[i + 1] > bounds[i]
        ]
        if not found:
            result.notes.append("no mean shift exceeded the acceptance penalty")
    except Exception as exc:
        result.reason = f"{type(exc).__name__}: {exc}"
    return result


def _robust_outliers(values: np.ndarray) -> tuple[np.ndarray, int]:
    """Modified z-score (MAD-based) outlier mask."""
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad <= 0:
        deviation = np.abs(values - median)
        scale = float(deviation.mean())
        if scale <= 0:
            return np.zeros(values.size, dtype=bool), 0
        scores = deviation / (1.253314 * scale)
    else:
        scores = 0.6745 * np.abs(values - median) / mad
    mask = scores > ROBUST_Z_CUTOFF
    return mask, int(mask.sum())


# --------------------------------------------------------------------------- #
# Multivariate analysis
# --------------------------------------------------------------------------- #
def _prepare_matrix(
    frame: pd.DataFrame, columns: list[str]
) -> tuple[np.ndarray | None, np.ndarray | None, str | None]:
    """Impute, standardise and return (scaled, row_positions, reason_if_failed)."""
    if len(columns) < 2:
        return None, None, "needs at least 2 numeric parameters"
    subset = frame[columns].apply(pd.to_numeric, errors="coerce")
    keep = subset.notna().any(axis=1)
    subset = subset[keep]
    if len(subset) < MIN_POINTS_MULTIVARIATE:
        return None, None, (
            f"needs at least {MIN_POINTS_MULTIVARIATE} usable rows, got {len(subset)}"
        )
    positions = np.flatnonzero(keep.to_numpy())
    imputed = SimpleImputer(strategy="median").fit_transform(subset.to_numpy(dtype=float))
    scaled = StandardScaler().fit_transform(imputed)
    if not np.isfinite(scaled).all():
        return None, None, "non-finite values remain after imputation"
    return scaled, positions, None


def _analyze_correlation(frame: pd.DataFrame, columns: list[str]) -> CorrelationResult:
    result = CorrelationResult(columns=columns)
    if len(columns) < 2:
        result.reason = "needs at least 2 numeric parameters"
        return result
    try:
        subset = frame[columns].apply(pd.to_numeric, errors="coerce")
        pearson = subset.corr(method="pearson", min_periods=MIN_POINTS_TREND)
        spearman = subset.corr(method="spearman", min_periods=MIN_POINTS_TREND)

        def to_lists(matrix: pd.DataFrame) -> list[list[float | None]]:
            return [
                [None if pd.isna(v) else round(float(v), 4) for v in row]
                for row in matrix.to_numpy()
            ]

        result.pearson = to_lists(pearson)
        result.spearman = to_lists(spearman)

        pairs: list[dict[str, Any]] = []
        for i, a in enumerate(columns):
            for j, b in enumerate(columns):
                if j <= i:
                    continue
                value = spearman.iloc[i, j]
                if pd.isna(value):
                    continue
                joint = subset[[a, b]].dropna()
                if len(joint) < MIN_POINTS_TREND:
                    continue
                _, p_value = stats.spearmanr(joint[a], joint[b])
                pairs.append(
                    {
                        "a": a, "b": b,
                        "spearman": round(float(value), 4),
                        "pearson": (
                            None if pd.isna(pearson.iloc[i, j])
                            else round(float(pearson.iloc[i, j]), 4)
                        ),
                        "n": int(len(joint)),
                        "p": None if pd.isna(p_value) else float(p_value),
                        "significant": bool(not pd.isna(p_value) and p_value < ALPHA),
                    }
                )
        pairs.sort(key=lambda d: abs(d["spearman"]), reverse=True)
        result.strongest = pairs[:12]
        result.available = True
    except Exception as exc:
        result.reason = f"{type(exc).__name__}: {exc}"
    return result


def _analyze_pca(frame: pd.DataFrame, columns: list[str]) -> PCAResult:
    result = PCAResult(columns=columns)
    scaled, _, reason = _prepare_matrix(frame, columns)
    if scaled is None:
        result.reason = reason
        return result
    try:
        n_components = min(len(columns), scaled.shape[0], 5)
        model = PCA(n_components=n_components, random_state=0).fit(scaled)
        ratios = [round(float(v), 4) for v in model.explained_variance_ratio_]
        result.explained_variance_ratio = ratios
        result.cumulative_variance = [
            round(float(v), 4) for v in np.cumsum(model.explained_variance_ratio_)
        ]
        result.loadings = {
            column: [round(float(model.components_[c][i]), 4) for c in range(n_components)]
            for i, column in enumerate(columns)
        }
        result.n_components = n_components
        result.samples_used = int(scaled.shape[0])
        result.available = True
    except Exception as exc:
        result.reason = f"{type(exc).__name__}: {exc}"
    return result


def _analyze_clusters(frame: pd.DataFrame, columns: list[str]) -> ClusterResult:
    """Select k by silhouette score and describe the resulting regimes."""
    result = ClusterResult()
    scaled, positions, reason = _prepare_matrix(frame, columns)
    if scaled is None:
        result.reason = reason
        return result
    try:
        n_samples = scaled.shape[0]
        max_k = int(min(5, max(2, n_samples // 10)))
        if max_k < 2:
            result.reason = "not enough rows to form two clusters"
            return result

        # Silhouette on a bounded subsample keeps this responsive on large files.
        rng = np.random.default_rng(0)
        if n_samples > 5000:
            sample_idx = rng.choice(n_samples, 5000, replace=False)
        else:
            sample_idx = np.arange(n_samples)

        best_k, best_score, candidates = 0, -1.0, {}
        for k in range(2, max_k + 1):
            model = KMeans(n_clusters=k, n_init=10, random_state=0).fit(scaled)
            try:
                score = float(
                    silhouette_score(scaled[sample_idx], model.labels_[sample_idx])
                )
            except ValueError:
                continue
            candidates[k] = round(score, 4)
            if score > best_score:
                best_k, best_score = k, score
        if best_k < 2:
            result.reason = "silhouette score undefined for all candidate k"
            return result

        final = KMeans(n_clusters=best_k, n_init=10, random_state=0).fit(scaled)
        labels = final.labels_

        # Report centroids in the original units, not standardised space.
        subset = frame[columns].apply(pd.to_numeric, errors="coerce").iloc[positions]
        subset = subset.assign(_cluster=labels)
        centroids = subset.groupby("_cluster")[columns].median()

        result.available = True
        result.k = best_k
        result.silhouette = round(best_score, 4)
        result.candidates = candidates
        result.sizes = [int((labels == c).sum()) for c in range(best_k)]
        result.centroids = {
            column: [
                round(float(centroids.loc[c, column]), 4) if c in centroids.index else float("nan")
                for c in range(best_k)
            ]
            for column in columns
        }
        result.labels = [int(v) for v in labels]
        result.samples_used = int(n_samples)
    except Exception as exc:
        result.reason = f"{type(exc).__name__}: {exc}"
    return result


def _analyze_anomalies(
    frame: pd.DataFrame,
    columns: list[str],
    times: pd.Series | None,
    contamination: float | str = "auto",
) -> AnomalyResult:
    """Unsupervised multivariate outlier detection with an isolation forest.

    ``contamination`` is the expected anomaly fraction. The ``"auto"`` default
    applies the original Isolation Forest offset, which is deliberately
    permissive and routinely flags 10-20 % of correlated environmental records;
    the caller exposes this as a sensitivity control rather than fixing an
    arbitrary rate, since the "right" fraction is a judgement about the data,
    not a property of it.
    """
    result = AnomalyResult(columns=columns)
    scaled, positions, reason = _prepare_matrix(frame, columns)
    if scaled is None:
        result.reason = reason
        return result
    try:
        result.method = f"IsolationForest(contamination={contamination})"
        model = IsolationForest(
            n_estimators=200, contamination=contamination, random_state=0, n_jobs=-1
        ).fit(scaled)
        predictions = model.predict(scaled)
        scores = model.score_samples(scaled)  # lower = more anomalous

        flagged = np.flatnonzero(predictions == -1)
        result.available = True
        result.samples_used = int(scaled.shape[0])
        result.n_anomalies = int(flagged.size)
        result.fraction = round(float(flagged.size) / float(scaled.shape[0]), 4)
        result.scores = [round(float(s), 5) for s in scores]
        result.flagged_indices = [int(positions[i]) for i in flagged]

        order = np.argsort(scores)[: min(15, scores.size)]
        top: list[dict[str, Any]] = []
        for i in order:
            row_position = int(positions[i])
            entry: dict[str, Any] = {
                "row": row_position,
                "score": round(float(scores[i]), 5),
                "flagged": bool(predictions[i] == -1),
            }
            if times is not None and row_position < len(times):
                stamp = times.iloc[row_position]
                if pd.notna(stamp):
                    entry["timestamp"] = (
                        stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
                    )
            for column in columns:
                value = pd.to_numeric(frame[column].iloc[row_position], errors="coerce")
                entry[column] = None if pd.isna(value) else round(float(value), 4)
            top.append(entry)
        result.top_anomalies = top
    except Exception as exc:
        result.reason = f"{type(exc).__name__}: {exc}"
    return result


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def analyze_dataset(
    frame: pd.DataFrame,
    profile: DatasetProfile,
    *,
    selected_columns: list[str] | None = None,
    contamination: float | str = "auto",
) -> AnalysisReport:
    """Run the full battery over the selected numeric columns.

    Each analysis is isolated: a failure in one parameter or one technique is
    recorded against that result and does not prevent the rest of the report
    from being produced.
    """
    import time as _time

    started = _time.perf_counter()
    report = AnalysisReport(profile=profile)
    columns = selected_columns or profile.numeric_columns
    columns = [c for c in columns if c in frame.columns]
    if not columns:
        report.warnings.append("no numeric columns selected for analysis")
        report.duration_ms = (_time.perf_counter() - started) * 1000.0
        return report

    # --- shared time base --------------------------------------------------- #
    times: pd.Series | None = None
    if profile.timestamp_column and profile.timestamp_column in frame.columns:
        parsed, _ = _parse_timestamps(frame[profile.timestamp_column])
        if parsed is not None:
            times = parsed

    order = np.arange(len(frame), dtype=float)
    if times is not None:
        as_seconds = times.astype("int64").to_numpy(dtype=float) / 1e9
        as_seconds[times.isna().to_numpy()] = np.nan
    else:
        as_seconds = None

    lookup = {p.name: p for p in profile.column_profiles}

    for column in columns:
        column_profile = lookup.get(column)
        values_all = pd.to_numeric(frame[column], errors="coerce")
        mask = values_all.notna().to_numpy()
        if as_seconds is not None:
            mask &= np.isfinite(as_seconds)
        values = values_all.to_numpy(dtype=float)[mask]

        analysis = ParameterAnalysis(
            column=column,
            canonical_parameter=column_profile.canonical_parameter if column_profile else None,
            unit=column_profile.unit if column_profile else None,
            n=int(values.size),
        )
        if values.size:
            analysis.mean = float(np.mean(values))
            analysis.median = float(np.median(values))
            analysis.std = float(np.std(values, ddof=1)) if values.size > 1 else None
            analysis.minimum = float(np.min(values))
            analysis.maximum = float(np.max(values))

            masked_times = times[mask] if times is not None else None

            # 1. Establish periodicity first — everything downstream depends on
            #    whether a cycle has to be removed.
            analysis.seasonality = _analyze_seasonality(
                masked_times, values, profile.median_interval_hours
            )

            # 2. Remove that cycle so the trend, level shifts and outliers are
            #    measured against it rather than confounded by it.
            working = values
            adjusted = False
            period_for_adjustment = (
                analysis.seasonality.period_used_days
                or analysis.seasonality.dominant_period_days
            )
            if (
                analysis.seasonality.detected
                and period_for_adjustment
                and as_seconds is not None
            ):
                candidate, populated = _deseasonalize(
                    values, as_seconds[mask] / 86400.0, float(period_for_adjustment)
                )
                if populated >= 4:
                    working, adjusted = candidate, True

            if as_seconds is not None:
                x_units = as_seconds[mask] / (365.25 * 86400.0)
                per = "year"
            else:
                x_units = order[mask]
                per = "sample"

            # 3. Trend on the adjusted series, with the raw slope kept for contrast.
            analysis.trend = _analyze_trend(x_units, working, analysis.unit, per)
            analysis.trend.seasonally_adjusted = adjusted
            if adjusted:
                try:
                    analysis.trend.raw_ols_slope = float(
                        stats.linregress(x_units, values).slope
                    )
                except Exception:
                    analysis.trend.raw_ols_slope = None
                analysis.trend.notes.append(
                    f"a {period_for_adjustment:.2f}-day cycle was removed before "
                    "fitting; the unadjusted slope would be "
                    + (
                        f"{analysis.trend.raw_ols_slope:+.4f} per {per}"
                        if analysis.trend.raw_ols_slope is not None
                        else "unavailable"
                    )
                )

            # 4. Level shifts, also on the adjusted series.
            analysis.changepoints = _find_changepoints(working, masked_times)

            # A step change is monotonic, so a significant trend over a series
            # containing one may describe a level shift rather than gradual drift.
            if analysis.trend.significant and analysis.changepoints.changepoint_indices:
                analysis.trend.notes.append(
                    f"{len(analysis.changepoints.changepoint_indices)} abrupt level "
                    "shift(s) detected — a step change registers as a monotonic "
                    "trend, so read the slope alongside the changepoints"
                )

            # 5. Outliers against the local level, so a regime shift does not
            #    flag half the record as anomalous.
            residual = working.astype(float).copy()
            bounds = [0, *analysis.changepoints.changepoint_indices, working.size]
            for i in range(len(bounds) - 1):
                start, end = bounds[i], bounds[i + 1]
                if end > start:
                    residual[start:end] -= float(np.median(working[start:end]))
            outlier_mask, count = _robust_outliers(residual)
            analysis.robust_outliers = count
            analysis.robust_outlier_fraction = round(count / values.size, 4)
            if times is not None and count:
                stamps = times.to_numpy()[mask][outlier_mask][:25]
                analysis.outlier_timestamps = [
                    pd.Timestamp(s).to_pydatetime() for s in stamps if pd.notna(s)
                ]
        else:
            analysis.trend.reason = "no finite observations"
            analysis.seasonality.reason = "no finite observations"
            analysis.changepoints.reason = "no finite observations"

        report.parameters.append(analysis)

    report.correlation = _analyze_correlation(frame, columns)
    report.pca = _analyze_pca(frame, columns)
    report.clusters = _analyze_clusters(frame, columns)
    report.anomalies = _analyze_anomalies(frame, columns, times, contamination)

    if profile.duplicate_timestamps:
        report.warnings.append(
            f"{profile.duplicate_timestamps:,} duplicate timestamp(s) present; "
            "readings at the same instant are treated as independent samples"
        )
    if profile.unsorted_timestamps:
        report.warnings.append(
            "timestamps are not monotonically increasing; autocorrelation and "
            "changepoint results assume file order"
        )
    if (
        profile.largest_gap_hours
        and profile.median_interval_hours
        and profile.largest_gap_hours > 20 * profile.median_interval_hours
    ):
        report.warnings.append(
            f"largest gap ({profile.largest_gap_hours:,.1f}h) is far longer than the "
            f"median interval ({profile.median_interval_hours:,.2f}h); periodicity "
            "estimates assume even spacing"
        )

    report.duration_ms = (_time.perf_counter() - started) * 1000.0
    return report


if __name__ == "__main__":  # pragma: no cover - manual check
    import sys

    if len(sys.argv) < 2:
        print("usage: python ml_analysis.py <path-to-csv>")
        raise SystemExit(2)

    path = sys.argv[1]
    with open(path, "rb") as handle:
        table = read_uploaded(path, handle.read())
    dataset_profile = profile_dataset(table, path)
    print(f"rows={dataset_profile.rows:,} cols={dataset_profile.columns} "
          f"time={dataset_profile.timestamp_column} "
          f"numeric={dataset_profile.numeric_columns}")
    print(f"span={dataset_profile.span_days} d  "
          f"interval={dataset_profile.median_interval_hours} h")

    result = analyze_dataset(table, dataset_profile)
    for item in result.parameters:
        trend = item.trend
        print(f"\n[{item.column}] n={item.n} mean={item.mean}")
        if trend.available:
            print(f"   trend {trend.direction}: theil-sen {trend.theil_sen_slope:+.5f} "
                  f"[{trend.theil_sen_low:+.5f},{trend.theil_sen_high:+.5f}] per {trend.per}"
                  f"  ols {trend.ols_slope:+.5f}  tau={trend.mk_tau}  "
                  f"p={trend.mk_p:.3g} adj={trend.mk_p_adjusted:.3g}")
        else:
            print(f"   trend unavailable: {trend.reason}")
        season = item.seasonality
        print(f"   seasonality detected={season.detected} "
              f"period={season.dominant_period_days} d acf={season.acf_peak}")
        print(f"   changepoints={len(item.changepoints.changepoint_indices)} "
              f"outliers={item.robust_outliers}")
    print(f"\ncorrelation: {[p['a'] + '~' + p['b'] + '=' + str(p['spearman']) for p in result.correlation.strongest[:5]]}")
    print(f"pca: {result.pca.explained_variance_ratio}")
    print(f"clusters: k={result.clusters.k} silhouette={result.clusters.silhouette} sizes={result.clusters.sizes}")
    print(f"anomalies: {result.anomalies.n_anomalies}/{result.anomalies.samples_used} "
          f"({result.anomalies.fraction:.1%})")
    print(f"duration={result.duration_ms:.0f}ms")
    for warning in result.warnings:
        print(f"WARNING: {warning}")
