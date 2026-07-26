# Linux deployment path. On Windows, use run_overnight.ps1 instead — in a
# container, the runtime's restart policy (e.g. `--restart unless-stopped`)
# replaces that supervisor.
#
# NOTE: no Docker was present in the development environment, so this image has
# not been built here. Build and smoke-test before relying on it:
#     docker build -t mehi . && docker run --rm -p 8501:8501 mehi
#     curl -fsS http://localhost:8501/_stcore/health   # -> ok
# For a proxied, authenticated deployment see deploy/ (nginx + compose).
FROM python:3.13-slim

# curl is only for the container HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user: nothing here needs root, and a compromised process
# should not own the filesystem. The runtime-state dirs are created and handed
# to that user before we drop to it. Streamlit's own on-disk cache lives under
# the user's home, so give the account a real home and point config there.
RUN useradd --create-home --home-dir /home/mehi --shell /usr/sbin/nologin mehi \
    && mkdir -p /app/.cache /app/logs /home/mehi/.streamlit \
    && chown -R mehi:mehi /app /home/mehi
USER mehi
ENV HOME=/home/mehi

# Runtime state; mount a volume here to persist last-known-good, score history
# and the context-tier disk cache across container restarts.
VOLUME ["/app/.cache"]

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

# Bind 0.0.0.0 so the port is reachable from outside the container. Do NOT
# publish this port straight to the internet — put the reverse proxy in deploy/
# in front of it (it terminates TLS and adds authentication). CORS/XSRF stay on;
# the proxy forwards a single trusted origin.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
