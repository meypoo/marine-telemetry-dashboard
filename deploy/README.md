# Deployment

Pick a path by what you need:

- **Free, always-on, public/portfolio → Streamlit Community Cloud** (Path 0
  below). No servers to run; the app stays up when your computer is off. This is
  the recommended path for the "share a link with dozens of people" case.
- **Self-hosted (your own box/VM), or you need auth in front of it → nginx**
  (Paths A/B below). The dashboard has no auth of its own and speaks plain HTTP,
  so behind a reverse proxy that terminates TLS and adds auth. Do not publish the
  Streamlit port to the internet directly.

---

## Path 0 — Streamlit Community Cloud (free, always-on)

Why this fits: one always-on instance, and the feed caches are shared across all
sessions, so dozens of visitors of the same regions issue almost no upstream
calls. No Redis, no replicas, no server to babysit.

1. **Push to GitHub.** Community Cloud deploys from a GitHub repo:
   ```
   git push -u origin production-hardening      # or merge to main and push that
   ```
2. **Create the app** at share.streamlit.io → "New app" → pick the repo/branch,
   main file `app.py`.
3. **Set secrets** (app → Settings → Secrets), TOML format:
   ```toml
   MEHI_CONTACT = "you@example.com"          # real address (Nominatim/Overpass etiquette)
   DATABASE_URL = "postgresql://user:pass@host/db?sslmode=require"   # optional; see below
   ```
   Streamlit exposes these as environment variables, which is exactly what the
   app reads.
4. **Durable score history (recommended): a free Neon Postgres.**
   - Create a free project at neon.tech; copy its connection string into
     `DATABASE_URL` above. No schema setup needed — the app creates its table on
     first write.
   - Neon auto-suspends when idle and fast-resumes on the next query, which suits
     a sporadic portfolio; the app reconnects transparently after a resume.
   - **Without** `DATABASE_URL`, history falls back to local disk, which
     Community Cloud **wipes on every redeploy** — so set it if you want the
     stress-trend panel to persist. Everything else self-heals from cache and is
     fine on the ephemeral filesystem.

**Access:** a portfolio app is normally left **public**. If you want it
restricted, Community Cloud's free viewer allowlist (Settings → Sharing) gates it
by email with no code — you do **not** need the nginx Basic-auth path for this.

**Known caveats of the free tier:** the outbound IP is shared with other apps, so
Nominatim/Overpass per-IP limits are shared — the shared cache, a real
`MEHI_CONTACT`, and `MEHI_MAX_CONCURRENT_FETCHES` (default 4) keep this in check.
The app also idles after inactivity and wakes on the next visit (a few seconds).

*Alternative:* Hugging Face Spaces (Streamlit SDK) works the same way with a
`Dockerfile` or the native Streamlit runtime; confirm current free-tier terms.

---

The self-hosted paths below are for running on your own machine or VM.

Two supported shapes:

## A. Bare host (systemd or `run_overnight.ps1`) + nginx

1. Run Streamlit bound to localhost so only nginx can reach it:
   ```
   streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
   ```
   (On Windows, `run_overnight.ps1` supervises it; add the address flag there.)
2. `cp deploy/nginx.conf.example /etc/nginx/sites-available/mehi` and edit
   `server_name` and the certificate paths.
3. Create the password file and a certificate:
   ```
   htpasswd -c /etc/nginx/.htpasswd yourname
   certbot --nginx -d dashboard.example.com
   ```
4. Enable the site and reload nginx.

## B. Docker Compose (app + nginx, only 443 exposed)

```
docker build -t mehi .
cd deploy
cp nginx.conf.example nginx.conf          # then edit server_name + cert paths
htpasswd -c ./htpasswd yourname
# put fullchain.pem + privkey.pem in ./certs (or mount a certbot volume)
docker compose -f docker-compose.yml.example up -d
```

**One change for compose:** in `nginx.conf`, point the proxy at the app service
name instead of localhost — `proxy_pass http://app:8501;`. On a bare host it
stays `http://127.0.0.1:8501;`.

## Checklist before going live

- [ ] Streamlit is **not** reachable except through the proxy (`curl` the host
      IP on 8501 from outside — it should refuse).
- [ ] TLS certificate is valid and HTTP redirects to HTTPS.
- [ ] Basic-auth prompt appears and rejects a wrong password.
- [ ] `MEHI_CONTACT` is set to a real address (Nominatim/Overpass etiquette).
- [ ] The `.cache` volume is mounted and writable, so last-known-good, score
      history and the context disk cache survive a restart.
- [ ] Health check passes: `curl -fsS https://.../\_stcore/health` → `ok`.

## Environment variables

Set these wherever the process runs (systemd unit, compose `environment:`,
Community Cloud Secrets):

| Variable | Purpose |
|---|---|
| `MEHI_CONTACT` | Real contact in the Nominatim/Overpass User-Agent. Set it. |
| `DATABASE_URL` (or `MEHI_HISTORY_DB`) | Postgres URL for **durable score history**; unset → local JSONL (wiped on redeploy on ephemeral hosts). |
| `MEHI_MAX_CONCURRENT_FETCHES` | Aggregate cap on concurrent upstream fetches across all sessions (default 4). Lower it if the shared egress IP trips Overpass/Nominatim limits. |
| `MEHI_DYNAMIC_TTL`, `MEHI_CONTEXT_TTL`, `MEHI_HISTORY_DAYS`, `MEHI_HTTP_TIMEOUT`, `MEHI_OVERPASS_BUDGET` | Tuning knobs; defaults are sensible. |

## Notes and limits

- **Single instance, on purpose.** The feed caches are in-process but **shared
  across every browser session**, so once one viewer warms a region the rest hit
  cache. Concurrent cold misses for the same region are coalesced to one fetch,
  and `MEHI_MAX_CONCURRENT_FETCHES` bounds the aggregate. This scales *up* (one
  instance, dozens of viewers), not *out* — do not run replicas, they would not
  share the cache. Score history is the only durable state (`DATABASE_URL`).
- **Public-API rate limits are per source IP.** Nominatim (geocoding) is
  throttled to 1 request/second *process-wide*, and Overpass sheds load under
  contention. Steady-state is fine thanks to the shared cache; a burst of
  *distinct* new searches from one IP is the residual limit. A property of the
  free upstream services, not the app.
- The WebSocket upgrade block in the nginx config is **required** — without it
  the page loads and then hangs, because Streamlit's live channel cannot
  connect.
