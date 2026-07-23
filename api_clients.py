"""Asynchronous, type-hinted HTTP clients for the four live sources behind the
Marine Ecosystem Health Index.

Sources
-------
OBIS          api.obis.org/v3               Biodiversity: occurrence statistics + taxonomic checklist.
Open-Meteo    marine-api.open-meteo.com     Live sea-surface temperature and wave state.
NOAA ERDDAP   coastwatch.pfeg.noaa.gov      NDBC buoys (tabledap) + OISST v2.1 climatology (griddap).
Overpass      overpass-api.de (+ mirrors)   OpenSeaMap maritime infrastructure / traffic separation.

Every outbound request is recorded in a :class:`TelemetrySink` with wall-clock
latency, HTTP status, payload size and attempt count. The dashboard's console
feed renders that sink directly, so what is displayed is measured, not modelled.

Two source constraints discovered by probing the live services shape this module:

* Open-Meteo's marine archive has **no sea-surface temperature before 2023-01-01**
  (earlier dates return well-formed responses whose value arrays are entirely
  null). It is therefore used only for present-day sea state, never for the
  historical baseline.
* NOAA OISST v2.1 (``ncdcOisst21Agg``) carries daily SST back to 1981 and is the
  real source of the ten-year baseline, but it lags roughly two weeks. The
  baseline is consequently built from the ten *completed* prior years, which
  avoids the lag entirely.
"""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Region",
    "REGIONS",
    "REGIONS_BY_CODE",
    "TelemetryEvent",
    "TelemetrySink",
    "ObisSnapshot",
    "SeaStateSnapshot",
    "BuoySnapshot",
    "ClimatologySnapshot",
    "InfrastructureSnapshot",
    "RegionSnapshot",
    "FeedBundle",
    "FEED_NAMES",
    "fetch_feeds",
    "fetch_region_snapshot",
]

USER_AGENT: Final[str] = (
    "MarineEcosystemHealthIndex/1.0 (+https://github.com/; contact via repository)"
)
RETRY_STATUS: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504})
EARTH_RADIUS_KM: Final[float] = 6371.0088


# --------------------------------------------------------------------------- #
# Regions
# --------------------------------------------------------------------------- #
class Region(BaseModel):
    """A study area: a bounding box plus candidate in-situ buoy stations.

    ``buoy_candidates`` is ordered by preference. The ERDDAP client walks the
    list and keeps the first station returning a fresh water-temperature
    reading, which is how the dashboard tolerates individual buoys going dark.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    west: float
    south: float
    east: float
    north: float
    #: Ordered NDBC station IDs to try. Empty means "discover the nearest
    #: stations dynamically" — the path taken for searched locations, where no
    #: curated list exists.
    buoy_candidates: tuple[str, ...] = ()
    #: "curated" for the seven built-in regions, "search" for a synthesized box.
    origin: str = "curated"

    @property
    def centroid(self) -> tuple[float, float]:
        """(latitude, longitude) of the bounding-box centre."""
        return ((self.south + self.north) / 2.0, (self.west + self.east) / 2.0)

    @property
    def filesystem_code(self) -> str:
        """``code`` reduced to characters safe for a cache filename."""
        return re.sub(r"[^A-Za-z0-9._-]", "_", self.code)

    @classmethod
    def from_point(
        cls,
        name: str,
        lat: float,
        lon: float,
        *,
        half_deg: float = 0.35,
        max_area_km2: float = 12_000.0,
        buoy_candidates: tuple[str, ...] = (),
    ) -> "Region":
        """Synthesize a region from a point with a **hard-capped** bounding box.

        The area cap is not optional. Overpass cost scales with bounding-box
        area and was already returning 504s on the small curated boxes, so a
        searched location must never produce an arbitrarily large query. The box
        starts at ``+/- half_deg`` and is shrunk geometrically until its area is
        within ``max_area_km2``.
        """
        lat = max(-89.5, min(89.5, lat))
        # Normalise longitude into [-180, 180).
        lon = ((lon + 180.0) % 360.0) - 180.0

        half = max(0.05, half_deg)
        for _ in range(24):
            south = max(-89.9, lat - half)
            north = min(89.9, lat + half)
            west = max(-180.0, lon - half)
            east = min(180.0, lon + half)
            candidate = cls(
                code=f"@{lat:.3f},{lon:.3f}",
                name=name,
                west=west, south=south, east=east, north=north,
                buoy_candidates=tuple(buoy_candidates),
                origin="search",
            )
            if candidate.area_km2 <= max_area_km2 or half <= 0.05:
                return candidate
            half *= 0.85
        return candidate  # pragma: no cover - loop always returns above

    @property
    def wkt(self) -> str:
        """Counter-clockwise closed WKT polygon, as OBIS expects."""
        w, s, e, n = self.west, self.south, self.east, self.north
        return f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"

    @property
    def area_km2(self) -> float:
        """Spherical area of the bounding box."""
        return abs(
            math.radians(self.east - self.west)
            * EARTH_RADIUS_KM
            * EARTH_RADIUS_KM
            * (math.sin(math.radians(self.north)) - math.sin(math.radians(self.south)))
        )


REGIONS: Final[tuple[Region, ...]] = (
    Region(
        code="MTBY",
        name="Monterey Bay, CA",
        west=-122.30, south=36.50, east=-121.70, north=37.00,
        buoy_candidates=("46042", "46114", "46092", "46012"),
    ),
    Region(
        code="SCAB",
        name="S. California Bight",
        west=-118.60, south=32.80, east=-117.10, north=34.05,
        buoy_candidates=("46025", "46086", "46222", "46221"),
    ),
    Region(
        code="MASS",
        name="Massachusetts Bay",
        west=-70.70, south=42.10, east=-69.80, north=42.90,
        buoy_candidates=("44013", "44029", "44018", "44005"),
    ),
    Region(
        code="NYBT",
        name="New York Bight",
        west=-74.10, south=40.20, east=-73.20, north=40.90,
        buoy_candidates=("44025", "44065", "44009"),
    ),
    Region(
        code="FLKY",
        name="Florida Keys",
        west=-81.90, south=24.30, east=-80.20, north=25.40,
        buoy_candidates=("41043", "41009", "41047"),
    ),
    Region(
        code="HATT",
        name="Cape Hatteras",
        west=-76.00, south=34.50, east=-74.50, north=35.60,
        buoy_candidates=("41002", "41025", "41048"),
    ),
    Region(
        code="OAHU",
        name="O'ahu, Hawai'i",
        west=-158.40, south=21.10, east=-157.50, north=21.80,
        buoy_candidates=("51201", "51002", "51001"),
    ),
)

REGIONS_BY_CODE: Final[dict[str, Region]] = {r.code: r for r in REGIONS}


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
class TelemetryEvent(BaseModel):
    """One completed HTTP exchange, as measured on the wire."""

    source: str
    label: str
    method: str
    url: str
    status: int | None
    ok: bool
    latency_ms: float
    payload_bytes: int
    attempts: int
    started_at: datetime
    error: str | None = None

    @property
    def line(self) -> str:
        """Fixed-width console rendering used by the dashboard's log feed."""
        stamp = self.started_at.strftime("%H:%M:%S.%f")[:-3]
        status = str(self.status) if self.status is not None else "---"
        verdict = "OK " if self.ok else "ERR"
        return (
            f"{stamp}Z  {self.source:<9} {self.label:<26} {status:>3} {verdict} "
            f"{self.latency_ms:>8.0f}ms {_humanise_bytes(self.payload_bytes):>9} "
            f"x{self.attempts}"
        )


