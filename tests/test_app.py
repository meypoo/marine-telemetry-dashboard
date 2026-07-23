"""Live integration + rendering tests: the terminal, the Data Lab, resilience.

These hit the network (live API fetch) and exercise the Streamlit runtime via
AppTest, so they are slower than the offline suites and are skipped by
`run_all.py --offline`. Run standalone with `python tests/test_app.py`.

Covers:
  * entry point renders the terminal with the portfolio palette (no legacy hues)
  * design fidelity against the handoff's stated numbers
  * region switch, manual refresh, dev flags
  * free-form location search (raw lat/lon and place name)
  * the land / no-marine-data state
  * Data Lab rendering across fixtures
  * overnight resilience: memory -> disk -> no-history fallbacks + STALE banner
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
HARNESS = Path(__file__).resolve().parent / "lab_harness.py"
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

_failures = 0


def _fail(stage: str, message: str) -> None:
    global _failures
    _failures += 1
    print(f"[FAIL] {stage}: {message}")


def _check(stage: str, at: AppTest, expect=()) -> str:
    global _failures
    excs, errs = list(at.exception), list(at.error)
    if excs or errs:
        _failures += 1
        print(f"[FAIL] {stage}")
        for e in excs:
            print("   EXCEPTION:", str(e.value)[:400])
        for e in errs:
            print("   ERROR:", str(e.value)[:400])
        return ""
    joined = "\n".join(m.value for m in at.markdown)
    missing = [t for t in expect if t not in joined]
    if missing:
        _failures += 1
        print(f"[FAIL] {stage}: missing {missing}")
    else:
        print(f"[ OK ] {stage}")
    return joined


def run() -> int:
    global _failures

    # Warm the shared load_region cache for every location the AppTest runs will
    # touch, so each AppTest hits a warm cache instead of triggering a cold live
    # fetch inside the harness timeout. This makes the suite depend on the APIs
    # once up front rather than repeatedly, and keeps it from timing out when an
    # upstream (OISST, Overpass) is briefly slow.
    print("=== warming caches (live fetch) ===")
    from data_access import load_region as _lr
    for code in ("MTBY", "HATT"):
        r = _lr(code, 0)
        print(f"    {code}: {'ok' if r.ok else 'FAILED ' + str(r.error)}")

    print("\n=== entry point -> terminal ===")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=400)
    at.run()
    joined = _check("app.py -> live terminal", at,
                    ["MARINE ECOSYSTEM HEALTH INDEX", "STRESS SCORE", "tm-side",
                     "CONSOLE — LIVE API TRANSPORT LOG"])

    print("\n=== palette / no legacy artefacts ===")
    for token in ("#E4B34A", "#3E8F62", "#0C1512", "Space Mono", "Space Grotesk"):
        if token not in joined:
            _fail("palette", f"missing {token}")
    for banned in ("#00FFFF", "#00FF00", "glow-red", "text-shadow", "#FF3B30", "#7FFFD4"):
        if banned in joined:
            _fail("palette", f"legacy artefact present: {banned}")
    if _failures == 0:
        print("[ OK ] palette clean")

    print("\n=== fidelity: handoff numbers ===")
    _fidelity()

    print("\n=== region switch + refresh + dev flags ===")
    if at.selectbox:
        at.selectbox[0].select("Cape Hatteras").run()
        _check("region switch", at, ["STRESS SCORE"])
    if at.button:
        at.button[0].click().run()
        _check("manual refresh", at, ["STRESS SCORE"])

    flagged = AppTest.from_file(str(ROOT / "app.py"), default_timeout=400)
    flagged.query_params["console"] = "0"
    flagged.query_params["density"] = "compact"
    flagged.run()
    j = _check("dev flags render", flagged, ["STRESS SCORE"])
    if "CONSOLE — LIVE API TRANSPORT LOG" in j:
        _fail("console flag", "console still shown with ?console=0")

    print("\n=== free-form location search ===")
    _search_tests()

    print("\n=== Data Lab ===")
    lab = AppTest.from_file(str(ROOT / "page_lab.py"), default_timeout=200)
    lab.run()
    _check("data lab empty state", lab, ["WHAT GETS COMPUTED", "DATA LAB"])
    for fixture, focus in [
        ("fixture_known.csv", "temperature_c"),
        ("fixture_nonmarine.csv", "widget_throughput"),
        ("fixture_tiny.csv", ""),
        ("fixture_semicolon.csv", "ph"),
    ]:
        os.environ["LAB_FIXTURE"] = str(FIXTURES / fixture)
        os.environ["LAB_FOCUS"] = focus
        h = AppTest.from_file(str(HARNESS), default_timeout=300)
        h.run()
        _check(f"lab render [{fixture}]", h, ["HARNESS-COMPLETE"])

    print("\n=== overnight resilience ===")
    _resilience_tests()

    print(f"\nRESULT: {'PASS' if _failures == 0 else str(_failures) + ' failing check(s)'}")
    return 1 if _failures else 0


def _fidelity() -> None:
    from data_access import load_region
    from terminal_render import render_body
    from ui import TerminalConfig, _terminal_css

    result = load_region("MTBY", 0)
    if not result.ok:
        _fail("fidelity", f"no data to render: {result.error}")
        return
    body = render_body(result.snapshot, result.assessment, TerminalConfig())
    css = _terminal_css(TerminalConfig())
    full = css + body

    # OISST bars: the renderer draws one per successfully-fetched baseline year,
    # so the count tracks years_covered (up to 10) rather than a fixed 10 — a
    # year-window can 503 under live conditions.
    bars = body.count('class="tm-bar"')
    years_covered = result.snapshot.climatology.years_covered if result.snapshot.climatology else 0
    checks = {
        "sidebar 360px": "width: 360px" in css,
        "1920px frame": "width: 1920px !important" in css,
        "no rounded corners": not re.search(r"border-radius:\s*[1-9]", full),
        "amber chips filled": "background: #E4B34A" in css,
        "no alert-red hue": "#FF3B30" not in full,
        "no glow/pulse": "text-shadow" not in full and "animation" not in full,
        "hero Space Grotesk 60px": "font-size: 60px" in css and "Space Grotesk" in css,
        "SST viewBox 1000x220": 'viewBox="0 0 1000 220"' in body,
        "sparkline viewBox 300x30": 'viewBox="0 0 300 30"' in body,
        "OISST bars == years covered (<=10)": bars == years_covered and 1 <= bars <= 10,
        "log grid columns": "140px 140px 160px 70px 80px 80px 30px" in css,
        "6 stat tiles": body.count('class="tm-tile"') == 6,
        "15 stat rows": body.count('class="tm-statrow"') == 15,
        "12 hbar rows": body.count('class="tm-hrow"') == 12,
    }
    bad = [name for name, ok in checks.items() if not ok]
    if bad:
        for name in bad:
            _fail("fidelity", name)
    else:
        print(f"[ OK ] fidelity ({len(checks)} checks)")


def _search_tests() -> None:
    from api_clients import Region
    from data_access import load_region

    # Raw lat/lon offshore -> should get marine coverage.
    offshore = Region.from_point("Offshore OR", 44.6, -124.5)
    res = load_region(offshore, 0)
    if not res.ok:
        _fail("search offshore", f"no data: {res.error}")
    elif res.snapshot.marine_coverage not in {"full", "model_only"}:
        _fail("search offshore", f"unexpected coverage {res.snapshot.marine_coverage}")
    else:
        print(f"[ OK ] offshore point (coverage={res.snapshot.marine_coverage}, "
              f"score={res.assessment.score})")

    # Land point -> explicit no-marine-data state.
    land = Region.from_point("Denver", 39.74, -104.98)
    res = load_region(land, 0)
    if not res.ok:
        _fail("search land", f"no data: {res.error}")
    elif res.snapshot.marine_coverage != "none":
        _fail("search land", f"expected coverage=none, got {res.snapshot.marine_coverage}")
    else:
        print("[ OK ] land point reports coverage=none")

    # Full UI wiring: typing a raw coordinate into the search box (no geocoder
    # call, so no rate limit) must resolve and render.
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=400)
    at.run()
    at.text_input(key="location_search").set_value("44.6, -124.5").run()
    j = _check("search box wiring", at, ["SEARCH:", "raw coordinates 44.600", "STRESS SCORE"])


def _resilience_tests() -> None:
    import data_access
    from data_access import load_region

    good = load_region("MTBY", 0)
    if not good.ok:
        _fail("baseline load", f"origin={good.origin} error={good.error}")
        return
    print(f"[ OK ] live load (origin={good.origin}, score={good.assessment.score})")

    original = data_access._fetch
    data_access._fetch = lambda _r, _k: (_ for _ in ()).throw(
        RuntimeError("simulated upstream outage")
    )
    try:
        degraded = load_region("MTBY", 999)
        if not degraded.ok or not degraded.stale:
            _fail("memory fallback", f"origin={degraded.origin} ok={degraded.ok}")
        else:
            print(f"[ OK ] memory fallback (origin={degraded.origin})")

        data_access._LAST_GOOD.clear()
        disk = load_region("MTBY", 998)
        if not disk.ok or disk.origin != "disk":
            _fail("disk fallback", f"origin={disk.origin}")
        else:
            print("[ OK ] disk fallback (survives restart)")

        # A region with no history anywhere must report cleanly, not raise.
        data_access._LAST_GOOD.clear()
        from api_clients import Region
        empty = load_region(Region.from_point("Nowhere", 0.0, 0.0), 7)
        if empty.ok or empty.origin != "none":
            _fail("no-history path", f"origin={empty.origin}")
        else:
            print("[ OK ] no-history path reports cleanly")

        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300)
        at.run()
        j = _check("degraded render", at, ["STRESS SCORE"])
        if "SHOWING LAST GOOD DATA" not in j:
            _fail("stale banner", "no STALE banner while degraded")
        else:
            print("[ OK ] STALE banner shown, dashboard populated")
    finally:
        data_access._fetch = original


if __name__ == "__main__":
    sys.exit(run())
