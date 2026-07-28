# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt

streamlit run app.py                            # dashboard on :8501 (Live Index at /, Data Lab at /lab)
.\run_overnight.ps1                             # supervised launch; restarts if the process dies

python tests/run_all.py                         # full suite (offline + live)
python tests/run_all.py --offline               # fast, deterministic, no network

python api_clients.py MTBY                      # transport smoke test: one region, all 6 feeds + telemetry
python analyzer.py MTBY                         # live pipeline: fetch + score, component breakdown
python ml_analysis.py path/to/observations.csv  # Data Lab engine against a local file
python geocoding.py "Monterey Bay"              # geocoder / lat-lon parser check
```

Region codes: `MTBY`, `SCAB`, `MASS`, `NYBT`, `FLKY`, `HATT`, `OAHU` (default `MTBY`). Any other location is
reached through the top-bar search box (place name or raw `lat, lon`).

Tests live in `tests/` (see `tests/README.md`) and need no pytest — each module runs standalone and exits
non-zero on failure. `test_ml_analysis.py` carries the seasonal-confounding regression guard; its fixtures are
committed and deterministic.

Query-parameter dev flags (from the design handoff): `?density=compact` tightens the two padding values
(22/24px → 16/16px), `?console=0` hides the API transport log, `?width=fluid` releases the fixed 1920px frame,
`?refresh=<seconds>` sets the auto-refresh cadence (floored at 60), `?alert=<score>` sets the alert threshold
(default 70, the CRITICAL band).

Environment overrides, all optional and defaulted to the current values: `MEHI_CONTACT` (the User-Agent
contact sent to Nominatim/Overpass — set a real one when deploying under a different operator),
`MEHI_HTTP_TIMEOUT`, `MEHI_DYNAMIC_TTL`, `MEHI_CONTEXT_TTL`, `MEHI_HISTORY_DAYS`, `MEHI_OVERPASS_BUDGET`,
`MEHI_MAX_CONCURRENT_FETCHES` (aggregate cap on concurrent upstream fetches across all sessions, default 4),
and `DATABASE_URL` / `MEHI_HISTORY_DB` (Postgres URL for durable score history; unset → local JSONL).

The suite above does not exercise the rendered UI, so verify UI changes with Streamlit's own harness — note
that `AppTest` does *not* put the app directory on `sys.path` the way `streamlit run` does:

```python
import sys; sys.path.insert(0, r"C:\marine-telemetry-dashboard")
from streamlit.testing.v1 import AppTest
at = AppTest.from_file("app.py", default_timeout=400); at.run()
assert not at.exception
at.query_params["console"] = "0"     # dev flags are testable this way
at.selectbox[0].select("Cape Hatteras").run()
at.button[0].click().run()           # forces a live re-fetch past the cache
```

**`AppTest` cannot drive `st.file_uploader`**, so the Data Lab's rendering is unreachable that way. That is why
presentation lives in `lab_render.py` as plain functions: point a harness script at a fixture CSV, call the
`render_*` functions, and run *that* under `AppTest`. Keep new Lab UI in `lab_render.py` rather than inlining it
into `page_lab.py`, or it becomes untestable.

Do **not** bulk-edit source with PowerShell `-replace`: it is case-insensitive by default (it will rewrite
prose words like "rule" along with the token `RULE`) and `Get-Content -Raw` decodes UTF-8 as ANSI in
Windows PowerShell 5.1, which corrupts the em-dashes and middots used throughout these files.

## Architecture

Two independent chains sharing one shell. Neither transport nor analysis imports Streamlit.

```
geocoding.py ─┐
api_clients.py → analyzer.py  ─┐
                               ├→ app.py (st.navigation, position="hidden")
ml_analysis.py ────────────────┘        ├→ page_live.py → terminal_render.py
                                        └→ page_lab.py  → lab_render.py
                                   shared: ui.py, data_access.py