def _humanise_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


class TelemetrySink:
    """Append-only collector for :class:`TelemetryEvent` records.

    Single-threaded by design: all writes happen from one asyncio event loop.
    """

    def __init__(self) -> None:
        self._events: list[TelemetryEvent] = []

    def record(self, event: TelemetryEvent) -> None:
        self._events.append(event)

    @property
    def events(self) -> list[TelemetryEvent]:
        return list(self._events)

    @property
    def total_bytes(self) -> int:
        return sum(e.payload_bytes for e in self._events)

    @property
    def total_requests(self) -> int:
        return len(self._events)

    @property
    def failed_requests(self) -> int:
        return sum(1 for e in self._events if not e.ok)

    def latency_percentile(self, pct: float) -> float:
        """Nearest-rank percentile over observed latencies (0.0 when empty)."""
        lat = sorted(e.latency_ms for e in self._events)
        if not lat:
            return 0.0
        idx = max(0, min(len(lat) - 1, math.ceil(pct / 100.0 * len(lat)) - 1))
        return lat[idx]


class ApiError(RuntimeError):
    """Raised when an endpoint cannot be satisfied after all retries/mirrors."""


# --------------------------------------------------------------------------- #
# Base client
# --------------------------------------------------------------------------- #
class _BaseClient:
    """Shared retry, mirror-rotation and telemetry behaviour."""

    source: str = "generic"

    def __init__(
        self,
        client: httpx.AsyncClient,
        sink: TelemetrySink,
        *,
        max_attempts: int = 3,
    ) -> None:
        self._client = client
        self._sink = sink
        self._max_attempts = max_attempts

    async def _fetch(
        self,
        url: str,
        label: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        timeout: float = 45.0,
        mirrors: Sequence[str] = (),
    ) -> httpx.Response:
        """Issue a request with exponential backoff, then mirror rotation.

        Retries only on transient statuses and transport errors. Raises
        :class:`ApiError` when every candidate URL is exhausted; the failure is
        recorded in the telemetry sink either way.
        """
        candidates = [url, *mirrors]
        last_error: str = "no attempt made"

        for candidate in candidates:
            for attempt in range(1, self._max_attempts + 1):
                started = datetime.now(timezone.utc)
                t0 = time.perf_counter()
                try:
                    if method == "POST":
                        response = await self._client.post(
                            candidate, data=data, timeout=timeout
                        )
                    else:
                        response = await self._client.get(candidate, timeout=timeout)
                    elapsed = (time.perf_counter() - t0) * 1000.0

                    if response.status_code in RETRY_STATUS:
                        last_error = f"HTTP {response.status_code}"
                        final = (
                            attempt == self._max_attempts
                            and candidate == candidates[-1]
                        )
                        self._sink.record(
                            TelemetryEvent(
                                source=self.source, label=label, method=method,
                                url=candidate, status=response.status_code, ok=False,
                                latency_ms=elapsed,
                                payload_bytes=len(response.content),
                                attempts=attempt, started_at=started,
                                error=last_error,
                            )
                        )
                        if final:
                            break
                        await asyncio.sleep(0.6 * (2 ** (attempt - 1)) + random.random() * 0.4)
                        continue

                    self._sink.record(
                        TelemetryEvent(
                            source=self.source, label=label, method=method,
                            url=candidate, status=response.status_code,
                            ok=response.is_success, latency_ms=elapsed,
                            payload_bytes=len(response.content), attempts=attempt,
                            started_at=started,
                            error=None if response.is_success else f"HTTP {response.status_code}",
                        )
                    )
                    if response.is_success:
                        return response
                    raise ApiError(f"{label}: HTTP {response.status_code}")

                except (httpx.TransportError, httpx.InvalidURL) as exc:
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    last_error = f"{type(exc).__name__}: {exc}"
                    self._sink.record(
                        TelemetryEvent(
                            source=self.source, label=label, method=method,
                            url=candidate, status=None, ok=False,
                            latency_ms=elapsed, payload_bytes=0, attempts=attempt,
                            started_at=started, error=last_error,
                        )
                    )
                    if attempt == self._max_attempts:
                        break
                    await asyncio.sleep(0.6 * (2 ** (attempt - 1)) + random.random() * 0.4)

        raise ApiError(f"{label}: exhausted {len(candidates)} endpoint(s) — {last_error}")


