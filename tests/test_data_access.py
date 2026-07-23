"""Offline tests for the disk cache: atomic writes, round-trip, bounded eviction.

Exercises the persistence layer that keeps the dashboard alive across restarts,
using a temporary cache directory so the real .cache/ is untouched. No network.
"""

from __future__ import annotations

import sys
import tempfile
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


ALL = [
    test_persist_restore_roundtrip,
    test_atomic_write_leaves_no_temp_files,
    test_eviction_caps_location_count,
    test_restore_missing_returns_none,
    test_compose_merges_tiers_and_times,
    test_remember_skips_empty_result,
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
