"""Offline tests for region synthesis and marine-coverage classification.

No network: exercises `Region.from_point` (the capped bounding box) and
`RegionSnapshot.marine_coverage` (the land / model-only / full logic) with
hand-built snapshots.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_clients import (  # noqa: E402
    BuoySnapshot, ClimatologySnapshot, Region, RegionSnapshot, SeaStateSnapshot,
)


def test_from_point_caps_area() -> None:
    # A deliberately huge half-extent must still yield a capped box.
    region = Region.from_point("Big", 0.0, 0.0, half_deg=5.0, max_area_km2=12_000.0)
    assert region.area_km2 <= 12_000.0, f"area {region.area_km2:.0f} exceeds cap"
    assert region.origin == "search"
    assert region.buoy_candidates == ()


def test_from_point_default_is_reasonable() -> None:
    region = Region.from_point("Monterey", 36.8, -121.9)
    assert 1_000 < region.area_km2 < 12_000
    lat, lon = region.centroid
    assert abs(lat - 36.8) < 0.01 and abs(lon + 121.9) < 0.01


def test_from_point_normalises_longitude() -> None:
    region = Region.from_point("Dateline", 0.0, 190.0)
    _, lon = region.centroid
    assert -180.0 <= lon <= 180.0, f"longitude {lon} not normalised"


def test_from_point_code_is_filesystem_safe() -> None:
    region = Region.from_point("X", 36.75, -122.0)
    assert region.code == "@36.750,-122.000"
    assert "@" not in region.filesystem_code and "," not in region.filesystem_code


def _snapshot(*, clim, sea, buoy) -> RegionSnapshot:
    return RegionSnapshot(
        region=Region.from_point("T", 0.0, 0.0),
        fetched_at=datetime.now(timezone.utc),
        duration_ms=0.0,
        climatology=clim,
        sea_state=sea,
        buoy=buoy,
    )


def test_coverage_none_for_land() -> None:
    # Both feeds succeeded but returned empty: the inland all-null shape.
    clim = ClimatologySnapshot(observations=0, years_covered=0)
    sea = SeaStateSnapshot(latitude=0, longitude=0, sst_c=[None, None, None])
    snap = _snapshot(clim=clim, sea=sea, buoy=BuoySnapshot(status="offline"))
    assert snap.marine_coverage == "none"


def test_coverage_model_only_without_buoy() -> None:
    clim = ClimatologySnapshot(observations=200, baseline_mean=14.0, years_covered=10)
    sea = SeaStateSnapshot(latitude=0, longitude=0, sst_c=[14.0, 14.1, 14.2])
    snap = _snapshot(clim=clim, sea=sea, buoy=BuoySnapshot(status="offline"))
    assert snap.marine_coverage == "model_only"


def test_coverage_full_with_live_buoy() -> None:
    clim = ClimatologySnapshot(observations=200, baseline_mean=14.0, years_covered=10)
    sea = SeaStateSnapshot(latitude=0, longitude=0, sst_c=[14.0, 14.1])
    buoy = BuoySnapshot(status="live", water_temp_c=15.1)
    snap = _snapshot(clim=clim, sea=sea, buoy=buoy)
    assert snap.marine_coverage == "full"


def test_coverage_unknown_on_transient_failure() -> None:
    # Feeds that raised are None, not empty — a transient failure, not land.
    snap = _snapshot(clim=None, sea=None, buoy=None)
    assert snap.marine_coverage == "unknown"


ALL = [
    test_from_point_caps_area,
    test_from_point_default_is_reasonable,
    test_from_point_normalises_longitude,
    test_from_point_code_is_filesystem_safe,
    test_coverage_none_for_land,
    test_coverage_model_only_without_buoy,
    test_coverage_full_with_live_buoy,
    test_coverage_unknown_on_transient_failure,
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