# --------------------------------------------------------------------------- #
# OBIS — biodiversity
# --------------------------------------------------------------------------- #
class ObisSnapshot(BaseModel):
    """Occurrence statistics and phylum composition for a region."""

    records: int
    species: int
    taxa: int
    datasets: int
    species_level_records: int
    year_min: int | None
    year_max: int | None
    phylum_records: dict[str, int] = Field(default_factory=dict)
    top_taxa: list[dict[str, Any]] = Field(default_factory=list)
    checklist_sampled: int = 0

    @property
    def total_classified_records(self) -> int:
        return sum(self.phylum_records.values())


class ObisClient(_BaseClient):
    source = "OBIS"
    BASE: Final[str] = "https://api.obis.org/v3"

    async def fetch(self, region: Region, *, checklist_size: int = 200) -> ObisSnapshot:
        stats_resp, checklist_resp = await asyncio.gather(
            self._fetch(
                str(httpx.URL(f"{self.BASE}/statistics", params={"geometry": region.wkt})),
                "statistics",
                timeout=60.0,
            ),
            self._fetch(
                str(
                    httpx.URL(
                        f"{self.BASE}/checklist",
                        params={"geometry": region.wkt, "size": checklist_size},
                    )
                ),
                "checklist",
                timeout=90.0,
            ),
        )

        stats: dict[str, Any] = stats_resp.json()
        checklist: dict[str, Any] = checklist_resp.json()
        results: list[dict[str, Any]] = checklist.get("results") or []

        phylum_records: dict[str, int] = {}
        for entry in results:
            phylum = entry.get("phylum") or entry.get("kingdom") or "Unassigned"
            count = entry.get("records") or 0
            if isinstance(count, (int, float)) and count > 0:
                phylum_records[phylum] = phylum_records.get(phylum, 0) + int(count)

        top_taxa = [
            {
                "scientificName": e.get("scientificName"),
                "phylum": e.get("phylum") or e.get("kingdom") or "Unassigned",
                "class": e.get("class"),
                "taxonRank": e.get("taxonRank"),
                "records": int(e.get("records") or 0),
            }
            for e in results[:40]
        ]

        year_range = stats.get("yearrange") or [None, None]
        return ObisSnapshot(
            records=int(stats.get("records") or 0),
            species=int(stats.get("species") or 0),
            taxa=int(stats.get("taxa") or 0),
            datasets=int(stats.get("datasets") or 0),
            species_level_records=int(stats.get("specieslevel") or 0),
            year_min=year_range[0] if year_range else None,
            year_max=year_range[1] if len(year_range) > 1 else None,
            phylum_records=phylum_records,
            top_taxa=top_taxa,
            checklist_sampled=len(results),
        )


# --------------------------------------------------------------------------- #
# Open-Meteo — live sea state
# --------------------------------------------------------------------------- #
class SeaStateSnapshot(BaseModel):
    """Present-day SST and wave state on an hourly grid."""

    latitude: float
    longitude: float
    times: list[datetime] = Field(default_factory=list)
    sst_c: list[float | None] = Field(default_factory=list)
    wave_height_m: list[float | None] = Field(default_factory=list)
    current_sst_c: float | None = None
    current_wave_height_m: float | None = None
    observed_at: datetime | None = None

    @property
    def coverage(self) -> float:
        """Fraction of the returned hourly SST slots that carry a value."""
        if not self.sst_c:
            return 0.0
        return sum(1 for v in self.sst_c if v is not None) / len(self.sst_c)


class OpenMeteoMarineClient(_BaseClient):
    source = "OPEN-METEO"
    BASE: Final[str] = "https://marine-api.open-meteo.com/v1/marine"

    async def fetch(self, region: Region, *, past_days: int = 5) -> SeaStateSnapshot:
        lat, lon = region.centroid
        url = str(
            httpx.URL(
                self.BASE,
                params={
                    "latitude": f"{lat:.4f}",
                    "longitude": f"{lon:.4f}",
                    "hourly": "sea_surface_temperature,wave_height",
                    "past_days": past_days,
                    "forecast_days": 1,
                    "timezone": "UTC",
                },
            )
        )
        payload: dict[str, Any] = (await self._fetch(url, "marine/hourly", timeout=45.0)).json()
        hourly: dict[str, Any] = payload.get("hourly") or {}

        raw_times: list[str] = hourly.get("time") or []
        times = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in raw_times]
        sst: list[float | None] = list(hourly.get("sea_surface_temperature") or [])
        waves: list[float | None] = list(hourly.get("wave_height") or [])

        # Latest slot that is at or before "now" and actually carries a value.
        now = datetime.now(timezone.utc)
        current_sst: float | None = None
        current_wave: float | None = None
        observed_at: datetime | None = None
        for idx in range(len(times) - 1, -1, -1):
            if times[idx] > now:
                continue
            value = sst[idx] if idx < len(sst) else None
            if value is not None:
                current_sst = float(value)
                observed_at = times[idx]
                if idx < len(waves) and waves[idx] is not None:
                    current_wave = float(waves[idx])
                break

        return SeaStateSnapshot(
            latitude=float(payload.get("latitude", lat)),
            longitude=float(payload.get("longitude", lon)),
            times=times,
            sst_c=sst,
            wave_height_m=waves,
            current_sst_c=current_sst,
            current_wave_height_m=current_wave,
            observed_at=observed_at,
        )