```

**Live chain.** `api_clients.fetch_region_snapshot()` opens one `httpx.AsyncClient` and gathers six feeds
concurrently with `return_exceptions=True`, so a dead source degrades the result instead of aborting it.
`_BaseClient._fetch` handles retry-with-backoff then mirror rotation, recording a `TelemetryEvent` for *every*
attempt including failures; that sink is what the console feed renders, so the log is measured rather than
narrated. `analyzer.assess_region()` blends four components (thermal 0.25, accumulated thermal stress 0.20,
taxonomic 0.30, charted maritime activity 0.25), each computed inside its own `try/except` — there is deliberately **no outer
catch** in `assess_region`, so a new component must carry its own guard. The thermal budget is still 0.45 in
total; what changed when DHW landed is that it is *split* between the instantaneous reading and the
accumulated one, leaving taxonomic and pressure untouched. Unavailable components are dropped and the surviving weights
**renormalised**, so the score stays comparable; it is either backed by real observations or `None`.

A `FeedBundle` becomes a `RegionSnapshot` in exactly one place, `api_clients.snapshot_from_bundle()`. Both the
single-shot fetch and `data_access._compose`'s two-tier merge go through it, so a new feed field cannot exist
in one path and silently not the other.

**Lab chain.** `ml_analysis` ingests an uploaded table, infers the time base and parameter columns, and returns
an `AnalysisReport`. Same evidential rule: every result carries its sample size and significance, and tests
that cannot run report why.

**Expandable detail.** Panels carry click-to-expand drawers built as native HTML `<details>`/`<summary>`
(which survive Streamlit's HTML sanitizer) — no Streamlit callback, no layout disruption. They surface data
that is already fetched but not shown on the face of the panel: every component's full derivation dict, the
per-year OISST baseline, the complete taxa list and phylum breakdown, and every charted seamark type. Because
the injected HTML is re-rendered on each auto-refresh, an open drawer collapses on refresh — acceptable at the
multi-minute cadence. If you add a panel, add its drawer in `terminal_render.py` via `_drawer(...)`.

**Score history, comparison, alerting.** `data_access` keeps an append-only `history_<code>.jsonl` per location
alongside the last-known-good mirror — the mirror overwrites, so it can never show a trend. `_append_history`
records one point per scored load, rate-limited to one per 30 min and pruned to `HISTORY_RETENTION_DAYS`;
history is evicted with its location. `terminal_render._history_panel` charts it (and states "collecting"
below two points rather than drawing a one-point trend). The top bar's COMPARE selector loads a second region
through the same `load_region` path and renders `render_comparison`. `render_alert_banner` fires at or above
the `?alert=` threshold (default 70) using the one amber accent, and the crossing is also logged so an
unattended run leaves a trace in `logs/`.

**Rendering.** The Live Index is generated by `terminal_render.py` as one HTML/SVG string, not with Streamlit
layout primitives — the design is a fixed 1920px frame with a 360px sidebar beside a flexible main column,
which `st.columns` cannot express, and the handoff specifies inline SVG with no chart library. Streamlit's own
navigation is hidden (`position="hidden"`) for the same reason: a second framework sidebar would break the
layout. Page links live in the top bar via `ui.safe_page_link`, which degrades to plain text when a page is run
outside a navigation context (as the test harness does).

### Location search

Beyond the seven curated regions, the top-bar box accepts a place name (resolved via Nominatim in
`geocoding.py`, rate-limited to 1 req/sec with a required User-Agent) or a raw `lat, lon` pair. Raw coordinates
are the reliable path for offshore points — geocoders index the ocean poorly. A searched location becomes a
`Region` via `Region.from_point`, which synthesizes a **hard-capped** bounding box (Overpass cost scales with
area and 504s on large boxes), and flows through the same `load_region` cache/fallback path as a curated region.

Two things a searched location loses that a curated one has, both surfaced in the UI rather than hidden:

- **In-situ buoys.** Curated regions carry hand-tuned `buoy_candidates`; a searched region has none, so
  `ErddapClient.discover_stations` finds the nearest NDBC stations dynamically. Outside US waters there usually
  are none — `RegionSnapshot.marine_coverage == "model_only"`, confidence drops, and the banner says why.
- **Guaranteed marine data.** `marine_coverage == "none"` is the explicit land / no-ocean state: both OISST and
  the model returned *successfully but empty* (the same silent all-null shape that once hid the pre-2023 SST
  gap). It is distinguished from a transient fetch failure (`"unknown"`, feeds are `None`) so "you searched
  Denver" reads differently from "the API is down".

### Naming

The product is a "Health Index" but the headline figure is a **stress** score — high is bad. The score→colour
rule (amber ≥70, canopy <30) lives in `ui.stress_accent`, not in the analyzer — presentation stays in the UI
layer; the hero number and the comparison strip both colour through it.

Because the name inverts the number, **the direction of the scale is stated on the face of the sidebar**
(`0 = UNDISTURBED · 100 = SEVERE · HIGHER IS WORSE`) and repeated in the alert banner. Do not remove it: a
band of `LOW` reads equally well as "low health", and a 0.0 produced by two excluded components would
otherwise be indistinguishable from a pristine reading. For the same reason confidence sits next to the hero,
and each excluded component prints its reason inline rather than only inside the methodology drawer.

**The index has no memory, and says so.** The thermal anomaly is scored against the *same calendar date* in
the ten-year baseline. That date-matching is what stops the index reporting "it is winter" as stress — and is
exactly why it cannot see damage done in an earlier warm season: a reef bleached last summer sits at a
genuinely normal temperature in its winter and scores clean. `terminal_render.season_for` therefore prints the
hemisphere and season on the face of the sidebar, amber in the cool season, above a one-line statement that the
score is a present-conditions reading. Do not remove it — without it a cool-season score reads as an all-clear
for a reef that is in fact wrecked. Near the equator (|lat| < 10°) the function reports `tropical` rather than
asserting a warm/cool half-year that does not exist at that latitude. A cumulative measure
(NOAA Coral Reef Watch Degree Heating Weeks) is the real fix and is not implemented; until it is, the
framing note is the only thing standing between a cool-season reading and a wrong conclusion.

## Design system

Implemented from the Marine Ecosystem Health Dashboard handoff; tokens live in `ui.py` and nothing downstream
should hard-code a hex value.

| Token | Hex | Use |
|---|---|---|
| `INK` | `#0C1512` | page background |
| `PAPER` | `#EDF2EC` | primary text and numbers |
| `PAPER_DIM` | `#C6D3CA` | secondary values, tick labels |
| `MIST` | `#82A896` | dimmest labels, live indicator, secondary chart line |
| `LINE` | `#22362B` | hairline dividers |
| `BORDER_STRONG` | `#2f4a3a` | pills, dashed reference line |
| `SIGNAL` | `#E4B34A` | amber: label chips, alerts, warnings, NOAA-ERDDAP log rows |
| `CANOPY` | `#3E8F62` | primary accent: chart lines, bars, in-situ legend |

