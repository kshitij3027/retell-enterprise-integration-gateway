#!/usr/bin/env bash
# scripts/fire_inbound_hydration.sh — fire a signed inbound hydration webhook.
#
# Usage:
#   ./scripts/fire_inbound_hydration.sh \
#       --tenant 00000000-... \
#       --phone  +14155551234 \
#       [--url   http://localhost:8000]
#
# Builds a minimal `call_inbound` payload with the caller's phone, signs
# with REIG_RETELL_API_KEY, and POSTs to /webhooks/retell/<tenant>/inbound.
# Prints the response body so the caller can grep for `caller_name`.
set -euo pipefail

TENANT=""
PHONE=""
URL="${REIG_BASE_URL:-http://localhost:8000}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tenant) TENANT="$2"; shift 2;;
        --phone)  PHONE="$2";  shift 2;;
        --url)    URL="$2";    shift 2;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

if [[ -z "$TENANT" || -z "$PHONE" ]]; then
    echo "usage: $0 --tenant <uuid> --phone <+E164> [--url ...]" >&2
    exit 2
fi

API_KEY="${REIG_RETELL_API_KEY:?REIG_RETELL_API_KEY env var required}"

BODY="$(python3 -c "
import json, sys
print(json.dumps({
    'event': 'call_inbound',
    'call_inbound': {
        'from_number': sys.argv[1],
        'to_number':   '+14085550000',
        'agent_id':    'agent_demo',
    },
}))
" "$PHONE")"

TS="$(python3 -c 'import time; print(int(time.time()*1000))')"
DIGEST="$(printf '%s%s' "$BODY" "$TS" | openssl dgst -sha256 -hmac "$API_KEY" -hex | awk '{print $NF}')"
SIG="v=${TS},d=${DIGEST}"

echo "-> POST $URL/webhooks/retell/$TENANT/inbound (phone=$PHONE)"
RESP_FILE=/tmp/reig_inbound_$$.json
HTTP_CODE=$(curl -sS -o "$RESP_FILE" -w "%{http_code}" \
    -X POST "$URL/webhooks/retell/$TENANT/inbound" \
    -H "x-retell-signature: $SIG" \
    -H "Content-Type: application/json" \
    --data-binary "$BODY")
echo "   HTTP $HTTP_CODE"
cat "$RESP_FILE"
echo
rm -f "$RESP_FILE"
