"""Place-name geocoding for free-form location search.

Turns a typed query into coordinates so the dashboard is not limited to the
seven curated regions. Two resolution paths:

* **Raw coordinates** — ``"36.75, -122.0"`` (and similar) are parsed directly.
  This is the escape hatch that matters: geocoders index the ocean poorly, so
  for an offshore point the reliable input is the latitude/longitude itself.
* **Place names** — resolved through OpenStreetMap's Nominatim service, which
  handles marine and natural features (bays, seas, capes) better than a
  populated-places index does.

Nominatim's usage policy is strict and honoured here: a descriptive
``User-Agent`` is always sent, and calls are throttled to at most one per
second process-wide. Callers are expected to cache results (the Streamlit page
wraps :func:`geocode` in ``st.cache_data``) so repeat lookups do not hit the
service at all.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Final

import httpx
from pydantic import BaseModel

__all__ = ["GeocodeResult", "geocode", "parse_latlon", "GeocodingError"]

NOMINATIM_URL: Final[str] = "https://nominatim.openstreetmap.org/search"
USER_AGENT: Final[str] = (
    "MarineEcosystemHealthIndex/1.0 (location search; contact via project repository)"
)
MIN_INTERVAL_SECONDS: Final[float] = 1.1  # Nominatim asks for <= 1 req/sec.

_rate_lock = threading.Lock()
_last_call = 0.0


class GeocodingError(RuntimeError):
    """Raised when a place-name lookup cannot be completed."""


class GeocodeResult(BaseModel):
    """One resolved location."""

    display_name: str
    latitude: float
    longitude: float
    category: str | None = None
    kind: str | None = None
    importance: float | None = None
    #: OSM bounding box as (south, north, west, east) when the service supplies
    #: one. Marine features often lack it, so it is advisory only.
    osm_bbox: tuple[float, float, float, float] | None = None

    @property
    def short_name(self) -> str:
        return self.display_name.split(",")[0].strip() or self.display_name

    @property
    def looks_marine(self) -> bool:
        """Heuristic: does OSM classify this as a water feature?"""
        marine_kinds = {
            "sea", "bay", "ocean", "strait", "gulf", "sound", "channel",
            "lagoon", "reef", "cape", "water", "wetland", "beach", "shoal",
        }
        return (self.kind or "").lower() in marine_kinds or (
            self.category or ""
        ).lower() in {"natural", "water", "waterway"}


# --------------------------------------------------------------------------- #
# Raw coordinate parsing
# --------------------------------------------------------------------------- #
# "36.75, -122.0" | "36.75 -122.0" | "36.75N 122.0W" | "-33.9, 151.2"
_COORD_PAIR = re.compile(
    r"""^\s*
        (?P<lat>[-+]?\d{1,2}(?:\.\d+)?)\s*(?P<lat_hemi>[NnSs])?\s*
        [ ,;/]+\s*
        (?P<lon>[-+]?\d{1,3}(?:\.\d+)?)\s*(?P<lon_hemi>[EeWw])?\s*
    $""",
    re.VERBOSE,
)


def parse_latlon(query: str) -> tuple[float, float] | None:
    """Parse a raw coordinate pair, or return None if the text is not one.

    Accepts decimal degrees with optional hemisphere letters. Latitude is
    validated to [-90, 90] and longitude to [-180, 180]; anything outside those
    ranges (or ambiguous) returns None so the caller falls back to place-name
    lookup.
    """
    match = _COORD_PAIR.match(query or "")
    if not match:
        return None
    try:
        lat = float(match.group("lat"))
        lon = float(match.group("lon"))
    except ValueError:
        return None

    if (match.group("lat_hemi") or "").upper() == "S":
        lat = -abs(lat)
    if (match.group("lon_hemi") or "").upper() == "W":
        lon = -abs(lon)

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return lat, lon


# --------------------------------------------------------------------------- #
# Place-name lookup
# --------------------------------------------------------------------------- #
def _throttle() -> None:
    """Block until at least MIN_INTERVAL_SECONDS has passed since the last call."""
    global _last_call
    with _rate_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def geocode(query: str, *, limit: int = 5, timeout: float = 20.0) -> list[GeocodeResult]:
    """Resolve a place name to candidate locations, best match first.

    Raises :class:`GeocodingError` on transport failure or a non-200 response so
    the caller can distinguish "service unreachable" from "no such place" (an
    empty list). Does not itself parse raw coordinates — call :func:`parse_latlon`
    first for that.
    """
    query = (query or "").strip()
    if not query:
        return []

    _throttle()
    try:
        response = httpx.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": max(1, min(limit, 10)),
                "addressdetails": 0,
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise GeocodingError(f"geocoder unreachable: {type(exc).__name__}") from exc

    if response.status_code != 200:
        raise GeocodingError(f"geocoder returned HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise GeocodingError("geocoder returned a non-JSON response") from exc

    results: list[GeocodeResult] = []
    for item in payload:
        try:
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue

        bbox: tuple[float, float, float, float] | None = None
        raw_bbox = item.get("boundingbox")
        if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
            try:
                south, north, west, east = (float(v) for v in raw_bbox)
                bbox = (south, north, west, east)
            except (TypeError, ValueError):
                bbox = None

        results.append(
            GeocodeResult(
                display_name=str(item.get("display_name", query)),
                latitude=lat,
                longitude=lon,
                category=item.get("category") or item.get("class"),
                kind=item.get("type"),
                importance=(
                    float(item["importance"]) if "importance" in item else None
                ),
                osm_bbox=bbox,
            )
        )
    return results


if __name__ == "__main__":  # pragma: no cover - manual check
    import sys

    q = " ".join(sys.argv[1:]) or "Monterey Bay"
    coords = parse_latlon(q)
    if coords:
        print(f"raw coordinates: {coords[0]:.4f}, {coords[1]:.4f}")
    else:
        for r in geocode(q):
            marine = " [marine]" if r.looks_marine else ""
            print(f"{r.latitude:9.4f}, {r.longitude:9.4f}  {r.short_name}{marine} "
                  f"({r.category}/{r.kind})")
