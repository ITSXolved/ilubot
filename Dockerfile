# ── Ilubot Study Tutor: web widget + webhook API container ─────────────────────
# Serves the 3D tutor UI (/ and /embed.js), the WebSocket call (/ws) and the
# webhook API (/api/v1/*). Sessions/history live in Redis (REDIS_URL).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docker.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements-docker.txt

COPY web/ ./web/
COPY avatar/ ./avatar/
COPY core/ ./core/
COPY actions/ ./actions/
COPY memory/ ./memory/
COPY providers/ ./providers/
COPY config/ ./config/
COPY headless_server.py .

# Run unprivileged; config/ stays writable for the saved teaching-language setting.
RUN useradd --create-home --uid 10001 ilubot && chown -R ilubot:ilubot /app/config
USER ilubot

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

EXPOSE 8080

CMD ["python", "headless_server.py"]
