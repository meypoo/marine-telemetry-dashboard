"""Regenerate the ``CURATED_MMM`` constants in ``api_clients.py``.

MMM — the climatological maximum monthly mean — is a *static* property of a
location, so the curated regions carry theirs as committed constants and never
pay the climatology fetch at runtime. Run this only when a curated region's
bounding box moves (which moves its centroid, and therefore its grid cell).

    python tools/precompute_mmm.py

Prints a ready-to-paste ``CURATED_MMM`` block. Takes a couple of minutes: one
strided request per region against the fixed 1985-2012 OISST climatology.

The baseline is fixed, not rolling, and that is the whole point — see the
"MMM baseline must be fixed" note in CLAUDE.md. A rolling window folds recent
warming into the reference it is measured against and silently zeroes DHW
through real bleaching events.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from api_clients import (
    MMM_BASELINE_END,
    MMM_BASELINE_START,
    MMM_STRIDE_DAYS,
    REGIONS,
    USER_AGENT,
    ErddapClient,
    TelemetrySink,
)


async def main() -> int:
    results: dict[str, tuple[float, int] | None] = {}
    async with httpx.AsyncClient(
        timeout=300.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        erddap = ErddapClient(client, TelemetrySink())
        print(
            f"fixed baseline {MMM_BASELINE_START} .. {MMM_BASELINE_END}, "
            f"stride {MMM_STRIDE_DAYS}d\n"
        )
        print(f"{'code':6s} {'lat':>8s} {'lon':>9s} {'pts':>6s} {'MMM':>7s} {'mon':>4s}  secs")
        for region in REGIONS:
            lat, lon = region.centroid
            started = time.time()
            try:
                rows = await erddap._sst_series(
                    lat, lon, MMM_BASELINE_START, MMM_BASELINE_END,
                    label=f"oisst/mmm/{region.code}",
                    stride=MMM_STRIDE_DAYS, timeout=240.0,
                )
            except Exception as exc:  # noqa: BLE001 - operator-facing script
                print(f"{region.code:6s} FAILED {type(exc).__name__}: {exc}")
                results[region.code] = None
                continue

            if not rows:
                # Every value null is the land-cell signature, not a cold ocean.
                print(f"{region.code:6s} {lat:8.3f} {lon:9.3f} "
                      f"{0:6d}  ALL-NULL (land cell?)")
                results[region.code] = None
                continue

            monthly: dict[int, list[float]] = defaultdict(list)
            for day, value, _, _ in rows:
                monthly[day.month].append(value)
            means = {m: sum(v) / len(v) for m, v in monthly.items()}
            month = max(means, key=lambda m: means[m])
            mmm = round(means[month], 2)
            results[region.code] = (mmm, month)
            print(f"{region.code:6s} {lat:8.3f} {lon:9.3f} {len(rows):6d} "
                  f"{mmm:7.2f} {month:4d}  {time.time() - started:.0f}")

    print("\nCURATED_MMM: Final[dict[str, tuple[float, int]]] = {")
    for code, value in results.items():
        if value is None:
            print(f'    # "{code}": unavailable — rerun')
        else:
            print(f'    "{code}": ({value[0]}, {value[1]}),')
    print("}")
    return 1 if any(v is None for v in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