# --------------------------------------------------------------------------- #
# NOAA ERDDAP — buoys (tabledap) and OISST climatology (griddap)
# --------------------------------------------------------------------------- #
class BuoySnapshot(BaseModel):
    """Latest in-situ reading from an NDBC station, plus a short history.

    ``status`` is one of ``live`` (fresh reading with water temperature),
    ``partial`` (station reporting but water temperature null), ``stale``
    (last reading older than the freshness window) or ``offline`` (no station
    in the candidate list answered).
    """

    station: str | None = None
    status: str = "offline"
    latitude: float | None = None
    longitude: float | None = None
    observed_at: datetime | None = None
    age_hours: float | None = None
    water_temp_c: float | None = None
    air_temp_c: float | None = None
    wind_speed_ms: float | None = None
    wave_height_m: float | None = None
    distance_km: float | None = None
    history_times: list[datetime] = Field(default_factory=list)
    history_wtmp_c: list[float | None] = Field(default_factory=list)
    stations_tried: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.status == "live" and self.water_temp_c is not None


class ClimatologySnapshot(BaseModel):
    """Ten-year OISST baseline for the current calendar window."""

    latitude: float | None = None
    longitude: float | None = None
    window_days: int = 10
    anchor_month: int = 1
    anchor_day: int = 1
    yearly_means: dict[int, float] = Field(default_factory=dict)
    baseline_mean: float | None = None
    baseline_std: float | None = None
    baseline_p90: float | None = None
    observations: int = 0
    years_covered: int = 0
    #: Least-squares point estimate of warming across the baseline years.
    trend_c_per_decade: float | None = None
    #: Standard error of that slope, same units.
    trend_stderr_c_per_decade: float | None = None
    #: Fraction of interannual variance the linear fit explains.
    trend_r2: float | None = None
    #: One-sided ~95 % lower confidence bound on the slope, floored at zero.
    #: This is the only trend figure used for scoring — warming that survives
    #: its own uncertainty. Zero means "no trend distinguishable from noise".
    trend_lower_c_per_decade: float | None = None
    failed_years: list[int] = Field(default_factory=list)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _linear_trend(
    xs: Sequence[float], ys: Sequence[float]
) -> tuple[float, float, float] | None:
    """Ordinary-least-squares fit returning ``(slope, stderr_of_slope, r_squared)``.

    The standard error matters as much as the slope here: a regression over ten
    annual means of a short seasonal window is dominated by interannual
    variability, so a point estimate alone routinely implies warming rates an
    order of magnitude above anything physical. Callers use the error to shrink
    the trend toward zero when it cannot be distinguished from noise.

    Returns None when fewer than three points or zero variance in ``xs``.
    """
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None

    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    sst = sum((y - my) ** 2 for y in ys)

    stderr = math.sqrt(sse / (n - 2) / sxx) if n > 2 else float("inf")
    r_squared = 1.0 - sse / sst if sst > 0 else 0.0
    return slope, stderr, r_squared


