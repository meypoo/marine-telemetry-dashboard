# Linux deployment path. On Windows, use run_overnight.ps1 instead — in a
# container, the runtime's restart policy (e.g. `--restart unless-stopped`)
# replaces that supervisor.
#
# NOTE: this Dockerfile was written against the pinned requirements but has not
# been built in the development environment (no Docker present there). Build and
# smoke-test before relying on it:
#     docker build -t mehi . && docker run --rm -p 8501:8501 mehi
FROM python:3.13-slim

# curl is only for the container HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime state lives here; a mounted volume persists last-known-good across
# container restarts (data_access writes .cache/, the supervisor writes logs/).
RUN mkdir -p /app/.cache /app/logs
VOLUME ["/app/.cache"]

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

# --server.address 0.0.0.0 so the port is reachable from outside the container.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