Three rules are easy to undo by accident and are asserted by the fidelity check:

- **No alert-red hue.** Every warning, anomaly and error uses the single amber accent. The palette is
  deliberately two accents total.
- **No rounded corners anywhere**, even where a framework default would round them.
- **No glow, pulse, or transition.** An earlier draft had a pulsing live dot; it was removed deliberately.

Space Mono throughout; Space Grotesk is used *once*, for the 60px hero number.

## Unattended operation

The dashboard is expected to run overnight with nobody watching, so failures degrade rather than surface.

- `page_live.py` wraps the whole page in `st.fragment(run_every=...)`, set to the data cache TTL plus a
  margin so each timed rerun pulls genuinely fresh data instead of re-rendering the same cached snapshot.
- **Volatility-tiered caching.** The six feeds age at very different rates, so `data_access` caches them in
  two tiers and composes the `RegionSnapshot`: a *dynamic* tier (buoy + model SST) cached ~10 min and keyed by
  `(code, refresh-generation)` so REFRESH re-pulls it, and a *context* tier (OISST baseline + OBIS + Overpass)
  keyed by `(code, UTC-date)` and cached a day, since it is stable within a day. A timed refresh then re-fetches only
  the fast feeds — measured **25 s cold to 1.5 s refresh (~94% less)** — instead of re-pulling the ~70 s
  baseline every cycle. `_compose` sets `fetched_at`/wall-clock from the live tier and merges both tiers'
  telemetry, so the console honestly shows each feed's last request time. New feeds go in the tier matching
  their volatility (`DYNAMIC_FEEDS` / `CONTEXT_FEEDS`). `_remember` refuses to store a result that carries no
  score or fewer than two live feeds, so an outage never poisons last-known-good.
  Measured end to end: **warm rerun ~12 ms, REFRESH ~1.4 s, cold load 8-20 s** (was 33-70 s before the
  Overpass single-query change). The two tiers are fetched sequentially, which costs the dynamic tier's
  ~1.5-3 s on a cold load; making them concurrent was considered and rejected — the tiers are separately
  `st.cache_data`-wrapped and the independent invalidation is worth more than the seconds.
  Because `fetch_feeds` *returns* its errors rather than raising, a context tier that failed transiently would
  otherwise sit in the cache until the UTC date rolled over. `_schedule_context_retry` therefore folds a retry
  generation into the context cache key whenever the bundle carries feed errors, spaced at least `DYNAMIC_TTL`
  apart so a persistent outage is retried once per refresh cycle rather than on every page load.
