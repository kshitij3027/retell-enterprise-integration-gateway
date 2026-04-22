# REIG — FastAPI gateway image.
# Pins Python 3.12-slim; installs build deps for psycopg/asyncpg; bakes the
# spaCy en_core_web_lg model into the image layer so Presidio has zero cold
# start at first request. The CMD is deliberately omitted — docker-compose
# supplies `uvicorn app.main:app ...` so we can override it in tests.

FROM python:3.12-slim

# --- System deps ---
# build-essential + libpq-dev: needed to compile psycopg / asyncpg extensions
# curl: used inside the container by CI smoke and healthcheck loops
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps (copied first so layer caches on unchanged requirements) ---
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# --- Pre-bake the 560 MB spaCy model into the image layer ---
# Without this, first Presidio call would download at runtime and fail in
# an air-gapped demo. This is the single heaviest build step (~2-4 min).
RUN python -m spacy download en_core_web_lg

# --- Application source ---
# `adapters/` does not exist in C1 (lands in C5) — guarded copy so build works.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY pyproject.toml ./

# Expose the uvicorn port. The actual CMD is set by docker-compose.
EXPOSE 8000
