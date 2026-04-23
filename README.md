# retell-enterprise-integration-gateway

[![CI](https://github.com/kshitij3027/retell-enterprise-integration-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/kshitij3027/retell-enterprise-integration-gateway/actions/workflows/ci.yml)

A Python/FastAPI middleware between **Retell** webhooks and **Salesforce**, enforcing signature verification, idempotency, multi-tenant isolation via Postgres RLS, PII redaction, and pluggable CRM adapters. Built around a `CRMAdapter` Protocol so a second downstream (ServiceNow) can be added without touching the middleware core.

---

## What it does

Retell fires webhooks on voice-agent lifecycle events (`call_started`, `call_ended`, `call_analyzed`, `call_inbound`). REIG is the hardened boundary between Retell and the CRM:

- **HMAC signature verification** (CR-2) — every inbound webhook is verified before any business logic runs; tampered bodies return 401 and land in the audit log.
- **Idempotency** (CR-3) — `ON CONFLICT (tenant_id, call_id, event_type) DO NOTHING` collapses replay storms to exactly one downstream write.
- **Multi-tenant isolation** (CR-5) — tenant scoping enforced at the Postgres RLS layer under a non-BYPASSRLS role, so an app-tier bug cannot leak data across tenants.
- **PII redaction** (CR-10) — Microsoft Presidio rewrites SSN, email, phone, and credit-card spans with `<TYPE_REDACTED>` tokens **before** any data flows into Salesforce or appears in the audit log.
- **pgcrypto-encrypted refresh tokens** (CR-7) — Salesforce OAuth refresh tokens live in `credentials.refresh_token_encrypted` encrypted with `REIG_ENCRYPTION_KEY`; a DB-only compromise can't forge OAuth sessions.
- **tenacity-retried CRM upserts** (CR-9) — 5 attempts, exponential backoff with jitter, capped at 60 s, on `TransientError` only; 4xx other than `401 INVALID_SESSION_ID` bubbles out immediately.
- **Append-only audit log** (CR-14) — `UPDATE` / `DELETE` revoked from the runtime role at the SQL layer; `signature.failed`, `dedup.hit/miss`, `adapter.upsert.success/failed`, etc., are all tamper-evident.
- **Pluggable downstream via `CRMAdapter` Protocol** (CR-11) — Salesforce is the reference impl; ServiceNow ships as a stub that structurally satisfies the Protocol (mypy --strict verified).
- **OpenTelemetry → Jaeger** (CR-13) — seven named spans (`webhook.received`, `signature.verified`, `dedup.checked`, `pii.redacted`, `oauth.refreshed`, `adapter.upsert`, `audit.written`) with `retell.call_id`, `tenant.id`, `sfdc.lead_id` attributes queryable in Jaeger UI.

---

## Architecture

```
               +-----------------+               +----------------+
               |      Retell     |   webhooks    |                |
   caller  --> |  voice agent    |  --POST-->    |      REIG      |  --PATCH-->  Salesforce
               |                 |  (HMAC-signed)| (FastAPI +     |             (Lead /
               |                 |               |  Postgres RLS) |              SOQL)
               +-----------------+               +----------------+
                                                        |
                                                        v
                                                   +---------+
                                                   | Jaeger  |
                                                   | (OTLP)  |
                                                   +---------+
```

Three services defined in `docker-compose.yml`:

| Service | Image | Purpose |
|---|---|---|
| `api` | `reig-api:local` (built from `./Dockerfile`) | FastAPI app, uvicorn on :8000. spaCy + Presidio baked in. |
| `db` | `postgres:16-alpine` | 7-table schema under RLS + pgcrypto. Migrations auto-applied on first boot. |
| `jaeger` | `jaegertracing/all-in-one:latest` | OTLP collector (4317) + trace UI (16686). |

Request flow for an outbound `call_analyzed` webhook:

1. `POST /webhooks/retell/{tenant_id}` hits FastAPI.
2. `verify_retell_signature` — HMAC-SHA256 over body+timestamp, 5 min skew. Fail → 401 + `signature.failed` audit row.
3. `claim_event` — atomic `INSERT … ON CONFLICT DO NOTHING RETURNING id`. Hit → 204 + `dedup.hit`. Miss → proceed.
4. `process_call_analyzed` (BackgroundTask) — redact transcript via Presidio, upsert `calls.metadata`, `resolve_adapter` → `SalesforceAdapter`.
5. `authenticate()` — load + decrypt refresh token; refresh access_token if < 60 s to expiry (`oauth.refreshed` span).
6. `upsert_record` — `PATCH /services/data/v60.0/sobjects/Lead/External_Call_Id__c/{call_id}` with tenacity retry on 429/5xx.
7. Write `crm_writes.status='success'` + `adapter.upsert.success` audit row + span with `sfdc.lead_id`.

Inbound hydration (`POST /webhooks/retell/{tenant_id}/inbound`) uses the same adapter's `lookup_by_phone(...)` and responds with `{"dynamic_variables":{...}}` under a **1.8 s** hard budget so Retell's 2 s SLA is always green.

---

## Demo walkthrough

### Preflight (one-time, human)

See [docs/PREFLIGHT.md](docs/PREFLIGHT.md) for the full list. The short version:

1. Retell dashboard → Settings → API Keys → copy `REIG_RETELL_API_KEY`.
2. ngrok → reserve a static domain → `REIG_NGROK_DOMAIN`.
3. Salesforce → Setup → App Manager → New External Client App, OAuth enabled, scopes `api + refresh_token`, IP relaxation **"Relax IP restrictions"**, Callback URL `https://<ngrok>/admin/oauth/callback`. Copy Consumer Key + Secret → `REIG_SFDC_CLIENT_ID` / `REIG_SFDC_CLIENT_SECRET`.
4. Salesforce → Object Manager → Lead → Fields → New custom field `External_Call_Id__c` (text, length 64, **External ID + Unique**).
5. `openssl rand -base64 32` → `REIG_ENCRYPTION_KEY`.

### Bring up the stack

```bash
cp .env.example .env         # fill in the real values from preflight
make prepare                  # pulls base images + builds the api image (~3 min first run)
make demo                     # boots db + jaeger + api; healthz returns 200 in ≤ 90 s
make seed                     # creates the two demo tenants + issues API keys
                              # -> writes .env.local.seeds with TENANT_LENDING_ID / _KEY
```

### Drive the pipeline

```bash
# Pick up the seeded tenant IDs/keys.
source .env.local.seeds

# 1. Fire a redacted call_analyzed webhook.
make fire-webhook \
    PAYLOAD=tests/fixtures/valid_call_analyzed_with_pii.json \
    TENANT=$TENANT_LENDING_ID

# 2. Look at the Jaeger trace (http://localhost:16686) — service
#    `retell-integration-gateway`, search by retell.call_id.

# 3. Check Salesforce → Leads → filter External_Call_Id__c.
#    Description should contain <US_SSN_REDACTED> + <CREDIT_CARD_REDACTED>,
#    no raw digits.

# 4. Replay 5x (SC-2) — still exactly one Lead.
make fire-webhook \
    PAYLOAD=tests/fixtures/valid_call_analyzed_with_pii.json \
    TENANT=$TENANT_LENDING_ID \
    TIMES=5

# 5. Tamper (SC-3) — 401 returned, signature.failed audit row written.
make fire-webhook \
    PAYLOAD=tests/fixtures/valid_call_analyzed_with_pii.json \
    TENANT=$TENANT_LENDING_ID \
    TAMPER=1
```

### Inbound hydration

```bash
make fire-inbound TENANT=$TENANT_LENDING_ID PHONE=+14155551234
# -> {"dynamic_variables":{"caller_name":"Jane Doe","last_interaction":"2026-04-10"}}
```

### Verification stories (bowser-qa-agent)

`verification/stories.yaml` ships two stories that cover SC-1/2/3/5/6 (end-to-end call_analyzed + Jaeger + SFDC) and SC-8 (inbound hydration greets by name). Runnable via `/ui-review` in Claude Code.

---

## Security model

| Layer | Control |
|---|---|
| Webhook ingress | HMAC-SHA256 on `body+timestamp_ms`, 5 min skew window, constant-time compare. Tamper → 401 + audit row + source IP logged. |
| API auth | `X-API-Key`, SHA-256 hashed at rest, compared via `hmac.compare_digest`. Bootstrap SELECT admitted only under `app.bootstrap='true'`. |
| Tenant isolation | Postgres RLS on every tenant-scoped table; runtime role has no `BYPASSRLS`. Default-deny when `app.tenant_id` is unset (via `NULLIF(..., '')::uuid`). |
| Credential storage | `credentials.refresh_token_encrypted` bytea via pgcrypto `pgp_sym_encrypt(..., current_setting('app.encryption_key'))`. Key lives in env, not DB. |
| PII | Presidio `AnalyzerEngine` + `AnonymizerEngine` replace detected spans with `<TYPE_REDACTED>` **before** any persist / CRM write. |
| Audit | `audit_log.UPDATE/DELETE` revoked from the runtime role. Replay immutable — `processed_events` same treatment. |
| Cross-tenant guard | Middleware regex on `/admin/tenants/<uuid>/...` re-asserts path-param UUID matches authenticated tenant (403 otherwise). |

---

## Roadmap (CR-18 scoping-judgment deliverable)

**What we didn't build, and why that's correct for a 2-3 day build:**

Core v1 is the 18 CRs. Everything below is deliberately deferred because it either (a) depends on a load profile we haven't measured yet, (b) is a second-order concern that doesn't gate the demo, or (c) doesn't improve the reviewer's "does the core work?" story.

### Area A — Mid-call custom function

* **`/tools/lookup-customer/{tenant_id}`** — mid-call Retell function that invokes an SFDC lookup and returns live fields to the agent during the conversation. High-impact if latency budget fits; the `/inbound` hydration path already covers the pre-call case. **Deferred** as the strongest single enhancement to revisit.

### Area B — Reliability hardening

* **Circuit breaker (`pybreaker`)** — protects SFDC from hammering during outages. Our tenacity budget caps at 5 attempts × 60 s = bounded; a breaker matters once per-second call volume exceeds manual recovery bandwidth.
* **Rate limiter (`aiolimiter`) honouring `Sforce-Limit-Info`** — same story. **Deferred** until we measure a live call rate.
* **`gitleaks` pre-commit hook** — defensive, not load-bearing for demo.

### Area C — Durable outbox

* **`crm_outbox` table + `FOR UPDATE SKIP LOCKED` worker** — decouples the webhook response from the CRM write. For `< 100 rps` the tenacity-in-BackgroundTask path holds; outbox is the right surgery once sustained load exceeds the FastAPI request lifecycle. **Deferred** until a measured need.

### Bonus B-1..B-6

* **Replay script** — superseded by `scripts/fire_webhook.sh --times N` which covers the same demo need.
* **Locust load test** — worth adding once we have a target rps.
* **Node signing sidecar** — only relevant if we add tenants that can't call `openssl dgst`.
* **SECURITY.md** — worth adding once the repo has external consumers.
* **Postman collection** — nice-to-have for humans evaluating adapters.
* **SFDC outage drill** — the tenacity + `crm_writes.status='failed'` path already exercises this on every CI run via `test_upsert_5x_503_exhausts_retries`.

### Other backlog

* **Epic adapter stub** — parallel to ServiceNow; proves the CRMAdapter Protocol generalises beyond two implementers. Small diff; one day's work.
* **Hash-chained audit log** — upgrade path from REVOKE-based immutability. Each row's hash includes the previous row's hash; tampering becomes globally detectable. Nontrivial migration.
* **Admin UI** — Postman + curl + `scripts.cli` cover v1.

### How we scoped

The 18 CRs plus 11 SCs were treated as a hard gate. Everything above was considered by name and rejected for v1 on a "does it move demo-day outcome?" basis. The audit log format, the RLS policy shape, and the CRMAdapter Protocol are the three decisions most expensive to change later; everything else is inside a single file that a second engineer can refactor in isolation.

---

## Operating

| Task | Command |
|---|---|
| Run the full test suite | `make test` |
| Tail logs | `make logs` |
| Teardown (keep data) | `make down` |
| Teardown (wipe volumes) | `make clean` |
| Create a tenant | `docker compose run --rm api python -m scripts.cli create-tenant --name Acme --profile consumer-lending` |
| Issue an API key | `docker compose run --rm api python -m scripts.cli issue-key --tenant-id <uuid>` |

---

## Requirements traceability

| CR | Artefact |
|---|---|
| CR-1 | [app/routes/webhooks.py](app/routes/webhooks.py) — 204 within 2 s, BackgroundTasks deferred |
| CR-2 | [app/signature.py](app/signature.py) — HMAC + 5 min skew + audit on fail |
| CR-3 | [app/dedup.py](app/dedup.py) — ON CONFLICT DO NOTHING + [tests/test_dedup.py](tests/test_dedup.py) |
| CR-4 | [app/routes/webhooks.py](app/routes/webhooks.py) — only `call_analyzed` fires adapter |
| CR-5 | [migrations/0001_initial.sql](migrations/0001_initial.sql) + [app/db.py](app/db.py) — RLS + SET LOCAL dependency |
| CR-6 | [app/auth.py](app/auth.py) — hash_key / verify_key / generate_key |
| CR-7 | [migrations/0003_pgcrypto_helpers.sql](migrations/0003_pgcrypto_helpers.sql) — encrypt/decrypt_refresh_token |
| CR-8 | [adapters/salesforce.py](adapters/salesforce.py) — authenticate + OAuth refresh |
| CR-9 | `upsert_record` in [adapters/salesforce.py](adapters/salesforce.py) — tenacity retry |
| CR-10 | [app/pii.py](app/pii.py) — Presidio singletons + redact |
| CR-11 | [adapters/base.py](adapters/base.py) — Protocol + ServiceNow stub |
| CR-12 | [app/routes/inbound.py](app/routes/inbound.py) — 1.8 s hydration budget |
| CR-13 | [app/tracing.py](app/tracing.py) + seven named spans |
| CR-14 | [app/audit.py](app/audit.py) + `REVOKE UPDATE, DELETE` in migration 0001 |
| CR-15 | [scripts/seed.py](scripts/seed.py) — lending + health tenants |
| CR-16 | `make demo` + image layer-caching via `make prepare` |
| CR-17 | [.github/workflows/ci.yml](.github/workflows/ci.yml) — ruff + mypy --strict + pytest + smoke |
| CR-18 | This README's Roadmap section |

| SC | Verification |
|---|---|
| SC-1 | Real call demo (Final E2E F11) + [tests/test_pipeline_redact.py](tests/test_pipeline_redact.py) |
| SC-2 | [tests/test_dedup.py](tests/test_dedup.py) + `verification/stories.yaml` replay block |
| SC-3 | [tests/test_signature.py](tests/test_signature.py) + stories.yaml tamper block |
| SC-4 | [tests/test_rls_cross_tenant.py](tests/test_rls_cross_tenant.py) |
| SC-5 | [tests/test_pipeline_redact.py](tests/test_pipeline_redact.py) + stories.yaml redaction assertions |
| SC-6 | [tests/test_otel_spans.py](tests/test_otel_spans.py) + stories.yaml Jaeger block |
| SC-7 | [tests/test_adapter_protocol.py](tests/test_adapter_protocol.py) + `mypy --strict adapters/` |
| SC-8 | [tests/test_inbound_hydration.py](tests/test_inbound_hydration.py) + stories.yaml hydration block |
| SC-9 | [tests/test_audit_immutability.py](tests/test_audit_immutability.py) |
| SC-10 | CI badge above |
| SC-11 | [tests/test_cold_boot.py](tests/test_cold_boot.py) |
