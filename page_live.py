"""Live Index page — the Ecological Stress Score terminal.

Thin by design: it resolves config, renders the top bar's live controls
(including free-form location search), loads the region, and hands everything to
``terminal_render`` which produces the dashboard body as a single HTML/SVG block.

The whole page sits inside an ``st.fragment`` on a timer, so it refreshes itself
without anyone clicking anything — the dashboard is meant to be left running.

Location resolution has three inputs, in precedence order: a raw ``lat, lon``
pair typed into the search box (the reliable path for offshore points), a place
name resolved through the geocoder, or — when the box is empty — the curated
region selector. A searched location flows through exactly the same fetch,
cache and fallback path as a built-in region.
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from api_clients import REGIONS, Region
from data_access import CACHE_TTL_SECONDS, load_region
from geocoding import (
    GeocodeResult, GeocodingError, geocode, marine_query_variants, parse_latlon,
)
from terminal_render import (
    location_subtitle, render_body, render_footer, render_topbar_left,
)
from ui import (
    CANOPY, LINE, PAPER_DIM, SIGNAL, TerminalConfig, esc,
    inject_terminal_css, safe_page_link,
)

config = TerminalConfig.from_query_params()
inject_terminal_css(config)

#: Default is the data cache TTL plus a margin, so each timed rerun pulls fresh
#: data rather than re-rendering the same cached snapshot. Overridable with
#: ``?refresh=<seconds>``.
AUTO_REFRESH_SECONDS = max(config.refresh_seconds, CACHE_TTL_SECONDS + 120)

if "nonce" not in st.session_state:
    st.session_state.nonce = 0


@st.cache_data(ttl=3600, show_spinner=False, max_entries=128)
def _geocode_cached(query: str) -> list[GeocodeResult]:
    """Cached place-name lookup. Respects Nominatim's rate limit only on a miss.

    Fetches several candidates so the resolver can prefer a sea feature over a
    same-name land feature."""
    return geocode(query, limit=10)


def _resolve_location(
    search_text: str, curated: Region
) -> tuple[Region, str, str | None]:
    """Turn the search box (or curated selector) into a Region.

    Returns ``(region, source, note)`` where ``source`` is one of ``curated``,
    ``coords``, ``search``, ``nomatch`` or ``error``. On ``nomatch``/``error``
    the curated region is returned so the dashboard still shows something, with
    the reason in ``note``.
    """
    text = (search_text or "").strip()
    if not text:
        return curated, "curated", None

    coords = parse_latlon(text)
    if coords is not None:
        lat, lon = coords
        name = f"{lat:.3f}, {lon:.3f}"
        return (
            Region.from_point(name, lat, lon),
            "coords",
            f"raw coordinates {lat:.3f}, {lon:.3f}",
        )

    def _sea_first(rs: list[GeocodeResult]) -> list[GeocodeResult]:
        return sorted(
            (r for r in rs if r.is_sea_feature),
            key=lambda r: r.importance or 0.0, reverse=True,
        )

    try:
        results = _geocode_cached(text)
    except GeocodingError as exc:
        return curated, "error", f"geocoder unavailable ({exc}); showing {curated.name}"

    if not results:
        return curated, "nomatch", f"no location matched “{text}”; showing {curated.name}"

    # Prefer an actual sea/ocean water body over a same-name land feature, so a
    # lake or coastal district never outranks a sea feature (is_sea_feature is
    # strict). If the raw query surfaces no water body but is phrased like a
    # water body ("bay of tokyo"), retry the canonical phrasing ("tokyo bay")
    # before falling back to the top overall result.
    matched_via: str | None = None
    sea = _sea_first(results)
    if not sea:
        for variant in marine_query_variants(text):
            try:
                alt = _geocode_cached(variant)
            except GeocodingError:
                continue
            alt_sea = _sea_first(alt)
            if alt_sea:
                results, sea, matched_via = alt, alt_sea, variant
                break

    best = sea[0] if sea else results[0]
    tag = " · water body" if best.is_sea_feature else (
        " · marine feature" if best.looks_marine else " · inland"
    )
    via = f" (matched “{matched_via}”)" if matched_via else ""
    note = f"{best.short_name} → {best.latitude:.3f}, {best.longitude:.3f}{tag}{via}"
    return Region.from_point(best.short_name, best.latitude, best.longitude), "search", note


def _banner(text: str, colour: str) -> None:
    st.markdown(
        '<div class="tm-root"><div style="padding:8px 32px;'
        f'border-bottom:1px solid {LINE};font-size:11.5px;color:{colour}">'
        f"{esc(text)}</div></div>",
        unsafe_allow_html=True,
    )


@st.fragment(run_every=AUTO_REFRESH_SECONDS)
def terminal() -> None:
    """Render the whole dashboard. Re-runs on its own timer.

    The body is guarded: an unhandled exception here would end the fragment and
    with it the refresh loop, leaving an unattended dashboard frozen until
    someone reloads. Failing visibly but staying alive is the better outcome.
    """
    try:
        _render()
    except Exception as exc:  # keep the timer alive
        st.markdown(
            '<div class="tm-root"><div class="tm-main">'
            '<div class="tm-chip">RENDER ERROR</div>'
            f'<div style="margin-top:12px;font-size:11.5px;color:{SIGNAL}">'
            f"{esc(type(exc).__name__)}: {esc(str(exc)[:300])}"
            "</div><div style=\"margin-top:8px;font-size:11.5px;color:"
            f'{PAPER_DIM}">The refresh loop is still running and will retry '
            "on the next cycle.</div></div></div>",
            unsafe_allow_html=True,
        )


def _render() -> None:
    now = datetime.now(timezone.utc)

    # Controls are rendered first so their values are known; the title column is
    # filled in last, once the location is resolved, so its subtitle can name it.
    title_col, search_col, region_col, refresh_col, nav_col = st.columns(
        [0.40, 0.24, 0.15, 0.11, 0.10]
    )
    with search_col:
        search_text = st.text_input(
            "SEARCH",
            key="location_search",
            placeholder="place name or lat, lon",
            label_visibility="collapsed",
        )
    with region_col:
        selected_name = st.selectbox(
            "REGION", options=[r.name for r in REGIONS], index=0,
            label_visibility="collapsed",
            help="Built-in regions. Type in the search box to go anywhere else.",
        )
    with refresh_col:
        if st.button("REFRESH", use_container_width=True):
            st.session_state.nonce += 1
    with nav_col:
        safe_page_link("page_live.py", "LIVE INDEX")
        safe_page_link("page_lab.py", "DATA LAB")

    curated = next(r for r in REGIONS if r.name == selected_name)
    region, source, note = _resolve_location(search_text, curated)
    result = load_region(region, st.session_state.nonce)
    coverage = result.snapshot.marine_coverage if result.ok else "unknown"

    with title_col:
        st.markdown(
            render_topbar_left(
                config, location_subtitle(region, coverage), f"{now:%Y-%m-%d %H:%M:%S}Z"
            ),
            unsafe_allow_html=True,
        )

    st.markdown(
        f'<div style="border-bottom:1px solid {LINE};margin-bottom:0"></div>',
        unsafe_allow_html=True,
    )

    # Resolution note (what the search box became).
    if note:
        colour = SIGNAL if source in {"nomatch", "error"} else CANOPY
        prefix = "SEARCH" if source in {"search", "coords"} else "NOTE"
        _banner(f"{prefix}: {note}", colour)

    if not result.ok:
        st.markdown(
            '<div class="tm-root"><div class="tm-main">'
            f'<div class="tm-chip">DATA UNAVAILABLE</div>'
            '<div style="margin-top:12px;font-size:12px;color:' + PAPER_DIM + '">'
            "No live fetch succeeded and no cached snapshot exists for this location yet. "
            "The dashboard will retry automatically on the next refresh cycle."
            "</div>"
            f'<div style="margin-top:8px;font-size:11.5px;color:{SIGNAL}">'
            f"{esc(result.error or 'unknown error')}</div>"
            "</div></div>",
            unsafe_allow_html=True,
        )
        return

    if result.stale:
        minutes = result.age_seconds / 60.0
        _banner(
            f"SHOWING LAST GOOD DATA ({minutes:,.0f} min old, from {result.origin}) "
            f"— live refresh failed: {(result.error or '')[:160]}",
            SIGNAL,
        )

    # Coverage state: distinguish "no marine data here" from a transient failure.
    # (coverage was computed above for the subtitle.)
    if coverage == "none":
        st.markdown(
            '<div class="tm-root"><div class="tm-main">'
            '<div class="tm-chip">NO MARINE DATA AT THIS LOCATION</div>'
            '<div style="margin-top:12px;font-size:12px;color:' + PAPER_DIM + '">'
            "This point is inland, or otherwise outside ocean coverage: the NOAA OISST "
            "baseline and the Open-Meteo model both returned empty sea-surface "
            "temperature. Thermal scoring is therefore unavailable here. The "
            "biodiversity and maritime-infrastructure panels below still describe the "
            "surrounding area, but the stress score is not a marine reading at this "
            "location — search a coastal or offshore point instead."
            "</div></div></div>",
            unsafe_allow_html=True,
        )
    elif coverage == "model_only":
        _banner(
            "MODEL-ONLY SST — no in-situ NDBC buoy within range (typical outside US "
            "waters); the model reading has no independent cross-check and confidence "
            "is reduced accordingly.",
            SIGNAL,
        )

    st.markdown(
        render_body(result.snapshot, result.assessment, config, stale=result.stale),
        unsafe_allow_html=True,
    )
    st.markdown(render_footer(), unsafe_allow_html=True)


terminal()
