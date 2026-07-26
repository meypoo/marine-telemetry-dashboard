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

**Live Index** (`/`) — the stress score for one of seven curated regions or
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
  `?width=fluid`, `?refresh=<seconds>`, `?alert=<score>` (alert threshold,
  default 70).
- **Environment overrides:** `MEHI_CONTACT` (User-Agent contact for
  Nominatim/Overpass — set a real one when deploying), `MEHI_HTTP_TIMEOUT`,
  `MEHI_DYNAMIC_TTL`, `MEHI_CONTEXT_TTL`, `MEHI_HISTORY_DAYS`,
  `MEHI_OVERPASS_BUDGET`, `MEHI_MAX_CONCURRENT_FETCHES` (aggregate cap on
  concurrent upstream fetches across all sessions, default 4), and
  `DATABASE_URL` / `MEHI_HISTORY_DB` (Postgres URL for durable score history;
  unset falls back to local JSONL — see Hosting below).
- **Concurrency.** The feed caches are process-global and **shared across every
  browser session**, so once one visitor warms a region/day the rest hit cache
  and issue zero upstream calls. Concurrent *cold* misses for the same region are
  coalesced into one fetch (single-flight), and the aggregate is bounded by
  `MEHI_MAX_CONCURRENT_FETCHES`. This is why a single instance comfortably serves
  dozens of viewers; it is also why you should run **one** instance, not replicas
  (replicas would not share the cache).
- **Speed.** A warm rerun is ~12 ms and REFRESH ~1.4 s; a *cold* load for a
  location never seen before is 8-20 s, dominated by Overpass. Searching a new
  place is slower than picking a curated region because it misses both cache
  tiers, uses a larger bounding box, and adds geocoding plus buoy discovery.
- **Not source, not committed:** `.cache/` (runtime snapshots and score
  history) and `logs/`.

## Deployment

Full instructions, an nginx reverse-proxy config, and a docker-compose file are
in [`deploy/`](deploy/README.md). In short:

- **Free, always-on hosting (recommended for a public/portfolio site):**
  [Streamlit Community Cloud](https://streamlit.io/cloud). Push this repo to
  GitHub, point Community Cloud at it, and set secrets in its UI — `MEHI_CONTACT`
  (a real address) and, for durable score history, `DATABASE_URL` (a free
  [Neon](https://neon.tech) serverless Postgres URL). One always-on instance is
  exactly right here: the shared in-process cache does the heavy lifting, so no
  Redis or replicas are needed. Two caveats to know going in: the filesystem is
  **ephemeral** (only *history* needs to survive a redeploy, which is why it goes
  to Postgres — everything else self-heals from cache), and the outbound IP is
  **shared with other apps**, so the Nominatim/Overpass per-IP limits are shared
  — the shared cache, a real `MEHI_CONTACT`, and `MEHI_MAX_CONCURRENT_FETCHES`
  mitigate this. A portfolio app is public; Community Cloud offers a free viewer
  allowlist if you want it restricted (no code). The nginx + Basic-auth path
  below is only for **self-hosting**. See [`deploy/README.md`](deploy/README.md)
  for the step-by-step.
- **Windows:** `run_overnight.ps1` (supervised, self-restarting).
- **Linux/container:** a hardened [`Dockerfile`](Dockerfile) (non-root user) is
  provided; the container runtime's restart policy replaces the supervisor.
  Mount a volume at `/app/.cache` to persist last-known-good data, score history
  and the context disk cache across restarts. *(No Docker was present in the dev
  environment, so the image has not been built here — `docker build` and hit
  `/_stcore/health` before relying on it.)*
- **Put a reverse proxy in front of it — this is required, not optional.**
  Streamlit speaks plain HTTP and has no authentication. `deploy/nginx.conf.example`
  terminates TLS, adds HTTP Basic auth, and carries the WebSocket-upgrade block
  Streamlit needs. Run the app bound to `127.0.0.1` (or on an internal Docker
  network) so it is unreachable except through the proxy.
- **Single instance, on purpose.** Caches are in-process (shared across all
  sessions) plus a disk mirror; do not run multiple replicas — they would not
  share the cache and would multiply upstream load. One instance with the
  shared cache, single-flight coalescing, and the concurrency cap handles dozens
  of concurrent viewers. Public-API rate limits are per source IP; a burst of
  *distinct* new searches from one IP is the residual limit (mitigated, not
  removed).

### Known limitations (by design or scope)

- Auto-refresh only ticks while a browser tab is open (inherent to Streamlit
  fragments); the process stays healthy regardless.
- Single-node by design. In-process caches are shared across sessions but not
  across processes, so this scales *up* (one bigger instance, dozens of viewers)
  rather than *out* (replicas). Score history is the one piece of durable state;
  it goes to Postgres when `DATABASE_URL` is set (else local JSONL). Sustained
  heavy geocoding beyond "dozens" would want a self-hosted or commercial geocoder
  — Nominatim's public service is rate-limited and best-effort.
- No metrics/alerting beyond process supervision and the `logs/` trail.

## Architecture

Two independent chains — live fetch/scoring and ML analysis — behind a
fixed-width terminal UI and a Streamlit multipage shell. Neither transport nor
analysis imports Streamlit. See [`CLAUDE.md`](CLAUDE.md) for the full map and the
non-obvious source constraints and statistical guards.