- **Bump `SNAPSHOT_SCHEMA_VERSION` whenever a snapshot model changes.** The context tier is
  `st.cache_data(persist="disk")`, which **pickles**. Unpickling restores an instance's `__dict__` directly,
  so a field added to a model does *not* acquire its pydantic default on an object the previous deploy wrote —
  it is simply absent, and the first attribute access raises `AttributeError` on a live page. Adding
  `annual_peak_dhw` took the deployed dashboard down exactly this way, served from a cache entry the old build
  had written. The version is folded into both tier cache keys, so bumping it retires every stale entry at
  once. (The `.cache/*.json` last-known-good mirrors are immune — they go through `model_validate_json`, which
  *does* apply defaults and ignores fields that no longer exist.) Guarded by
  `test_cache_keys_carry_the_snapshot_schema_version`.
- **Multi-user on one instance.** The two `st.cache_data` tiers are process-global and shared by *every*
  browser session, so once one visitor warms a region/day the rest hit cache and issue zero upstream calls —
  which is why a single instance serves dozens of viewers and why you must run *one* instance, not replicas
  (replicas would not share the cache). Two in-process guards handle concurrent *cold* misses, both in
  `data_access`: `_single_flight` (a per-key lock registry with a waiter refcount) collapses a same-region
  stampede to one fetch, and `_FETCH_GATE` (a `threading.BoundedSemaphore`, `MEHI_MAX_CONCURRENT_FETCHES`)
  bounds the aggregate concurrent fan-out so a burst of distinct searches cannot trip the shared-IP rate
  limits. REFRESH bumps a **process-global** generation (`bump_refresh`), so one viewer's refresh re-warms the
  dynamic tier once for everyone rather than forking a private cache entry per session. The Nominatim throttle
  (`geocoding._throttle`) stays a global 1 req/1.1 s by policy; its 1-hour result cache absorbs popular
  searches.
- **Durable score history.** History is the one piece of state that must survive a redeploy on an ephemeral
  host, so `load_history`/`_append_history` are a seam: with `DATABASE_URL` (or `MEHI_HISTORY_DB`) set they go
  to Postgres (`psycopg`, one lock-guarded connection, reconnect-on-drop for a serverless DB resuming from
  suspend); unset, they use the local `.jsonl` mirror unchanged. History I/O is best-effort — a DB failure
  degrades to `[]`/silent, and a *missing* `psycopg` disables the DB path once and falls back to local. The
  same ≤1/30-min rate limit and `HISTORY_RETENTION_DAYS` prune apply on both backends.
- `data_access.load_region()` never raises. On a failed fetch it returns the last good result for that region —
  from process memory, or from the `.cache/` JSON mirror if the server has restarted — flagged `stale`, with
  the failure reason attached. The page then shows a `SHOWING LAST GOOD DATA` banner over a populated
  dashboard rather than blanking. Only a region with no successful fetch *ever* renders the empty state.
- Backend failures are **logged, not swallowed**: `data_access` holds a module logger and `app.py` configures
  root logging once, so cache-mirror problems, fallback decisions and alert crossings reach stderr — which
  `run_overnight.ps1` redirects into `logs/`. The never-raise contract is unchanged; the difference is that a
  silent `except: pass` now leaves a trace.
