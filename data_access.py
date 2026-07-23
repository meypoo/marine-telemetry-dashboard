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
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from analyzer import StressAssessment, assess_region
from api_clients import REGIONS_BY_CODE, Region, RegionSnapshot, fetch_region_snapshot

__all__ = ["LoadResult", "load_region", "CACHE_TTL_SECONDS"]

#: How long a successful fetch is reused before the next one is attempted.
CACHE_TTL_SECONDS = 600

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


def _persist(region_code: str, snapshot: RegionSnapshot,
             assessment: StressAssessment) -> None:
    """Mirror a good result to disk. Never raises — persistence is best-effort."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        snap_path, assess_path = _paths(region_code)
        snap_path.write_text(snapshot.model_dump_json(), encoding="utf-8")
        assess_path.write_text(assessment.model_dump_json(), encoding="utf-8")
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
# Fetch
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=32)
def _fetch(
    _region: Region, cache_key: tuple[str, int]
) -> tuple[RegionSnapshot, StressAssessment]:
    """Live fetch + score. Cached by ``cache_key``; ``_region`` is excluded from
    the hash (leading underscore) because it is the same for a given code.
    ``cache_key`` is ``(region.code, nonce)`` — nonce forces a bypass on manual
    refresh.
    """
    snapshot = asyncio.run(fetch_region_snapshot(_region))
    return snapshot, assess_region(snapshot)


def load_region(region: Region | str, nonce: int = 0) -> LoadResult:
    """Load a region, degrading to the last good result instead of raising.

    Accepts either a curated region code (looked up in ``REGIONS_BY_CODE``) or a
    ready-made :class:`~api_clients.Region` — the latter is how searched
    locations flow through the same caching, persistence and fallback path as
    the built-in regions.

    A partial fetch (some sources down) still counts as success — the analyzer
    redistributes weights and reports the degradation. Only a total failure to
    produce a snapshot falls back to cached data.
    """
    resolved = REGIONS_BY_CODE[region] if isinstance(region, str) else region
    code = resolved.code

    try:
        snapshot, assessment = _fetch(resolved, (code, nonce))
        _LAST_GOOD[code] = (snapshot, assessment)
        _persist(code, snapshot, assessment)
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
