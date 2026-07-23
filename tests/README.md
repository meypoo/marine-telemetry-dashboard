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
| `test_geocoding.py` | opt | `parse_latlon` (offline, always); live Nominatim lookup only when `MEHI_LIVE_TESTS=1`. |
| `test_app.py` | yes | Terminal renders with the portfolio palette (no legacy hues); design fidelity vs the handoff numbers; region switch / refresh / dev flags; free-form search (offshore + land); Data Lab rendering; overnight resilience (memory → disk → no-history) and the STALE banner. |

## Fixtures

`fixtures/*.csv` are committed and deterministic (`make_fixtures.py`, fixed
seed). They carry the ground truth the regression test checks against, so they
must stay in version control — regenerate with `python tests/make_fixtures.py`
only if you intend to change the planted truth (and update the assertions).

`AppTest` cannot drive `st.file_uploader`, so `lab_harness.py` renders a fixture
through the Data Lab's `render_*` functions directly; `test_app.py` runs that
harness under `AppTest`.