- `run_overnight.ps1` supervises the process itself, restarting it with backoff and appending to `logs/`.
- **The file watcher is split by environment, deliberately.** `.streamlit/config.toml` carries the *hosted*
  values (`fileWatcherType = "auto"`, `runOnSave = true`) because on Community Cloud the only thing that ever
  changes a file is a deploy, and that is exactly when a reload is wanted. With the watcher off, a deploy
  logs `Updated app!` and then keeps serving the **old** modules: almost all of this app lives in imports
  (`ui`, `terminal_render`, `data_access`) and `import` hits `sys.modules`, not disk — so a pull updates the
  files while the running process ignores them, and the only fix is a manual Reboot. `run_overnight.ps1`
  pins the opposite (`--server.fileWatcherType none --server.runOnSave false`) on the command line, which
  outranks the config file, so an unattended local run still never reloads mid-run.

## Source constraints

Established by probing the live services; easy to get wrong:

- **Open-Meteo has no SST before 2023-01-01.** Earlier dates return well-formed responses whose value arrays
  are entirely `null`, from both the marine endpoint and the ERA5 archive. It is used *only* for present-day
  sea state. The ten-year baseline comes from NOAA OISST v2.1 (`ncdcOisst21Agg`), which reaches back to 1981.
- **OISST lags roughly two weeks.** `fetch_climatology` requests only the ten *completed* prior years, centred
  on today's month/day. Fetching the full decade as one series also works but takes ~70 s; the day-of-year
  windows are small and run concurrently behind a semaphore. Measured: the ten windows sum to ~21-24 s of
  request time but complete in a ~6 s span (~3.6x against `max_concurrency=4`). Since the Overpass fix above,
  **this is the floor on a cold context fetch** — do not describe OISST as "the ~70-second baseline"; that
  was true before the windowing and is the single easiest stale assumption to act on.
- **ERDDAP rejects generic query encoding.** Constraints must follow a literal `&` and quoting must survive
  verbatim, so ERDDAP URLs are assembled as strings — do not switch them to `params=`. `ncdcOisst21Agg` takes
  longitude in 0-360; the `_LonPM180` variant takes -180-180, and `_sst_url` normalises for whichever is used.
- **NDBC stations fail in several distinct ways**, hence `Region.buoy_candidates` being an ordered list. A
  station can be long dead (44005 last reported in 2024), reporting but with `wtmp` null (common), or stale.
  `fetch_buoy` walks the list for a fresh non-null reading and otherwise returns the best partial result so the
  UI can explain *why* in-situ data is missing.
- **Overpass sheds load with 504s, and is by far the slowest feed.** Measured: 1.8-3.7 s for a curated bbox,
  11-16 s for a larger searched one, and 6.5-20.4 s *per failed attempt* when a mirror is shedding load. It
  dominates a cold load — everything else finishes underneath it. Three mirrors are configured;
  `OverpassClient` runs with `max_attempts=2` so it rotates rather than hammering a busy host, and carries a
  wall-clock budget (`OVERPASS_BUDGET_SECONDS`, 25 s) shared across *all* its requests, because an attempt
  count bounds nothing when one attempt can take 20 s. A 429 is a per-IP rate limit and moves straight to the
  next mirror instead of retrying — unlike a 504, it will not clear during a one-second backoff.
  **One query, not two.** `out tags N` returns one element per feature carrying its `type`, so node/way totals
  are counted from that response; the old `out count` query used an identical selector and therefore counted
  exactly the elements the tag query already returns (verified against the live API and every cached
  snapshot). `out count` is now issued *only* when the response hits `sample_cap`, where the elements are a
  truncated sample and cannot supply the true total, or as a fallback when the tag query fails (density-only
  scoring). Relations are structurally always zero — the selector never asks for them.

## Statistical caveats

Both chains had a version that produced confident, badly wrong numbers. Preserve these guards.

**Decadal trend (live chain).** A least-squares slope over ten annual means is noise-dominated — point
estimates of 1.5 °C/decade appear routinely, an order of magnitude above anything physical. `_linear_trend`
returns `(slope, stderr, r²)` and scoring uses **`trend_lower_c_per_decade`**, the one-sided ~95 % lower bound
floored at zero, so only warming that clears its own uncertainty contributes. The point estimate is displayed
with its error bar but never scored.

