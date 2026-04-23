#!/usr/bin/env bash
# scripts/fire_webhook.sh — HMAC-sign + POST a Retell webhook locally.
#
# Usage:
#   ./scripts/fire_webhook.sh \
#       --payload tests/fixtures/valid_call_analyzed.json \
#       --tenant  00000000-0000-0000-0000-000000000000 \
#       [--url    http://localhost:8000] \
#       [--times  N]     # fire the same payload N times (same call_id -> SC-2 replay)
#       [--tamper]       # mutate body AFTER signing so signature is invalid (SC-3)
#
# Env fallbacks:
#   REIG_RETELL_API_KEY  — HMAC secret. Must be the workspace API key the
#                          gateway is configured with (REIG_RETELL_API_KEY).
#   REIG_BASE_URL        — default target host. Set to the ngrok URL to
#                          fire at a remote instance.
#
# The signature format mirrors retell-sdk/python's helper:
#   digest = HMAC-SHA256(api_key, body || str(timestamp_ms))
#   header = v={ts},d={hex(digest)}
set -euo pipefail

PAYLOAD=""
TENANT=""
URL="${REIG_BASE_URL:-http://localhost:8000}"
TIMES=1
TAMPER=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --payload) PAYLOAD="$2"; shift 2;;
        --tenant)  TENANT="$2";  shift 2;;
        --url)     URL="$2";     shift 2;;
        --times)   TIMES="$2";   shift 2;;
        --tamper)  TAMPER=1;     shift;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

if [[ -z "$PAYLOAD" || -z "$TENANT" ]]; then
    echo "usage: $0 --payload <path> --tenant <uuid> [--url ...] [--times N] [--tamper]" >&2
    exit 2
fi

API_KEY="${REIG_RETELL_API_KEY:?REIG_RETELL_API_KEY env var required}"

BODY="$(cat "$PAYLOAD")"
TS="$(date +%s%3N)"  # milliseconds (Retell's unit). On macOS `%3N` is BSD-safe via gdate, so fall back:
if [[ "$TS" == *N ]]; then
    TS="$(python3 -c 'import time; print(int(time.time()*1000))')"
fi

# HMAC-SHA256 over `body + str(ts_ms)`.
DIGEST="$(printf '%s%s' "$BODY" "$TS" | openssl dgst -sha256 -hmac "$API_KEY" -hex | awk '{print $NF}')"
SIG="v=${TS},d=${DIGEST}"

# Tamper AFTER signing so the server sees a body that no longer matches.
if [[ "$TAMPER" == "1" ]]; then
    BODY="${BODY} "  # trailing space changes the digest
fi

for i in $(seq 1 "$TIMES"); do
    echo "-> POST $URL/webhooks/retell/$TENANT (attempt $i/$TIMES, tamper=$TAMPER)"
    HTTP_CODE=$(curl -sS -o /tmp/reig_fire_body.$$ -w "%{http_code}" \
        -X POST "$URL/webhooks/retell/$TENANT" \
        -H "x-retell-signature: $SIG" \
        -H "Content-Type: application/json" \
        --data-binary "$BODY")
    echo "   HTTP $HTTP_CODE"
    cat /tmp/reig_fire_body.$$ >/dev/null 2>&1 || true
    rm -f /tmp/reig_fire_body.$$
done
