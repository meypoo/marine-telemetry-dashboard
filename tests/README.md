# Tests

No pytest required — every test module runs standalone and exits non-zero on
failure. If pytest *is* installed, the `test_*` functions are discoverable too
(`pytest tests/`).

```bash
python tests/run_all.py            # everything (offline + live)
python tests/run_all.py --offline  # offline suites only, no network

python tests/test_ml_analysis.py   # a single suite
```

## Suites

| Module | Network | What it guards |
|---|---|---|
| `test_ml_analysis.py` | no | **The seasonal-confounding regression.** The fixture plants a +0.30 °C/yr trend under an annual cycle; a naive fit reads −0.72. Asserts the engine recovers +0.30. Also: pH trend, salinity step-vs-seasonality, correlation signs, arbitrary (non-marine) columns, edge cases, delimiter sniffing. |
| `test_regions.py` | no | `Region.from_point` bounding-box capping and longitude normalisation; `marine_coverage` land / model-only / full / unknown logic. |
| `test_geocoding.py` | no | `parse_latlon` (offline, always); live Nominatim lookup only when `MEHI_LIVE_TESTS=1`. |
| `test_analyzer.py` | no | **The scoring core.** Weight renormalisation when a component drops, band thresholds at their exact boundaries, the logistic mapping, the blend arithmetic, and that only the trend's lower confidence bound is scored. Also guards thermal quality against the requested baseline span rather than a hardcoded decade. |
| `test_transport.py` | no | `_BaseClient._fetch` retry / backoff / mirror rotation via `httpx.MockTransport`, and that every attempt (including failures) reaches the telemetry sink. Plus `_sst_url` longitude normalisation, `_linear_trend`, `_haversine_km`. |
| `test_data_access.py` | no | Disk mirror round-trip, atomic writes, bounded eviction; two-tier composition; `load_region`'s memory → disk → none fallback; the context-tier retry (a transient feed failure must not be cached for the UTC day); score-history append, rate-limiting and pruning. **Multi-user concurrency:** single-flight collapses a same-region cold stampede to one fetch, the global cap bounds distinct-key concurrency, and the refresh generation advances globally. |
| `test_history_db.py` | opt | The durable-history Postgres backend against a **fake** psycopg (dispatch, ≤1/30-min rate limit, retention prune, reconnect-on-drop, and clean fallback to local when the driver is missing). A real round-trip runs only when `MEHI_HISTORY_DB` points at a throwaway Postgres. |
| `test_render.py` | no | The history panel's empty state and charted series, alert threshold behaviour and direction reporting, the comparison strip, region-name escaping, and that the new panels introduce no hex outside the token table. |
| `test_layout.py` | no | **Clipping and overlap guards.** Overflow rules on every element that can receive an unbounded string, the title truncating instead of bleeding into the search box, the sidebar breakpoint, and that baseline bars stay distinguishable (they were flat on a zero-based scale). Also the three fragile design rules: sharp corners, no glow/transition, no stray hex. |
| `test_app.py` | yes | Terminal renders with the portfolio palette (no legacy hues); design fidelity vs the handoff numbers; region switch / refresh / dev flags; free-form search (offshore + land); Data Lab rendering; overnight resilience (memory → disk → no-history) and the STALE banner. |

## Fixtures

`fixtures/*.csv` are committed and deterministic (`make_fixtures.py`, fixed
seed). They carry the ground truth the regression test checks against, so they
must stay in version control — regenerate with `python tests/make_fixtures.py`
only if you intend to change the planted truth (and update the assertions).

`AppTest` cannot drive `st.file_uploader`, so `lab_harness.py` renders a fixture
through the Data Lab's `render_*` functions directly; `test_app.py` runs that
harness under `AppTest`.