**Do not reimplement Degree Heating Weeks — fetch CRW's.** DHW accumulates SST above the climatological
warmest-month mean (MMM), and getting MMM right is where a local implementation dies. Computing it from a
*rolling recent* window folds the warming DHW exists to detect into the baseline it is measured against: an
OISST-derived version of this component reported **0.0 °C-weeks for 2016, 2017, 2022 and 2024** on the Great
Barrier Reef, all documented mass-bleaching years. Switching to a fixed 1985-2012 baseline fixed the sign of
the error but not its size — a single 25 km OISST cell smooths away the shallow-reef temperatures that drive
bleaching, scoring GBR 2024 at 1.4 °C-weeks where NOAA's 5 km product says **7.3**. The component now reads
`NOAA_DHW` (Coral Reef Watch, 5 km daily) directly, on the ERDDAP host the client already uses, so there is no
new mirror and no new failure mode. Validated against ground truth: every documented GBR mass-bleaching year
(2016, 2017, 2020, 2022, 2024) shows up, 2018/2019/2021 correctly read ~0, the Florida Keys' catastrophic 2023
reads 21.9, and Monterey Bay's 2015 "Blob" reads 28.1.

**The 12-week window has no memory; `annual_peak_dhw` is the memory.** CRW's DHW is a rolling 12-week
accumulation, so it decays to zero within a season of the event — the GBR in austral winter genuinely reads
0.0 at 4.95 °C *below* its warmest-month mean, which is exactly how it scored LOW while carrying 7.3 °C-weeks
from two years earlier. `fetch_thermal_stress` therefore also pulls a strided decade of DHW and keeps each
year's peak. Scoring takes the worse of the current window and the best-scoring *aged* year.

**Age every year, not just the worst one.** `_score_thermal_stress` weighs `_dhw_to_score(peak) ×
_recency_weight(age)` across the whole history and takes the maximum. Decaying only the single largest year
lets an old severe event mask a recent near-severe one: on the GBR that scored 12.5, because 2017's 8.6
°C-weeks are nine years gone and worth 12% of themselves, burying 2024's still-current 7.3. Correct answer is
94.5. Guarded by `test_an_old_severe_event_cannot_mask_a_recent_one`.

**`_recency_weight` is a modelling choice, not a NOAA standard.** Same epistemic status as
`PHYLUM_SENSITIVITY`. NOAA calibrates DHW against bleaching *as it happens* and publishes nothing about how
long the damage should keep counting; `RECENT_EVENT_YEARS`/`RECOVERY_YEARS` (2 and 10) bracket published reef
recovery times. Argue with the shape, do not present it as sourced.

**Outside reef latitudes the current term is scored on Hobday, not on DHW.** NOAA's 4 and 8 °C-weeks are coral
mortality numbers and mean nothing in a kelp forest. Hobday et al.'s marine-heatwave categories are defined on
how far past the seasonally-varying 90th percentile a reading sits, in multiples of the (p90 − mean) gap —
1-2× Moderate, 2-3× Strong, 3-4× Severe, >4× Extreme — so they carry the same meaning at any latitude.
`_mhw_multiple` computes this from the OISST baseline the app already fetches, and takes over the *current*
term when `is_reef_latitude` is false. Two deviations from the published method are stated on the panel: the
baseline is ten completed years over ±10 days rather than thirty over ±5, and **the duration criterion cannot
be checked at all** — a real heatwave is five or more consecutive days above the percentile, which one
instantaneous SST cannot establish. This is the intensity of the present reading, not a confirmed event.
The *history* term still rides the coral curve; that is the remaining approximation and the notes say so.
Closing it needs annual MHW categories, which need per-day historical climatology the app does not fetch.

**DHW's thresholds are coral, its quantity is not.** NOAA's 4 and 8 °C-week thresholds are calibrated against
observed coral bleaching and mortality. Accumulated warm anomaly is real stress in a kelp forest too (the
2014-16 north-east Pacific event was measured this way), so the component scores everywhere, but
`is_reef_latitude` (|lat| ≤ 35°) gates the *bleaching wording* — and note the recovery decay is coral-derived
as well, so outside the tropics read the magnitude rather than the band. Of the seven curated regions only
Florida Keys and O'ahu are reef systems.

