"""Offline tests for the disk cache: atomic writes, round-trip, bounded eviction.

Exercises the persistence layer that keeps the dashboard alive across restarts,
using a temporary cache directory so the real .cache/ is untouched. No network.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data_access  # noqa: E402
from analyzer import StressAssessment, assess_region  # noqa: E402
from api_clients import (  # noqa: E402
    BuoySnapshot, ClimatologySnapshot, FeedBundle, InfrastructureSnapshot,
    ObisSnapshot, Region, RegionSnapshot, SeaStateSnapshot,
)


def _make(code_name: str) -> tuple[RegionSnapshot, StressAssessment]:
    region = Region.from_point(code_name, 10.0, 20.0)
    snap = RegionSnapshot(
        region=region, fetched_at=datetime.now(timezone.utc), duration_ms=1.0
    )
    assess = StressAssessment(region_code=region.code, region_name=region.name, score=42.0)
    return snap, assess


def _with_temp_cache(fn) -> None:
    original = data_access.CACHE_DIR
    with tempfile.TemporaryDirectory() as d:
        data_access.CACHE_DIR = Path(d)
        try:
            fn(Path(d))
        finally:
            data_access.CACHE_DIR = original


def test_persist_restore_roundtrip() -> None:
    def body(_: Path) -> None:
        snap, assess = _make("ROUNDTRIP")
        data_access._persist("ROUNDTRIP", snap, assess)
        restored = data_access._restore("ROUNDTRIP")
        assert restored is not None, "round-trip lost the snapshot"
        r_snap, r_assess = restored
        assert r_assess.score == 42.0
        assert r_snap.region.code == snap.region.code

    _with_temp_cache(body)


def test_atomic_write_leaves_no_temp_files() -> None:
    def body(d: Path) -> None:
        snap, assess = _make("ATOMIC")
        data_access._persist("ATOMIC", snap, assess)
        leftovers = list(d.glob("*.tmp-*"))
        assert not leftovers, f"atomic write left temp files: {leftovers}"

    _with_temp_cache(body)


def test_eviction_caps_location_count() -> None:
    def body(d: Path) -> None:
        for i in range(8):
            snap, assess = _make(f"R{i}")
            data_access._persist(f"R{i}", snap, assess)
        # Keep only the 3 most recent locations.
        data_access._evict_old(keep=3)
        snapshots = list(d.glob("last_good_*_snapshot.json"))
        assessments = list(d.glob("last_good_*_assessment.json"))
        assert len(snapshots) == 3, f"expected 3 snapshots, found {len(snapshots)}"
        assert len(assessments) == 3, f"assessment files not evicted with snapshots"

    _with_temp_cache(body)


def test_restore_missing_returns_none() -> None:
    def body(_: Path) -> None:
        assert data_access._restore("NEVER_WRITTEN") is None

    _with_temp_cache(body)


def test_compose_merges_tiers_and_times() -> None:
    """The two volatility tiers compose into one snapshot; wall clock and
    fetched_at reflect the live (dynamic) tier, not the cached context tier."""
    region = Region.from_point("Compose", 36.8, -121.9)
    now = datetime.now(timezone.utc)
    dynamic = FeedBundle(
        sea_state=SeaStateSnapshot(latitude=36.8, longitude=-121.9, sst_c=[15.0]),
        buoy=BuoySnapshot(status="live", water_temp_c=15.1),
        duration_ms=5_000.0, fetched_at=now,
    )
    context = FeedBundle(
        climatology=ClimatologySnapshot(observations=100, baseline_mean=14.0,
                                        baseline_std=0.5, years_covered=10),
        obis=ObisSnapshot(records=100, species=10, taxa=10, datasets=1,
                          species_level_records=50, year_min=2000, year_max=2024),
        infrastructure=InfrastructureSnapshot(total_count=5, area_km2=1000.0),
        duration_ms=70_000.0, fetched_at=now - timedelta(hours=3),  # cached
    )
    snap, _ = data_access._compose(region, dynamic, context)

    assert snap.sea_state is dynamic.sea_state and snap.buoy is dynamic.buoy
    assert snap.climatology is context.climatology
    assert snap.obis is context.obis and snap.infrastructure is context.infrastructure
    assert snap.sources_ok == 5
    assert snap.fetched_at == now, "fetched_at should track the live tier"
    # Context ran 3h ago, so it is not counted in this cycle's wall clock.
    assert abs(snap.duration_ms - 5_000.0) < 1e-6


def test_remember_skips_empty_result() -> None:
    """An all-empty outage result must not overwrite last-known-good on disk."""
    def body(_: Path) -> None:
        region = Region.from_point("Empty", 0.0, 0.0)
        empty = RegionSnapshot(region=region, fetched_at=datetime.now(timezone.utc),
                               duration_ms=0.0)  # every feed None
        assessment = assess_region(empty)  # score is None
        assert assessment.score is None and empty.sources_ok == 0

        data_access._LAST_GOOD.pop("Empty", None)
        data_access._remember("Empty", empty, assessment)
        assert "Empty" not in data_access._LAST_GOOD, "empty result poisoned memory"
        assert data_access._restore("Empty") is None, "empty result poisoned disk"

    _with_temp_cache(body)


def test_remember_skips_scoreless_result_with_live_feeds() -> None:
    """A snapshot with feeds present but no scorable component must not
    overwrite a last-known-good that *did* score."""
    def body(_: Path) -> None:
        region = Region.from_point("Scoreless", 5.0, 5.0)
        code = "Scoreless"

        good_snap = RegionSnapshot(
            region=region, fetched_at=datetime.now(timezone.utc), duration_ms=1.0,
            sea_state=SeaStateSnapshot(latitude=5.0, longitude=5.0, current_sst_c=20.0),
            buoy=BuoySnapshot(status="live", water_temp_c=20.1),
        )
        good = StressAssessment(region_code=code, region_name=region.name, score=55.0)
        data_access._LAST_GOOD.pop(code, None)
        data_access._remember(code, good_snap, good)
        assert code in data_access._LAST_GOOD, "a scored result should persist"

        # Two feeds arrive, but nothing the analyzer can score.
        partial = RegionSnapshot(
            region=region, fetched_at=datetime.now(timezone.utc), duration_ms=1.0,
            sea_state=SeaStateSnapshot(latitude=5.0, longitude=5.0),
            buoy=BuoySnapshot(status="partial"),
        )
        assert partial.sources_ok >= 2, "fixture should carry two live feeds"
        scoreless = StressAssessment(region_code=code, region_name=region.name,
                                     score=None)
        data_access._remember(code, partial, scoreless)

        kept = data_access._LAST_GOOD[code][1]
        assert kept.score == 55.0, (
            f"scoreless result overwrote last-known-good (score now {kept.score})"
        )

    _with_temp_cache(body)


def test_load_region_falls_back_to_memory_then_disk() -> None:
    """A failed fetch must serve the last good result and flag it stale."""
    def body(_: Path) -> None:
        region = Region.from_point("Fallback", 1.0, 2.0)
        code = region.code

        snap, assess = _make("Fallback")
        snap = RegionSnapshot(region=region, fetched_at=datetime.now(timezone.utc),
                              duration_ms=1.0)
        assess = StressAssessment(region_code=code, region_name=region.name, score=33.0)

        original = data_access._fetch_dynamic

        def explode(_region, _key):  # noqa: ANN001, ANN202
            raise RuntimeError("upstream down")

        data_access._fetch_dynamic = explode
        try:
            # 1. Memory path.
            data_access._LAST_GOOD[code] = (snap, assess)
            result = data_access.load_region(region)
            assert result.ok and result.stale, "should serve stale data, not blank"
            assert result.origin == "memory"
            assert result.assessment.score == 33.0
            assert "upstream down" in (result.error or ""), (
                "the failure reason should be reported, not hidden"
            )

            # 2. Disk path, after the process 'restarts' (memory cleared).
            data_access._persist(code, snap, assess)
            data_access._LAST_GOOD.pop(code, None)
            restored = data_access.load_region(region)
            assert restored.ok and restored.origin == "disk", (
                f"expected disk fallback, got {restored.origin}"
            )

            # 3. Nothing ever cached -> explicit empty result, still no raise.
            other = Region.from_point("NeverSeen", 40.0, 40.0)
            data_access._LAST_GOOD.pop(other.code, None)
            empty = data_access.load_region(other)
            assert not empty.ok and empty.origin == "none"
            assert empty.error, "an empty result must still carry the reason"
        finally:
            data_access._fetch_dynamic = original
            data_access._LAST_GOOD.pop(code, None)

    _with_temp_cache(body)


def test_context_tier_retries_after_a_failed_feed() -> None:
    """Regression: a transient context-feed failure used to be cached under the
    (code, UTC-date) key for the rest of the day. The retry generation must
    change so the next load re-fetches."""
    code = "RetryRegion"
    data_access._CONTEXT_RETRY.pop(code, None)

    healthy = FeedBundle(
        climatology=ClimatologySnapshot(observations=10, baseline_mean=14.0),
        obis=ObisSnapshot(records=1, species=1, taxa=1, datasets=1,
                          species_level_records=1, year_min=2000, year_max=2024),
        infrastructure=InfrastructureSnapshot(total_count=1, area_km2=100.0),
    )
    before = data_access._context_generation(code)
    data_access._schedule_context_retry(code, healthy)
    assert data_access._context_generation(code) == before, (
        "a healthy context bundle must not trigger a re-fetch"
    )

    degraded = FeedBundle(errors={"climatology": "ApiError: HTTP 504"})
    data_access._schedule_context_retry(code, degraded)
    after = data_access._context_generation(code)
    assert after != before, (
        "a context bundle carrying feed errors must bump the retry generation "
        "so it is not cached for the rest of the UTC day"
    )

    # Bumps are rate-limited: an immediate second failure must not bump again.
    data_access._schedule_context_retry(code, degraded)
    assert data_access._context_generation(code) == after, (
        "repeated failures within one cycle should not re-fetch every load"
    )

    # Recovery clears the retry state.
    data_access._schedule_context_retry(code, healthy)
    assert code not in data_access._CONTEXT_RETRY, (
        "a recovered context tier should stop forcing re-fetches"
    )


def test_history_appends_rate_limits_and_prunes() -> None:
    """History must accumulate (unlike the last-known-good mirror, which
    overwrites), skip near-duplicate points, and drop entries past retention."""
    def body(_: Path) -> None:
        region = Region.from_point("History", 36.0, -122.0)
        code = region.code
        snap = RegionSnapshot(
            region=region, fetched_at=datetime.now(timezone.utc), duration_ms=1.0,
            sea_state=SeaStateSnapshot(latitude=36.0, longitude=-122.0,
                                       current_sst_c=15.0),
            buoy=BuoySnapshot(status="live", water_temp_c=15.0),
        )
        assess = StressAssessment(region_code=code, region_name=region.name,
                                  score=61.0, band="ELEVATED", confidence=0.8)

        assert data_access.load_history(code) == [], "should start empty"

        data_access._remember(code, snap, assess)
        assert len(data_access.load_history(code)) == 1

        # A second remember moments later must not record a near-duplicate.
        data_access._remember(code, snap, assess)
        assert len(data_access.load_history(code)) == 1, (
            "history should be rate-limited, not one row per render"
        )

        # Seed one in-window and one expired entry, then append.
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=3)
        expired = now - timedelta(days=data_access.HISTORY_RETENTION_DAYS + 5)
        data_access._atomic_write(
            data_access._history_path(code),
            json.dumps({"at": expired.isoformat(), "score": 10.0,
                        "band": "LOW", "confidence": 0.5}) + "\n"
            + json.dumps({"at": recent.isoformat(), "score": 55.0,
                          "band": "ELEVATED", "confidence": 0.7}) + "\n",
        )
        data_access._remember(code, snap, assess)

        scores = [e["score"] for e in data_access.load_history(code)]
        assert 10.0 not in scores, "an entry past retention should be pruned"
        assert scores == [55.0, 61.0], f"unexpected history {scores}"

    _with_temp_cache(body)


def test_history_ignores_scoreless_and_torn_lines() -> None:
    def body(_: Path) -> None:
        region = Region.from_point("Torn", 1.0, 1.0)
        code = region.code
        data_access._atomic_write(
            data_access._history_path(code),
            json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                        "score": 44.0, "band": "MODERATE", "confidence": 0.6}) + "\n"
            + "{not valid json\n"
            + json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                          "score": None}) + "\n",
        )
        entries = data_access.load_history(code)
        assert len(entries) == 1, (
            f"a torn line or scoreless row should be skipped, got {entries}"
        )
        assert entries[0]["score"] == 44.0

    _with_temp_cache(body)


def _instrument_fetch(monkey_delay: float = 0.25):
    """Replace data_access.fetch_feeds with a counter that records how many
    fan-outs run and the peak concurrency. Returns (stats, restore)."""
    stats = {"calls": 0, "concurrent": 0, "max": 0}
    lock = threading.Lock()
    saved = data_access.fetch_feeds

    async def fake(region, feeds, **kw):
        with lock:
            stats["calls"] += 1
            stats["concurrent"] += 1
            stats["max"] = max(stats["max"], stats["concurrent"])
        time.sleep(monkey_delay)
        with lock:
            stats["concurrent"] -= 1
        return FeedBundle(
            feeds_requested=list(feeds),
            fetched_at=datetime.now(timezone.utc),
        )

    data_access.fetch_feeds = fake
    return stats, (lambda: setattr(data_access, "fetch_feeds", saved))


def test_single_flight_collapses_same_key_stampede() -> None:
    """N concurrent cold loads of the SAME region must fan out once (per tier),
    not N times — the shared-cache thundering-herd guard."""
    stats, restore = _instrument_fetch()
    region = Region.from_point("Stampede", 12.0, 34.0)
    # Fresh caches so this is a genuine cold miss.
    data_access._fetch_dynamic.clear()
    data_access._fetch_context.clear()
    try:
        threads = [
            threading.Thread(target=lambda: data_access.load_region(region))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Two tiers (dynamic + context), one fan-out each.
        assert stats["calls"] == 2, (
            f"8 concurrent same-region loads triggered {stats['calls']} fetches; "
            "single-flight should collapse them to 2 (dynamic + context)"
        )
    finally:
        restore()


def test_global_cap_bounds_distinct_key_concurrency() -> None:
    """A burst of DISTINCT cold searches must not open more than the configured
    number of concurrent upstream fan-outs."""
    stats, restore = _instrument_fetch()
    saved_gate = data_access._FETCH_GATE
    data_access._FETCH_GATE = threading.BoundedSemaphore(3)
    data_access._fetch_dynamic.clear()
    data_access._fetch_context.clear()
    try:
        regions = [Region.from_point(f"D{i}", 10.0 + i, 20.0) for i in range(12)]
        threads = [
            threading.Thread(target=lambda rr=rr: data_access.load_region(rr))
            for rr in regions
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert stats["max"] <= 3, (
            f"peak concurrent fan-outs was {stats['max']}, exceeding the cap of 3"
        )
        assert stats["max"] >= 2, "the cap should still allow real concurrency"
    finally:
        data_access._FETCH_GATE = saved_gate
        restore()


def test_bump_refresh_advances_the_global_generation() -> None:
    before = data_access.refresh_generation()
    after = data_access.bump_refresh()
    assert after == before + 1
    assert data_access.refresh_generation() == after


ALL = [
    test_persist_restore_roundtrip,
    test_atomic_write_leaves_no_temp_files,
    test_eviction_caps_location_count,
    test_restore_missing_returns_none,
    test_compose_merges_tiers_and_times,
    test_remember_skips_empty_result,
    test_remember_skips_scoreless_result_with_live_feeds,
    test_load_region_falls_back_to_memory_then_disk,
    test_context_tier_retries_after_a_failed_feed,
    test_history_appends_rate_limits_and_prunes,
    test_history_ignores_scoreless_and_torn_lines,
    test_single_flight_collapses_same_key_stampede,
    test_global_cap_bounds_distinct_key_concurrency,
    test_bump_refresh_advances_the_global_generation,
]


if __name__ == "__main__":
    failures = 0
    for fn in ALL:
        try:
            fn()
            print(f"[ OK ] {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[ERR ] {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL) - failures}/{len(ALL)} passed")
    sys.exit(1 if failures else 0)
