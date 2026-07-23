# Marine Ecosystem Health Dashboard

A live-look "environmental risk terminal" reporting an **Ecological Stress
Score** for a marine location, built entirely from real public data — buoy
telemetry, biodiversity records, satellite SST climatology, and maritime
infrastructure. Styled as a technical instrument (fixed-width terminal), with a
second view — the **Data Lab** — that analyses time-series you upload yourself.

Every number is derived from a live API response. Nothing is mocked, and a
source that is unavailable is reported as such rather than filled in.

## Quickstart

```bash
pip install -r requirements.txt
streamlit run app.py                 # http://localhost:8501
```

For an unattended run that restarts itself if the process dies:

```powershell
.\run_overnight.ps1                  # Windows PowerShell
```

## The two views

**Live Index** (`/live`) — the stress score for one of seven curated regions or
**any location you search** (a place name, or a raw `lat, lon` pair for offshore
points the geocoder can't place). The score blends three independently computed,
independently degradable components:

| Component | Weight | Source |
|---|---|---|
| Thermal anomaly | 0.45 | Present SST vs a 10-year NOAA OISST day-of-year baseline |
| Taxonomic exposure | 0.30 | OBIS phylum composition weighted by warming/acidification sensitivity |
| Vessel pressure | 0.25 | OpenSeaMap seamark density and routing/berthing share |

Outside US waters the buoy network thins out, so a searched location may fall
back to model-only SST (confidence drops, and the UI says why); an inland search
gets an explicit "no marine data here" state rather than a misleading number.

**Data Lab** (`/lab`) — upload a CSV/TSV/Excel table of observations and get
trend (Theil-Sen with a serial-correlation-adjusted Mann-Kendall test, fitted
*after* removing any detected seasonal cycle), periodicity, regime shifts,
anomalies (Isolation Forest), and structure (correlation, PCA, k-means). Works
on **any** numeric column, not just recognised marine parameters.

## Data sources

OBIS (`api.obis.org`), Open-Meteo Marine (`marine-api.open-meteo.com`), NOAA
ERDDAP (`coastwatch.pfeg.noaa.gov` — `cwwcNDBCMet` buoys and `ncdcOisst21Agg`
OISST v2.1), OpenStreetMap Overpass (`overpass-api.de`), and Nominatim for
place-name search. All public; no API keys.

## Tests

```bash
python tests/run_all.py              # everything (offline + live)
python tests/run_all.py --offline    # deterministic, no network
```

No pytest required. See [`tests/README.md`](tests/README.md). The offline suite
carries a known-truth regression guard (a planted +0.30 °C/yr trend that a naive
fit misreads as −0.72) and runs in CI.

## Operational notes

- **Unattended by design.** A failed refresh degrades to the last good snapshot
  (kept in memory and mirrored to `.cache/`, capped and evicted), shown behind a
  STALE banner rather than blanking. `run_overnight.ps1` restarts the process if
  it dies. Auto-refresh only ticks while a browser tab is open.
- **Config flags** (query params): `?density=compact`, `?console=0`,
  `?width=fluid`, `?refresh=<seconds>`.
- **Not source, not committed:** `.cache/` (runtime snapshots) and `logs/`.

## Deployment

- **Windows:** `run_overnight.ps1` (supervised, self-restarting).
- **Linux/container:** a [`Dockerfile`](Dockerfile) is provided; the container
  runtime's restart policy replaces the supervisor. Mount a volume at
  `/app/.cache` to persist last-known-good data across restarts. *(The Dockerfile
  is written against the pinned requirements but was not built in the dev
  environment — smoke-test before relying on it.)*
- **Put a reverse proxy in front of it.** Streamlit should not be exposed to the
  public internet directly; terminate TLS and (if needed) add auth at nginx /
  Caddy / your platform's ingress. There is no built-in authentication — the
  dashboard is read-only and public by design.

### Known limitations (by design or scope)

- Auto-refresh only ticks while a browser tab is open (inherent to Streamlit
  fragments); the process stays healthy regardless.
- Single-node. In-process caches and the Nominatim rate-limit are per-worker, so
  this is not built for horizontal scaling. Heavy geocoding traffic should move
  to a self-hosted or commercial geocoder (Nominatim's public service is
  rate-limited and best-effort).
- No metrics/alerting beyond process supervision and the `logs/` trail.

## Architecture

Two independent chains — live fetch/scoring and ML analysis — behind a
fixed-width terminal UI and a Streamlit multipage shell. Neither transport nor
analysis imports Streamlit. See [`CLAUDE.md`](CLAUDE.md) for the full map and the
non-obvious source constraints and statistical guards.