class ErddapClient(_BaseClient):
    """NOAA ERDDAP access.

    ERDDAP rejects the percent-encoding that a generic query-string builder
    produces (constraints must follow a literal ``&``, and quoting must survive
    verbatim), so request URLs here are assembled as strings rather than via
    ``params=``.
    """

    source = "NOAA-ERDDAP"
    BASE: Final[str] = "https://coastwatch.pfeg.noaa.gov/erddap"
    BUOY_DATASET: Final[str] = "cwwcNDBCMet"
    SST_DATASET: Final[str] = "ncdcOisst21Agg_LonPM180"
    SST_DATASET_FALLBACK: Final[str] = "ncdcOisst21Agg"

    # ---------------------------- buoys ----------------------------------- #
    async def discover_stations(
        self,
        lat: float,
        lon: float,
        *,
        search_deg: float = 5.0,
        lookback_hours: int = 72,
        limit: int = 6,
    ) -> list[tuple[str, float]]:
        """Find NDBC stations near a point, nearest first.

        Replaces the curated ``buoy_candidates`` list for searched locations.
        One row per station (its most recent position) is pulled from a
        lat/lon box around the point, then ranked by great-circle distance.

        Returns ``(station_id, distance_km)`` pairs. An empty list is the
        expected, correct result outside the NOAA coverage footprint — most of
        the planet — and the caller degrades to model-only SST rather than
        treating it as an error.
        """
        south = max(-90.0, lat - search_deg)
        north = min(90.0, lat + search_deg)
        west = max(-180.0, lon - search_deg)
        east = min(180.0, lon + search_deg)
        since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        url = (
            f"{self.BASE}/tabledap/{self.BUOY_DATASET}.json"
            "?station,latitude,longitude,time"
            f"&latitude%3E={south}&latitude%3C={north}"
            f"&longitude%3E={west}&longitude%3C={east}"
            f"&time%3E={since}"
            "&orderByMax(%22station,time%22)"
        )
        try:
            response = await self._fetch(url, "buoy/discover", timeout=60.0)
            rows: list[list[Any]] = response.json()["table"]["rows"]
        except (ApiError, KeyError, ValueError):
            return []

        ranked: list[tuple[str, float]] = []
        for row in rows:
            try:
                station = str(row[0])
                slat, slon = float(row[1]), float(row[2])
            except (IndexError, TypeError, ValueError):
                continue
            ranked.append((station, _haversine_km(lat, lon, slat, slon)))
        ranked.sort(key=lambda pair: pair[1])
        return ranked[:limit]

    async def fetch_buoy(
        self,
        region: Region,
        *,
        lookback_hours: int = 48,
        freshness_hours: float = 6.0,
    ) -> BuoySnapshot:
        """Return the first candidate station with a usable recent reading.

        For curated regions the candidate list is walked in order. For searched
        regions (empty ``buoy_candidates``) the nearest stations are discovered
        first; if none exist within range, an ``offline`` snapshot explains that
        the location is outside NDBC coverage so the UI can say so and drop to
        model-only SST rather than blanking the panel.

        Falls through the candidate list on HTTP failure, empty result sets,
        stale timestamps and null water temperature. If no station qualifies,
        the best partial/stale result is returned so the dashboard can show
        *why* in-situ data is missing.
        """
        lat0, lon0 = region.centroid
        since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        tried: list[str] = []
        notes: list[str] = []
        fallback: BuoySnapshot | None = None

        candidates: tuple[str, ...] = region.buoy_candidates
        if not candidates:
            discovered = await self.discover_stations(lat0, lon0)
            if not discovered:
                return BuoySnapshot(
                    status="offline",
                    stations_tried=[],
                    notes=[
                        "no NDBC station within ~550 km of this location "
                        "(outside NOAA coverage) — SST is model-only here"
                    ],
                )
            candidates = tuple(station for station, _ in discovered)
            nearest_km = discovered[0][1]
            notes.append(
                f"discovered {len(candidates)} nearby station(s); "
                f"nearest is {nearest_km:.0f} km away"
            )

        for station in candidates:
            tried.append(station)
            url = (
                f"{self.BASE}/tabledap/{self.BUOY_DATASET}.json"
                "?station,time,latitude,longitude,wtmp,atmp,wspd,wvht"
                f"&station=%22{station}%22&time%3E={since}"
            )
            try:
                response = await self._fetch(url, f"buoy/{station}", timeout=60.0)
            except ApiError as exc:
                notes.append(f"{station}: request failed ({exc})")
                continue

            try:
                rows: list[list[Any]] = response.json()["table"]["rows"]
            except (KeyError, ValueError) as exc:
                notes.append(f"{station}: unparseable response ({type(exc).__name__})")
                continue

            if not rows:
                notes.append(f"{station}: no observations in last {lookback_hours}h")
                continue

            times: list[datetime] = []
            wtmps: list[float | None] = []
            for row in rows:
                try:
                    times.append(
                        datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
                    )
                except (ValueError, IndexError):
                    continue
                wtmps.append(float(row[4]) if row[4] is not None else None)

            latest = rows[-1]
            observed_at = times[-1] if times else None
            age = (
                (datetime.now(timezone.utc) - observed_at).total_seconds() / 3600.0
                if observed_at
                else None
            )
            lat = float(latest[2]) if latest[2] is not None else None
            lon = float(latest[3]) if latest[3] is not None else None

            # Most recent non-null water temperature within the window.
            wtmp = next((v for v in reversed(wtmps) if v is not None), None)

            if wtmp is None:
                status = "partial"
                notes.append(f"{station}: reporting but water temperature null")
            elif age is not None and age > freshness_hours:
                status = "stale"
                notes.append(f"{station}: last reading {age:.1f}h old")
            else:
                status = "live"

            snapshot = BuoySnapshot(
                station=station,
                status=status,
                latitude=lat,
                longitude=lon,
                observed_at=observed_at,
                age_hours=age,
                water_temp_c=wtmp,
                air_temp_c=float(latest[5]) if latest[5] is not None else None,
                wind_speed_ms=float(latest[6]) if latest[6] is not None else None,
                wave_height_m=float(latest[7]) if latest[7] is not None else None,
                distance_km=(
                    _haversine_km(lat0, lon0, lat, lon)
                    if lat is not None and lon is not None
                    else None
                ),
                history_times=times,
                history_wtmp_c=wtmps,
                stations_tried=list(tried),
                notes=list(notes),
            )
            if status == "live":
                return snapshot
            fallback = fallback or snapshot

        if fallback is not None:
            return fallback
        return BuoySnapshot(
            status="offline",
            stations_tried=tried,
            notes=notes or ["no candidate station returned data"],
        )

    # ------------------------- OISST climatology --------------------------- #
    def _sst_url(self, dataset: str, lat: float, lon: float, start: date, end: date) -> str:
        query_lon = lon if "LonPM180" in dataset else (lon % 360.0)
        return (
            f"{self.BASE}/griddap/{dataset}.json"
            f"?sst%5B({start.isoformat()}T12:00:00Z):1:({end.isoformat()}T12:00:00Z)%5D"
            f"%5B(0.0):1:(0.0)%5D"
            f"%5B({lat:.4f}):1:({lat:.4f})%5D"
            f"%5B({query_lon:.4f}):1:({query_lon:.4f})%5D"
        )

    async def _fetch_window(
        self,
        lat: float,
        lon: float,
        year: int,
        anchor: date,
        window_days: int,
        semaphore: asyncio.Semaphore,
    ) -> tuple[int, list[float], float | None, float | None]:
        """Fetch one year's day-of-year window. Returns (year, values, lat, lon)."""
        try:
            centre = date(year, anchor.month, anchor.day)
        except ValueError:  # 29 Feb in a non-leap year
            centre = date(year, anchor.month, 28)
        start = centre - timedelta(days=window_days)
        end = centre + timedelta(days=window_days)

        async with semaphore:
            url = self._sst_url(self.SST_DATASET, lat, lon, start, end)
            try:
                response = await self._fetch(url, f"oisst/{year}", timeout=90.0)
            except ApiError:
                url = self._sst_url(self.SST_DATASET_FALLBACK, lat, lon, start, end)
                response = await self._fetch(url, f"oisst/{year}/alt", timeout=90.0)

        rows: list[list[Any]] = response.json()["table"]["rows"]
        values = [float(r[4]) for r in rows if r[4] is not None]
        grid_lat = float(rows[0][2]) if rows else None
        grid_lon = float(rows[0][3]) if rows else None
        return year, values, grid_lat, grid_lon

    async def fetch_climatology(
        self,
        region: Region,
        *,
        years: int = 10,
        window_days: int = 10,
        max_concurrency: int = 4,
        anchor: date | None = None,
    ) -> ClimatologySnapshot:
        """Build a ten-year day-of-year SST baseline from NOAA OISST v2.1.

        Only completed prior years are requested, which keeps the baseline clear
        of the product's ~2-week publication lag. Individual years that fail are
        recorded in ``failed_years`` and excluded rather than aborting the set.
        """
        lat, lon = region.centroid
        anchor = anchor or datetime.now(timezone.utc).date()
        target_years = list(range(anchor.year - years, anchor.year))
        semaphore = asyncio.Semaphore(max_concurrency)

        results = await asyncio.gather(
            *[
                self._fetch_window(lat, lon, y, anchor, window_days, semaphore)
                for y in target_years
            ],
            return_exceptions=True,
        )

        yearly_means: dict[int, float] = {}
        all_values: list[float] = []
        failed: list[int] = []
        grid_lat: float | None = None
        grid_lon: float | None = None

        for year, outcome in zip(target_years, results):
            if isinstance(outcome, BaseException) or not outcome[1]:
                failed.append(year)
                continue
            _, values, glat, glon = outcome
            yearly_means[year] = sum(values) / len(values)
            all_values.extend(values)
            grid_lat = grid_lat if grid_lat is not None else glat
            grid_lon = grid_lon if grid_lon is not None else glon

        baseline_mean: float | None = None
        baseline_std: float | None = None
        baseline_p90: float | None = None
        if all_values:
            baseline_mean = sum(all_values) / len(all_values)
            if len(all_values) > 1:
                variance = sum((v - baseline_mean) ** 2 for v in all_values) / (
                    len(all_values) - 1
                )
                baseline_std = math.sqrt(variance)
            ordered = sorted(all_values)
            rank = max(0, math.ceil(0.90 * len(ordered)) - 1)
            baseline_p90 = ordered[rank]

        trend: float | None = None
        trend_stderr: float | None = None
        trend_r2: float | None = None
        trend_lower: float | None = None
        if len(yearly_means) >= 3:
            fit = _linear_trend(
                [float(y) for y in yearly_means], list(yearly_means.values())
            )
            if fit is not None:
                slope, stderr, r_squared = fit
                trend = slope * 10.0
                trend_stderr = stderr * 10.0
                trend_r2 = r_squared
                # 1.645 sigma ~ one-sided 95 %: count only warming that clears noise.
                trend_lower = max(0.0, trend - 1.645 * trend_stderr)

        return ClimatologySnapshot(
            latitude=grid_lat,
            longitude=grid_lon,
            window_days=window_days,
            anchor_month=anchor.month,
            anchor_day=anchor.day,
            yearly_means=yearly_means,
            baseline_mean=baseline_mean,
            baseline_std=baseline_std,
            baseline_p90=baseline_p90,
            observations=len(all_values),
            years_covered=len(yearly_means),
            trend_c_per_decade=trend,
            trend_stderr_c_per_decade=trend_stderr,
            trend_r2=trend_r2,
            trend_lower_c_per_decade=trend_lower,
            failed_years=failed,
        )


