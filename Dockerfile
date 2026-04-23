# REIG — FastAPI gateway image.
# Pins Python 3.12-slim; installs build deps for psycopg/asyncpg; bakes the
# spaCy en_core_web_sm model into the image layer so Presidio has zero cold
# start at first request. The CMD is deliberately omitted — docker-compose
# supplies `uvicorn app.main:app ...` so we can override it in tests.
#
# Why en_core_web_sm (~15 MB) instead of en_core_web_lg (~560 MB):
# our default REIG_PII_ENTITIES (PHONE_NUMBER, US_SSN, EMAIL_ADDRESS,
# CREDIT_CARD) are all pattern-based — the spaCy NER model is only
# load-bearing for PERSON/LOCATION/ORGANIZATION entities (not in our
# default list). Smaller model = faster cold boot for CR-16 / SC-11.
# A production deployment that adds NER entities can override via
# REIG_PII_SPACY_MODEL=en_core_web_lg.

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

# --- Pre-bake the small spaCy model into the image layer ---
# Without this, first Presidio call would download at runtime and fail in
# an air-gapped demo. `en_core_web_sm` keeps the build step bounded to
# ~30 s instead of the 2-4 min `en_core_web_lg` would cost; see the
# header comment for the rationale.
RUN python -m spacy download en_core_web_sm

# --- Application source ---
# C5: `adapters/` ships alongside `app/` — CRMAdapter Protocol + Salesforce
# skeleton + ServiceNow stub live here (CR-11).
COPY app/ ./app/
COPY adapters/ ./adapters/
COPY migrations/ ./migrations/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY pyproject.toml ./

# Expose the uvicorn port. The actual CMD is set by docker-compose.
EXPOSE 8000
