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
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import streamlit as st

from analyzer import StressAssessment, assess_region
from api_clients import (
    REGIONS_BY_CODE, FeedBundle, Region, RegionSnapshot, fetch_feeds,
    snapshot_from_bundle,
)

__all__ = [
    "LoadResult", "load_region", "load_history", "CACHE_TTL_SECONDS",
    "HISTORY_RETENTION_DAYS",
]

logger = logging.getLogger(__name__)

#: Feeds that change fast (minutes-hours): re-fetched on every timed refresh.
DYNAMIC_FEEDS = ("buoy", "sea_state")
#: Feeds that are stable within a day: fetched roughly once per UTC day.
CONTEXT_FEEDS = ("climatology", "obis", "infrastructure")

#: TTLs matched to volatility; overridable for deployments with different
#: refresh economics (values in seconds). The context tier's real invalidation
#: is its date key — the long TTL is a memory-bound backstop.
DYNAMIC_TTL = int(os.getenv("MEHI_DYNAMIC_TTL", "600"))     # 10 minutes
CONTEXT_TTL = int(os.getenv("MEHI_CONTEXT_TTL", "86400"))   # 24 hours

#: Back-compat alias — the dynamic tier's TTL is the "freshness" the UI tracks.
CACHE_TTL_SECONDS = DYNAMIC_TTL

#: A composed result is only stored as last-known-good when it carries real
#: data, so an all-empty outage result never overwrites a good one on disk.
_MIN_SOURCES_TO_PERSIST = 2

CACHE_DIR = Path(__file__).resolve().parent / ".cache"

#: In-process last-known-good, keyed by region code. Survives reruns and
#: cache expiry; lost on restart, which is what the disk mirror covers.
_LAST_GOOD: dict[str, tuple[RegionSnapshot, StressAssessment]] = {}

#: Retry generation for the context tier, keyed by region code:
#: ``(generation, last_bump_monotonic)``. The generation is part of the context
#: cache key, so bumping it forces a re-fetch. Without this, a transient
#: OISST/OBIS/Overpass failure at the first fetch of a UTC day would be cached
#: as a degraded bundle until the date rolled over. Bumps are spaced at least
#: ``DYNAMIC_TTL`` apart so a persistent outage is re-tried once per refresh
#: cycle, not hammered on every page load.
_CONTEXT_RETRY: dict[str, tuple[int, float]] = {}


def _context_generation(code: str) -> int:
    return _CONTEXT_RETRY.get(code, (0, 0.0))[0]


def _schedule_context_retry(code: str, context: FeedBundle) -> None:
    """Arrange a context re-fetch next cycle if any context feed failed."""
    failed = [f for f in CONTEXT_FEEDS if f in context.errors]
    if not failed:
        _CONTEXT_RETRY.pop(code, None)
        return
    generation, last_bump = _CONTEXT_RETRY.get(code, (0, 0.0))
    now = time.monotonic()
    if now - last_bump >= DYNAMIC_TTL:
        _CONTEXT_RETRY[code] = (generation + 1, now)
        logger.warning(
            "context feeds %s failed for %s; scheduling re-fetch next cycle",
            failed, code,
        )


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


def _history_path(region_code: str) -> Path:
    return CACHE_DIR / f"history_{_safe_code(region_code)}.jsonl"


#: How long a region's score history is kept. The dashboard is a live index,
#: not an archive — this is enough to show a multi-day trend without letting an
#: unattended run grow the cache without bound.
HISTORY_RETENTION_DAYS = int(os.getenv("MEHI_HISTORY_DAYS", "90"))

#: Minimum spacing between recorded history points. The page re-renders every
#: few minutes; recording every render would bury a day's trend in hundreds of
#: near-identical rows.
HISTORY_MIN_INTERVAL_SECONDS = 1800.0


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
            # The score history is keyed by the same safe code; evict it with
            # its location so searched locations cannot grow the cache forever.
            safe = stale.name[len("last_good_"):-len("_snapshot.json")]
            (CACHE_DIR / f"history_{safe}.jsonl").unlink(missing_ok=True)
    except Exception:
        logger.warning("cache eviction failed", exc_info=True)


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
        logger.warning("failed to mirror %s to disk", region_code, exc_info=True)


def load_history(region_code: str) -> list[dict[str, Any]]:
    """Score history for a region, oldest first. Never raises.

    Entries are ``{"at": iso8601, "score": float, "band": str,
    "confidence": float}``. Returns an empty list when nothing has been
    recorded yet (a fresh install, or a region only just searched).
    """
    path = _history_path(region_code)
    entries: list[dict[str, Any]] = []
    try:
        if not path.exists():
            return []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # skip a torn line rather than losing the file
            if isinstance(entry, dict) and entry.get("score") is not None:
                entries.append(entry)
    except Exception:
        logger.warning("failed to read history for %s", region_code, exc_info=True)
        return entries
    return entries