# --------------------------------------------------------------------------- #
# Overpass / OpenSeaMap — maritime infrastructure
# --------------------------------------------------------------------------- #
#: Seamark types that indicate concentrated vessel activity: formal routing
#: measures (traffic separation schemes, fairways, recommended tracks) plus
#: berthing and anchoring infrastructure. Used as the shipping-pressure signal.
TRAFFIC_PRESSURE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "separation_lane",
        "separation_zone",
        "separation_boundary",
        "separation_line",
        "separation_crossing",
        "separation_roundabout",
        "recommended_track",
        "navigation_line",
        "fairway",
        "deep_water_route",
        "two-way_route",
        "anchorage",
        "anchor_berth",
        "harbour",
        "mooring",
        "berth",
        "dock",
        "pilot_boarding_place",
    }
)


class InfrastructureSnapshot(BaseModel):
    """OpenSeaMap seamark density and composition for a region."""

    node_count: int = 0
    way_count: int = 0
    relation_count: int = 0
    total_count: int = 0
    type_breakdown: dict[str, int] = Field(default_factory=dict)
    traffic_features: int = 0
    area_km2: float = 0.0
    sampled_elements: int = 0
    mirror_used: str | None = None

    @property
    def density_per_1000km2(self) -> float:
        if self.area_km2 <= 0:
            return 0.0
        return self.total_count / self.area_km2 * 1000.0

    @property
    def traffic_share(self) -> float:
        if self.sampled_elements <= 0:
            return 0.0
        return self.traffic_features / self.sampled_elements


