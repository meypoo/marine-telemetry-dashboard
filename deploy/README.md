# Deployment

The dashboard has **no authentication of its own** and speaks plain HTTP. It is
built to run behind a reverse proxy that terminates TLS and adds auth. Do not
publish the Streamlit port to the internet directly.

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

## Notes and limits

- **Single instance only.** Caches are in-process plus a disk mirror; there is
  no shared cache, so do not run multiple replicas behind a load balancer
  expecting them to share state. One process comfortably serves a small number
  of viewers.
- **Public-API rate limits are per source IP.** Nominatim (geocoding) is
  throttled to 1 request/second *process-wide*, and Overpass sheds load under
  contention. A handful of concurrent users is fine; dozens hammering new
  searches from one egress IP will hit those limits. This is a property of the
  free upstream services, not the app.
- The WebSocket upgrade block in the nginx config is **required** — without it
  the page loads and then hangs, because Streamlit's live channel cannot
  connect.
