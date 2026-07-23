"""Tests for location parsing and geocoding.

The `parse_latlon` tests are offline and always run. The live Nominatim lookup
is gated behind MEHI_LIVE_TESTS=1 so the default suite needs no network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geocoding import GeocodingError, geocode, parse_latlon  # noqa: E402


def test_parse_decimal_pair() -> None:
    assert parse_latlon("36.75, -122.0") == (36.75, -122.0)
    assert parse_latlon("36.75 -122.0") == (36.75, -122.0)
    assert parse_latlon("-33.9, 151.2") == (-33.9, 151.2)


def test_parse_hemisphere_letters() -> None:
    lat, lon = parse_latlon("36.75N 122.0W")
    assert lat == 36.75 and lon == -122.0
    lat, lon = parse_latlon("33.9S, 151.2E")
    assert lat == -33.9 and lon == 151.2


def test_parse_rejects_non_coordinates() -> None:
    assert parse_latlon("Monterey Bay") is None
    assert parse_latlon("") is None
    assert parse_latlon("100, 200") is None       # out of range
    assert parse_latlon("45") is None             # single number


def test_parse_out_of_range() -> None:
    assert parse_latlon("91.0, 10.0") is None
    assert parse_latlon("45.0, 200.0") is None


def test_live_geocode() -> None:
    if os.environ.get("MEHI_LIVE_TESTS") != "1":
        print("      (skipped — set MEHI_LIVE_TESTS=1 to run the live lookup)")
        return
    try:
        results = geocode("Monterey Bay")
    except GeocodingError as exc:
        print(f"      (geocoder unreachable: {exc})")
        return
    assert results, "no results for a well-known bay"
    assert any(r.looks_marine for r in results), "expected a marine feature match"


ALL = [
    test_parse_decimal_pair,
    test_parse_hemisphere_letters,
    test_parse_rejects_non_coordinates,
    test_parse_out_of_range,
    test_live_geocode,
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