class OverpassClient(_BaseClient):
    source = "OVERPASS"
    MIRRORS: Final[tuple[str, ...]] = (
        "https://overpass-api.de/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://overpass.osm.ch/api/interpreter",
    )

    async def fetch(self, region: Region, *, sample_cap: int = 800) -> InfrastructureSnapshot:
        """Fetch authoritative element counts plus a capped tag sample.

        Two queries are used because Overpass reports exact totals only via
        ``out count``, while composition requires tags. The tag query is capped
        and its failure is non-fatal — counts alone still drive the density term.
        """
        bbox = f"{region.south},{region.west},{region.north},{region.east}"
        count_query = (
            "[out:json][timeout:90];"
            f'(node["seamark:type"]({bbox});way["seamark:type"]({bbox}););'
            "out count;"
        )
        response = await self._fetch(
            self.MIRRORS[0],
            "seamark/count",
            method="POST",
            data={"data": count_query},
            timeout=120.0,
            mirrors=self.MIRRORS[1:],
        )
        elements: list[dict[str, Any]] = response.json().get("elements") or []
        tags: dict[str, str] = next(
            (e.get("tags", {}) for e in elements if e.get("type") == "count"), {}
        )
        snapshot = InfrastructureSnapshot(
            node_count=int(tags.get("nodes", 0)),
            way_count=int(tags.get("ways", 0)),
            relation_count=int(tags.get("relations", 0)),
            total_count=int(tags.get("total", 0)),
            area_km2=region.area_km2,
            mirror_used=str(response.request.url),
        )

        tag_query = (
            "[out:json][timeout:90];"
            f'(node["seamark:type"]({bbox});way["seamark:type"]({bbox}););'
            f"out tags {sample_cap};"
        )
        try:
            tag_response = await self._fetch(
                self.MIRRORS[0],
                "seamark/tags",
                method="POST",
                data={"data": tag_query},
                timeout=120.0,
                mirrors=self.MIRRORS[1:],
            )
            sampled = tag_response.json().get("elements") or []
        except (ApiError, ValueError):
            return snapshot

        breakdown: dict[str, int] = {}
        traffic = 0
        for element in sampled:
            kind = (element.get("tags") or {}).get("seamark:type")
            if not kind:
                continue
            breakdown[kind] = breakdown.get(kind, 0) + 1
            if kind in TRAFFIC_PRESSURE_TYPES:
                traffic += 1

        snapshot.type_breakdown = dict(
            sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)
        )
        snapshot.traffic_features = traffic
        snapshot.sampled_elements = sum(breakdown.values())
        return snapshot


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
class RegionSnapshot(BaseModel):
    """Everything the analyzer needs for one region, plus fetch provenance."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    region: Region
    fetched_at: datetime
    duration_ms: float
    obis: ObisSnapshot | None = None
    sea_state: SeaStateSnapshot | None = None
    buoy: BuoySnapshot | None = None
    climatology: ClimatologySnapshot | None = None
    infrastructure: InfrastructureSnapshot | None = None
    errors: dict[str, str] = Field(default_factory=dict)
    telemetry: list[TelemetryEvent] = Field(default_factory=list)

    @property
    def sources_ok(self) -> int:
        return sum(
            1
            for s in (self.obis, self.sea_state, self.buoy, self.climatology, self.infrastructure)
            if s is not None
        )

    @property
    def marine_coverage(self) -> str:
        """Classify the thermal coverage at this location.

        * ``"none"`` — both the OISST baseline and the Open-Meteo model returned
          successfully but carry no sea-surface temperature. That all-null shape
          is what an inland point produces (someone searched "Denver"), and it
          is the same silent shape that once hid the pre-2023 SST gap, so it is
          detected explicitly rather than left to look like a fetch failure.
          Only reported when both feeds *succeeded* and were empty; if either
          feed raised, the coverage is left as degraded, not "none".
        * ``"model_only"`` — SST is available, but no usable in-situ buoy backs
          it up (the norm outside US waters). Confidence is lower and the UI
          says why.
        * ``"full"`` — SST plus a fresh in-situ buoy cross-check.
        * ``"unknown"`` — the thermal feeds failed to return; this is a
          transient degradation, handled by the normal error path.
        """
        clim_present = self.climatology is not None
        model_present = self.sea_state is not None
        if not (clim_present and model_present):
            return "unknown"

        clim_empty = (self.climatology.observations or 0) == 0
        model_empty = not any(v is not None for v in (self.sea_state.sst_c or []))
        if clim_empty and model_empty:
            return "none"

        buoy_ok = self.buoy is not None and self.buoy.is_usable
        return "full" if buoy_ok else "model_only"


#: All five source feeds, in fetch order.
FEED_NAMES: Final[tuple[str, ...]] = (
    "obis", "sea_state", "buoy", "climatology", "infrastructure"
)


class FeedBundle(BaseModel):
    """The result of fetching a subset of feeds: the feed objects plus the
    provenance (errors, telemetry, timing) for exactly what was fetched.

    Feeds age at very different rates, so the dashboard caches them in
    volatility tiers and composes a :class:`RegionSnapshot` from more than one
    bundle (see ``data_access``). Every field is picklable so a bundle can live
    in Streamlit's cache.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    obis: ObisSnapshot | None = None
    sea_state: SeaStateSnapshot | None = None
    buoy: BuoySnapshot | None = None
    climatology: ClimatologySnapshot | None = None
    infrastructure: InfrastructureSnapshot | None = None
    feeds_requested: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    telemetry: list[TelemetryEvent] = Field(default_factory=list)
    duration_ms: float = 0.0
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


