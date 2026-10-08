# Debian slim, not Alpine: the Claude Agent SDK wheel bundles a glibc Claude Code binary.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DB=/data/state.db \
    ROUTING_CONFIG=/config/config.yaml \
    HOME=/home/app

RUN useradd --create-home --uid 10001 app && mkdir -p /data /config && chown app /data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

# One worker on purpose: the per-Issue lock and SQLite state live in this process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