**CRW rejects a query past the end of its record.** It publishes ~2 days behind and returns a hard **404**
("your query produced no matching results") rather than clamping, so a fixed lag constant would break the feed
every time publication slipped. `_dhw_url` emits griddap's `last` keyword for the end index instead, which
always resolves to the newest available day. The history uses `stride=10` days: DHW is already a 12-week
rolling accumulation, so its annual peak is a broad feature — measured against a stride-5 reference, stride 10
moves no annual peak by more than 0.14 °C-weeks and costs ~5 s for eleven years, while stride 20 drifts 0.76.

**Seasonal confounding (Lab chain).** Fitting a line through a seasonal series measures the cycle's phase, not
its trend: a clean sine sampled over three cycles carries a linear slope of roughly −1 unit/year from its own
geometry. On a fixture with a planted +0.30 °C/yr trend the naive fit returned **−0.72**. `analyze_dataset`
therefore establishes periodicity *first*, removes the cycle via `_deseasonalize` (phase-bin medians, valid for
diurnal, tidal and annual cycles alike), and only then fits the trend, detects changepoints and scores
outliers. `_snap_period` snaps a measured period to the exact physical constant when close, because phase
drift from an approximate period leaks trend into the seasonal adjustment.

Two related traps in the same code: autocorrelation peaks are searched only *after the ACF's first zero
crossing* (otherwise the decay from lag 1 always wins and an annual cycle is reported as a 3-day one), and
Mann-Kendall p-values are adjusted for serial correlation via an effective sample size, since consecutive
readings are not independent.

`PHYLUM_SENSITIVITY` is an ordinal weighting of how *exposed* an assemblage is to warming and acidification
(calcifiers and sessile taxa highest). It is not a measurement of observed decline, and the taxonomic
component should not be described as one.

**The pressure component is named `CHARTED MARITIME ACTIVITY`, not "anthropogenic pressure".** It counts
OpenSeaMap seamarks — buoys, beacons, shipping lanes. It knows nothing about agricultural runoff,
crown-of-thorns outbreaks or cyclone damage, so a remote reef scores near zero and that is a fact about the
chart rather than the reef: the Great Barrier Reef reads 0.3/100 while carrying a quarter of the blend's
weight. Widening this needs new data sources (water-quality monitoring, disturbance records), not different
arithmetic. Do not relabel it back.

**Prokaryotes are excluded from the phylum mix, and that is load-bearing.** OBIS carries whole metagenomics
surveys, and where one lands on a region it can be most of the checklist: measured at Massachusetts Bay,
**82% of the sample is Bacteria and Archaea** (Proteobacteria alone 48.5%). None of them have an entry in
`PHYLUM_SENSITIVITY`, which ranks marine animals and algae, so before `NON_TARGET_KINGDOMS` they all landed on
`DEFAULT_SENSITIVITY` and then drove *both* the sensitivity mean and the evenness term — a microbial survey
scored by a table written for calcifiers. The excluded share is reported on the stats strip and reduces the
component's quality, because a sample that was mostly microbial is a thin basis for an assemblage claim
however clean the remainder looks. The component also states what fraction of the *surviving* mix still falls
back to the default (30% at Massachusetts Bay), since that default is a placeholder for "unknown" and its
weight should be visible rather than inferred.

**The phylum mix is a top-N sample, not the region's composition.** `ObisClient.fetch` pulls a 200-taxa
checklist, so `phylum_records` (and every percentage derived from it) is computed over that sample — which is
biased toward abundant, heavily-recorded groups by construction — while the occurrence count in the stats
strip comes from the statistics endpoint and can be three orders of magnitude larger. The panel is titled
`PHYLUM MIX (TOP-TAXA SAMPLE)` and the strip carries a `Taxa sampled for mix` row for exactly this reason;
do not relabel either as the region's full composition.

**Two series on one axis must share a time domain.** The model SST covers ~6 days and the buoy history 48 h.
`_line_chart` takes optional `x_fractions` per series so each is positioned by *when* it was measured; the
same applies to the score-history panel, whose points accrue irregularly. Spacing points evenly by index
draws an hour-long jump and a week-long drift as the same slope. Axis labels are laid out evenly by flexbox,
so they must be generated at evenly spaced *times*, not from sampled instants.