async def fetch_feeds(
    region: Region,
    feeds: Sequence[str],
    *,
    baseline_years: int = 10,
    window_days: int = 10,
    timeout: float = 120.0,
) -> FeedBundle:
    """Fetch the named feeds concurrently under one client, isolating failures.

    Only the requested feeds are fetched, so a caller can refresh the
    fast-changing feeds (buoy, model SST) without re-pulling the slow ones (the
    OISST baseline, biodiversity, infrastructure). Each feed degrades
    independently; the reason is preserved in ``errors``.
    """
    wanted = [f for f in FEED_NAMES if f in set(feeds)]
    sink = TelemetrySink()
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        limits=limits,
    ) as client:
        obis = ObisClient(client, sink)
        marine = OpenMeteoMarineClient(client, sink)
        erddap = ErddapClient(client, sink)
        # The public Overpass instances shed load with 504s under contention.
        # Rotating to the next mirror beats hammering a host that is already
        # saying it is busy, so this client retries less and fails over sooner.
        overpass = OverpassClient(client, sink, max_attempts=2)

        # Factories (not coroutines) so unrequested feeds are never created.
        factories = {
            "obis": lambda: obis.fetch(region),
            "sea_state": lambda: marine.fetch(region),
            "buoy": lambda: erddap.fetch_buoy(region),
            "climatology": lambda: erddap.fetch_climatology(
                region, years=baseline_years, window_days=window_days
            ),
            "infrastructure": lambda: overpass.fetch(region),
        }
        results = await asyncio.gather(
            *(factories[name]() for name in wanted), return_exceptions=True
        )

    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, outcome in zip(wanted, results):
        if isinstance(outcome, BaseException):
            errors[name] = f"{type(outcome).__name__}: {outcome}"
            values[name] = None
        else:
            values[name] = outcome

    return FeedBundle(
        feeds_requested=wanted,
        errors=errors,
        telemetry=sink.events,
        duration_ms=(time.perf_counter() - t0) * 1000.0,
        fetched_at=started,
        **values,
    )


async def fetch_region_snapshot(
    region: Region,
    *,
    baseline_years: int = 10,
    window_days: int = 10,
    timeout: float = 120.0,
) -> RegionSnapshot:
    """Fetch all five source feeds for a region concurrently and compose them.

    Thin wrapper over :func:`fetch_feeds` requesting every feed at once. The
    dashboard's caching layer fetches feeds in volatility tiers instead (see
    ``data_access``); this single-shot form is what the standalone CLIs and
    tests use, and its behaviour is unchanged.
    """
    bundle = await fetch_feeds(
        region, FEED_NAMES,
        baseline_years=baseline_years, window_days=window_days, timeout=timeout,
    )
    return RegionSnapshot(
        region=region,
        fetched_at=bundle.fetched_at,
        duration_ms=bundle.duration_ms,
        obis=bundle.obis,
        sea_state=bundle.sea_state,
        buoy=bundle.buoy,
        climatology=bundle.climatology,
        infrastructure=bundle.infrastructure,
        errors=bundle.errors,
        telemetry=bundle.telemetry,
    )


# --------------------------------------------------------------------------- #
# Standalone connectivity check
# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    code = sys.argv[1].upper() if len(sys.argv) > 1 else "MTBY"
    target = REGIONS_BY_CODE.get(code, REGIONS[0])

    snapshot = asyncio.run(fetch_region_snapshot(target, baseline_years=3, window_days=5))
    print(f"\nregion={target.name}  sources_ok={snapshot.sources_ok}/5  "
          f"wall={snapshot.duration_ms:.0f}ms")
    if snapshot.obis:
        print(f"  OBIS      records={snapshot.obis.records:,} species={snapshot.obis.species:,} "
              f"phyla={len(snapshot.obis.phylum_records)}")
    if snapshot.sea_state:
        print(f"  MARINE    sst={snapshot.sea_state.current_sst_c} "
              f"coverage={snapshot.sea_state.coverage:.0%}")
    if snapshot.buoy:
        print(f"  BUOY      station={snapshot.buoy.station} status={snapshot.buoy.status} "
              f"wtmp={snapshot.buoy.water_temp_c}")
    if snapshot.climatology:
        print(f"  OISST     mean={snapshot.climatology.baseline_mean} "
              f"years={snapshot.climatology.years_covered} "
              f"trend={snapshot.climatology.trend_c_per_decade}")
    if snapshot.infrastructure:
        print(f"  OVERPASS  total={snapshot.infrastructure.total_count} "
              f"density={snapshot.infrastructure.density_per_1000km2:.1f}/1000km2")
    for key, message in snapshot.errors.items():
        print(f"  ERROR {key}: {message}")
    print("\n--- telemetry ---")
    for event in snapshot.telemetry:
        print(" ", event.line)
