# retell-enterprise-integration-gateway

A Python/FastAPI middleware that sits between **Retell** webhooks and **Salesforce**, enforcing signature verification, idempotency, multi-tenant isolation via Postgres RLS, and PII redaction. Built around a `CRMAdapter` Protocol so a second downstream system (ServiceNow) can be added without touching the middleware.

---

## What it does

Retell fires webhooks on voice-agent lifecycle events (`call_started`, `call_ended`, `call_analyzed`, inbound-call hydration). This gateway is the hardened boundary between Retell and the customer's CRM:

- **Signature verification** — every inbound webhook is HMAC-verified before it touches business logic.
- **Idempotency** — webhook IDs are deduplicated at the DB layer; replays are safe.
- **Multi-tenant isolation** — tenant scoping is enforced by **Postgres Row-Level Security**, not application code. A bug in a route handler cannot leak tenant A's data to tenant B.
- **PII redaction** — phone numbers, emails, and free-text transcripts are redacted before they are logged, traced, or persisted to the audit log.
- **Append-only audit log** — every inbound webhook and every outbound CRM write is recorded.
- **Pluggable downstream via `CRMAdapter` Protocol** — Salesforce is the reference implementation; ServiceNow can be added as a sibling adapter without changes to routes, middleware, persistence, or retry logic.
- **In-process bounded-retry** — outbound CRM writes retry with exponential backoff inside the FastAPI process. No separate worker in v1.
- **Distributed tracing** — OpenTelemetry spans exported to Jaeger cover the full path from webhook receipt to CRM ack.

---

## Architecture

Long-lived multi-container stack orchestrated by Docker Compose. Three services:

| Service | Role |
|---|---|
| `api` (FastAPI) | Webhook ingestion, inbound-call hydration endpoint, admin REST API, and in-process CRM writer with bounded retry. |
| `db` (Postgres 16) | Tenants, webhook dedup table, append-only audit log. Row-Level Security enabled on all tenant-scoped tables. |
| `jaeger` (Jaeger all-in-one) | Trace collector + UI for visualizing webhook → CRM spans. |

```
  Retell ──(HTTPS + HMAC)──▶ ngrok ──▶ [ api (FastAPI) ] ──▶ [ db (Postgres 16, RLS) ]
                                             │
                                             ├──▶ Salesforce  (CRMAdapter impl)
                                             └──▶ ServiceNow  (future CRMAdapter impl)
                                             │
                                             └──(OTLP)──▶ [ jaeger ]
```

The FastAPI process owns both ingestion **and** downstream writes. There is no Celery/RQ/worker tier in v1 — the bounded-retry path lives inside the request lifecycle (with backgrounded retry for transient failures).

---

## Endpoints (surface area)

- `POST /webhooks/retell` — signed Retell webhook receiver.
- `POST /calls/hydrate` — inbound-call hydration, returning dynamic variables to Retell at call start.
- `GET  /admin/tenants`, `POST /admin/tenants`, etc. — small admin REST API for tenant onboarding and audit-log inspection.
- `GET  /healthz`, `GET  /readyz` — liveness/readiness.

---

## How it runs

1. `docker compose up` brings up `api`, `db`, `jaeger`.
2. `ngrok http 8000` exposes the FastAPI server publicly.
3. The ngrok URL is registered in the Retell dashboard as the webhook destination.
4. Operator drives the system via a **Postman collection** + `curl` scripts (admin onboarding, replay testing, PII-redaction checks).
5. The demo is driven by **real inbound phone calls** into a Retell-provisioned number.

Jaeger UI is available at `http://localhost:16686` for trace inspection.

---

## The `CRMAdapter` Protocol

The middleware never imports a CRM SDK directly. It depends only on:

```python
class CRMAdapter(Protocol):
    async def upsert_contact(self, tenant_id: UUID, payload: ContactPayload) -> CRMResult: ...
    async def create_call_activity(self, tenant_id: UUID, payload: CallActivityPayload) -> CRMResult: ...
    async def attach_transcript(self, tenant_id: UUID, payload: TranscriptPayload) -> CRMResult: ...
```

- `SalesforceAdapter` — reference implementation (v1).
- `ServiceNowAdapter` — added later as a sibling file. No route, middleware, persistence, or retry code changes.

Adapter selection is per-tenant configuration, stored in `db` and resolved at request time.

---

## Tech stack

**Backend (Python):** FastAPI, Pydantic v2, SQLAlchemy 2.x (async) + asyncpg, Alembic, httpx, tenacity, structlog, OpenTelemetry SDK + OTLP exporter.

**Data:** Postgres 16 with Row-Level Security and an append-only audit log.

**Observability:** Jaeger all-in-one (OTLP in, UI out).

**Tooling (JS):** Postman collection (JSON), small Node helper scripts for local tasks (webhook signing, payload generators). Node is **not** part of the runtime stack — it is tooling only.

**Infra:** Docker Compose, ngrok.

---

## Repository layout (planned)

```
.
├── README.md
├── requirements.txt
├── .gitignore
├── docker-compose.yml            # (not generated yet)
├── Dockerfile                    # (not generated yet)
├── src/                          # (not generated yet)
│   ├── api/                      # FastAPI routes
│   ├── adapters/                 # CRMAdapter Protocol + Salesforce impl
│   ├── middleware/               # signature verification, PII redaction, tenant resolution
│   ├── db/                       # models, RLS policies, migrations
│   ├── observability/            # OTel setup, structlog config
│   └── main.py
├── migrations/                   # Alembic
├── tests/
├── scripts/                      # curl + Node helpers
└── postman/                      # exported collection
```

---

## Status

Scaffolding only — `README.md`, `requirements.txt`, `.gitignore` are in place. No application code, no Dockerfiles, no compose file yet. Implementation has not started and will not start until explicitly authorized.
