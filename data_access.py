"""Cached, failure-tolerant access to the live region feeds.

Built for unattended operation. The dashboard is expected to run overnight with
nobody watching, so a transient upstream outage must not blank the screen or
crash the page. Every load returns a :class:`LoadResult`:

* a fresh fetch when the APIs cooperate;
* the last good result for that region when they do not — held in process
  memory and mirrored to disk, so it survives both a failed refresh and a
  restart of the Streamlit server;
* an empty result carrying the error only when there has never been a
  successful fetch for that region.

The staleness is always reported rather than hidden: ``LoadResult.stale`` drives
the LIVE/STALE indicator, and ``error`` carries the reason the refresh failed.

**Volatility-tiered caching.** The five feeds age at very different rates, so
they are cached in two tiers rather than as one blob:

* the *dynamic* tier — in-situ buoy and model SST — changes in minutes to hours
  and is cached briefly (``DYNAMIC_TTL``), keyed by ``(code, nonce)`` so the
  REFRESH button re-pulls it;
* the *context* tier — the OISST day-of-year baseline, OBIS biodiversity and
  OpenSeaMap infrastructure — is effectively stable within a calendar day (the
  baseline lags two weeks; biodiversity and infrastructure change over months),
  so it is keyed by ``(code, UTC date)`` and cached for a day.

The two tiers are fetched concurrently *within* a tier and composed into the
``RegionSnapshot`` the analyzer and UI already expect. The common path — a
timed refresh where the context tier is still fresh — then re-fetches only the
fast feeds (a few seconds) instead of the whole ~70-second baseline every
cycle. A cold miss of both tiers runs them back to back, a small, rare cost.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from analyzer import StressAssessment, assess_region
from api_clients import (
    REGIONS_BY_CODE, FeedBundle, Region, RegionSnapshot, fetch_feeds,
)

__all__ = ["LoadResult", "load_region", "CACHE_TTL_SECONDS"]

#: Feeds that change fast (minutes-hours): re-fetched on every timed refresh.
DYNAMIC_FEEDS = ("buoy", "sea_state")
#: Feeds that are stable within a day: fetched roughly once per UTC day.
CONTEXT_FEEDS = ("climatology", "obis", "infrastructure")

#: TTLs matched to volatility.
DYNAMIC_TTL = 600            # 10 minutes
CONTEXT_TTL = 86_400         # 24 hours (the date key does the real invalidation)

#: Back-compat alias — the dynamic tier's TTL is the "freshness" the UI tracks.
CACHE_TTL_SECONDS = DYNAMIC_TTL

#: A composed result is only stored as last-known-good when it carries real
#: data, so an all-empty outage result never overwrites a good one on disk.
_MIN_SOURCES_TO_PERSIST = 2

CACHE_DIR = Path(__file__).resolve().parent / ".cache"

#: In-process last-known-good, keyed by region code. Survives reruns and
#: cache expiry; lost on restart, which is what the disk mirror covers.
_LAST_GOOD: dict[str, tuple[RegionSnapshot, StressAssessment]] = {}


@dataclass(frozen=True)
class LoadResult:
    """Outcome of a load attempt, including provenance and staleness."""

    snapshot: RegionSnapshot | None
    assessment: StressAssessment | None
    origin: str                      # "live" | "memory" | "disk" | "none"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.snapshot is not None and self.assessment is not None

    @property
    def stale(self) -> bool:
        return self.origin in {"memory", "disk"}

    @property
    def age_seconds(self) -> float:
        if self.snapshot is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.snapshot.fetched_at).total_seconds()


# --------------------------------------------------------------------------- #
# Disk mirror
# --------------------------------------------------------------------------- #
def _safe_code(region_code: str) -> str:
    """Filesystem-safe form of a region code (searched codes hold '@' and ',')."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", region_code)


def _paths(region_code: str) -> tuple[Path, Path]:
    safe = _safe_code(region_code)
    return (
        CACHE_DIR / f"last_good_{safe}_snapshot.json",
        CACHE_DIR / f"last_good_{safe}_assessment.json",
    )


#: Cap on the number of distinct locations mirrored to disk. Searched locations
#: are user-driven and otherwise unbounded, so the oldest are evicted once this
#: many exist. Comfortably above the seven curated regions.
MAX_CACHED_LOCATIONS = 100


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + os.replace so a concurrent reader never sees a
    half-written file. os.replace is atomic on the same filesystem."""
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _evict_old(keep: int = MAX_CACHED_LOCATIONS) -> None:
    """Keep only the ``keep`` most recently written locations. Best-effort."""
    try:
        snaps = sorted(
            CACHE_DIR.glob("last_good_*_snapshot.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in snaps[keep:]:
            stale.unlink(missing_ok=True)
            assess = stale.with_name(
                stale.name.replace("_snapshot.json", "_assessment.json")
            )
            assess.unlink(missing_ok=True)
    except Exception:
        pass


def _persist(region_code: str, snapshot: RegionSnapshot,
             assessment: StressAssessment) -> None:
    """Mirror a good result to disk. Never raises — persistence is best-effort.

    Writes are atomic (temp + replace) so a concurrent ``_restore`` cannot read
    a partial file, and the location count is capped so user-driven searches do
    not grow the cache without bound.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        snap_path, assess_path = _paths(region_code)
        _atomic_write(snap_path, snapshot.model_dump_json())
        _atomic_write(assess_path, assessment.model_dump_json())
        _evict_old()
    except Exception:
        pass