def _append_history(region_code: str, assessment: StressAssessment) -> None:
    """Append one scored observation, rate-limited and pruned. Best-effort.

    Appending (rather than overwriting, as the last-known-good mirror does) is
    what makes a multi-day stress trend possible at all.
    """
    if assessment.score is None:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _history_path(region_code)
        now = datetime.now(timezone.utc)

        existing = load_history(region_code)
        if existing:
            try:
                last = datetime.fromisoformat(existing[-1]["at"])
                if (now - last).total_seconds() < HISTORY_MIN_INTERVAL_SECONDS:
                    return
            except (KeyError, TypeError, ValueError):
                pass  # unparseable tail: fall through and append

        entry = {
            "at": now.isoformat(),
            "score": assessment.score,
            "band": assessment.band,
            "confidence": assessment.confidence,
        }
        cutoff = now - timedelta(days=HISTORY_RETENTION_DAYS)
        kept: list[dict[str, Any]] = []
        for row in existing:
            try:
                if datetime.fromisoformat(row["at"]) >= cutoff:
                    kept.append(row)
            except (KeyError, TypeError, ValueError):
                continue
        kept.append(entry)

        # Rewritten atomically: the file is small (a few hundred lines at most)
        # and this is what prunes it.
        _atomic_write(path, "\n".join(json.dumps(r) for r in kept) + "\n")
    except Exception:
        logger.warning("failed to append history for %s", region_code, exc_info=True)


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
        logger.warning("failed to restore %s from disk", region_code, exc_info=True)
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


@st.cache_data(ttl=CONTEXT_TTL, show_spinner=False, max_entries=64, persist="disk")
def _fetch_context(_region: Region, cache_key: tuple[str, str, int]) -> FeedBundle:
    """Slowly-changing feeds (OISST baseline + OBIS + infrastructure).
    ``cache_key`` is ``(code, UTC-date, retry-generation)`` — the date changes
    daily and drives invalidation; the generation bumps when a cached bundle
    carries feed errors (see ``_schedule_context_retry``); the long TTL is a
    memory-bound backstop. Deliberately not keyed by nonce: REFRESH refreshes
    live conditions, not the stable baseline.

    ``persist="disk"`` is what makes a restart cheap. This tier is the slow one
    (Overpass + OISST, several seconds cold) but is stable within a UTC day, so
    Streamlit's on-disk cache lets the *same day's* result survive a process
    restart instead of re-fetching it — the date in the key still forces a fresh
    pull the next day, and the retry generation still forces one after a failure.
    The dynamic tier is deliberately NOT persisted: it is meant to be fresh.
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
    # Each tier supplies exactly the feeds it owns, so a feed added to
    # DYNAMIC_FEEDS / CONTEXT_FEEDS flows through without touching this merge.
    feed_values = {name: getattr(dynamic, name) for name in DYNAMIC_FEEDS}
    feed_values.update({name: getattr(context, name) for name in CONTEXT_FEEDS})
    merged = FeedBundle(
        feeds_requested=[*context.feeds_requested, *dynamic.feeds_requested],
        errors={**context.errors, **dynamic.errors},
        telemetry=telemetry,
        duration_ms=duration,
        fetched_at=dynamic.fetched_at,
        **feed_values,
    )
    snapshot = snapshot_from_bundle(region, merged)
    return snapshot, assess_region(snapshot)


def _remember(code: str, snapshot: RegionSnapshot, assessment: StressAssessment) -> None:
    """Store as last-known-good (memory + disk), bounded, and only when the
    result actually carries data — so an outage never poisons the cache.

    A scoreless snapshot is never remembered: "last known good" exists to keep
    a populated dashboard on screen during an outage, and a result that could
    not produce a score would overwrite one that did."""
    if assessment.score is None or snapshot.sources_ok < _MIN_SOURCES_TO_PERSIST:
        return
    _LAST_GOOD.pop(code, None)  # re-insert at end for true LRU eviction
    _LAST_GOOD[code] = (snapshot, assessment)
    while len(_LAST_GOOD) > MAX_CACHED_LOCATIONS:
        _LAST_GOOD.pop(next(iter(_LAST_GOOD)))
    _persist(code, snapshot, assessment)
    _append_history(code, assessment)


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
        context = _fetch_context(
            resolved, (code, day_key, _context_generation(code))
        )
        _schedule_context_retry(code, context)
        snapshot, assessment = _compose(resolved, dynamic, context)
        _remember(code, snapshot, assessment)
        return LoadResult(snapshot, assessment, "live")
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("live load failed for %s; falling back (%s)", code, reason)

    remembered = _LAST_GOOD.get(code)
    if remembered is not None:
        return LoadResult(remembered[0], remembered[1], "memory", reason)

    restored = _restore(code)
    if restored is not None:
        _LAST_GOOD[code] = restored
        return LoadResult(restored[0], restored[1], "disk", reason)

    return LoadResult(None, None, "none", reason)
