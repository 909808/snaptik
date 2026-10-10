# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SNAPTIK_HOST=0.0.0.0 \
    SNAPTIK_PORT=8000

# ffmpeg converts the sound track to MP3.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/snaptik

# Dependencies first so this layer is cached until requirements.txt changes.
# TikTok changes often: rebuild regularly (`docker compose build --pull --no-cache`) to pick up a newer yt-dlp.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10001 --no-create-home snaptik
USER snaptik

EXPOSE 8000

# ProxyHandler({}) keeps the check local even if HTTP(S)_PROXY is set for outbound traffic.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:%s/healthz' % os.environ.get('SNAPTIK_PORT', '8000'), timeout=3)"]

CMD ["python", "-m", "app"]