def _restore(region_code: str) -> tuple[RegionSnapshot, StressAssessment] | None:
    try:
        snap_path, assess_path = _paths(region_code)
        if not (snap_path.exists() and assess_path.exists()):
            return None
        snapshot = RegionSnapshot.model_validate_json(
            snap_path.read_text(encoding="utf-8")
        )
        assessment = StressAssessment.model_validate_json(
            assess_path.read_text(encoding="utf-8")
        )
        return snapshot, assessment
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Fetch — two volatility tiers, cached independently
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=DYNAMIC_TTL, show_spinner=False, max_entries=64)
def _fetch_dynamic(_region: Region, cache_key: tuple[str, int]) -> FeedBundle:
    """Fast-changing feeds (buoy + model SST). ``cache_key`` is ``(code, nonce)``
    so REFRESH re-pulls them; ``_region`` is excluded from the hash by its
    leading underscore."""
    return asyncio.run(fetch_feeds(_region, DYNAMIC_FEEDS))


@st.cache_data(ttl=CONTEXT_TTL, show_spinner=False, max_entries=64)
def _fetch_context(_region: Region, cache_key: tuple[str, str]) -> FeedBundle:
    """Slowly-changing feeds (OISST baseline + OBIS + infrastructure).
    ``cache_key`` is ``(code, UTC-date)`` — the date changes daily and drives
    invalidation; the long TTL is a memory-bound backstop. Deliberately not
    keyed by nonce: REFRESH refreshes live conditions, not the stable baseline.
    """
    return asyncio.run(fetch_feeds(_region, CONTEXT_FEEDS))


def _compose(
    region: Region, dynamic: FeedBundle, context: FeedBundle
) -> tuple[RegionSnapshot, StressAssessment]:
    """Merge the two tiers into one RegionSnapshot and score it.

    ``fetched_at`` and the wall-clock duration reflect the *live* tier — the
    context tier usually comes from cache and did not run this cycle. Telemetry
    from both tiers is merged and time-ordered, so the console honestly shows
    each feed's last request with its real timestamp (the baseline's hours ago,
    the buoy's seconds ago).
    """
    now = datetime.now(timezone.utc)
    context_ran_now = (now - context.fetched_at).total_seconds() < 30.0
    duration = dynamic.duration_ms + (context.duration_ms if context_ran_now else 0.0)
    telemetry = sorted(
        [*context.telemetry, *dynamic.telemetry], key=lambda e: e.started_at
    )
    snapshot = RegionSnapshot(
        region=region,
        fetched_at=dynamic.fetched_at,
        duration_ms=duration,
        obis=context.obis,
        sea_state=dynamic.sea_state,
        buoy=dynamic.buoy,
        climatology=context.climatology,
        infrastructure=context.infrastructure,
        errors={**context.errors, **dynamic.errors},
        telemetry=telemetry,
    )
    return snapshot, assess_region(snapshot)


def _remember(code: str, snapshot: RegionSnapshot, assessment: StressAssessment) -> None:
    """Store as last-known-good (memory + disk), bounded, and only when the
    result actually carries data — so an outage never poisons the cache."""
    if assessment.score is None and snapshot.sources_ok < _MIN_SOURCES_TO_PERSIST:
        return
    _LAST_GOOD.pop(code, None)  # re-insert at end for true LRU eviction
    _LAST_GOOD[code] = (snapshot, assessment)
    while len(_LAST_GOOD) > MAX_CACHED_LOCATIONS:
        _LAST_GOOD.pop(next(iter(_LAST_GOOD)))
    _persist(code, snapshot, assessment)


def load_region(region: Region | str, nonce: int = 0) -> LoadResult:
    """Load a region, degrading to the last good result instead of raising.

    Accepts either a curated region code (looked up in ``REGIONS_BY_CODE``) or a
    ready-made :class:`~api_clients.Region` — the latter is how searched
    locations flow through the same caching, persistence and fallback path as
    the built-in regions.

    The dynamic and context tiers are fetched (or served from cache) and
    composed. A partial fetch (some sources down) still counts as success — the
    analyzer redistributes weights and reports the degradation. Only a total
    failure to produce a snapshot falls back to cached data.
    """
    resolved = REGIONS_BY_CODE[region] if isinstance(region, str) else region
    code = resolved.code
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        dynamic = _fetch_dynamic(resolved, (code, nonce))
        context = _fetch_context(resolved, (code, day_key))
        snapshot, assessment = _compose(resolved, dynamic, context)
        _remember(code, snapshot, assessment)
        return LoadResult(snapshot, assessment, "live")
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"

    remembered = _LAST_GOOD.get(code)
    if remembered is not None:
        return LoadResult(remembered[0], remembered[1], "memory", reason)

    restored = _restore(code)
    if restored is not None:
        _LAST_GOOD[code] = restored
        return LoadResult(restored[0], restored[1], "disk", reason)

    return LoadResult(None, None, "none", reason)
