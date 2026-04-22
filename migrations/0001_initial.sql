-- migrations/0001_initial.sql
-- REIG initial schema. Runs once via Postgres initdb (the compose volume mount
-- drops this file into /docker-entrypoint-initdb.d on first boot).
--
-- Two Postgres roles:
--   * `reig`    (POSTGRES_USER, default superuser) — owns schema, runs migrations.
--   * `reig_app` (no SUPERUSER, no BYPASSRLS) — used by the FastAPI runtime.
--
-- Every tenant-scoped table has Row-Level Security with a policy that reads
-- `current_setting('app.tenant_id', true)`. The `true` second arg means the
-- setting is OPTIONAL — if missing, `current_setting` returns NULL, the
-- `tenant_id = NULL::uuid` comparison is NULL, and NULL != TRUE — so the
-- policy default-denies instead of throwing. This is the critical RLS
-- default-deny pattern; see CR-5.
--
-- audit_log and processed_events additionally have UPDATE/DELETE revoked from
-- reig_app so they are append-only at the SQL layer (CR-14).

-- =========================================================================
-- Extensions
-- =========================================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =========================================================================
-- Application role (used by the FastAPI process)
-- =========================================================================
-- NB: no SUPERUSER, no BYPASSRLS — RLS policies actually fire for this role.
-- Password matches nothing in .env because the app connects as `reig` today
-- (v2: switch app to `reig_app` once SET LOCAL wiring lands in C2).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reig_app') THEN
        CREATE ROLE reig_app WITH LOGIN PASSWORD 'reig_app';
    END IF;
END
$$;

-- =========================================================================
-- Tenants (catalog — no RLS, tenant_id IS the primary key)
-- =========================================================================
CREATE TABLE tenants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    profile         text NOT NULL,                   -- e.g. 'lending', 'healthcare'
    phi_mode        boolean NOT NULL DEFAULT false,
    active_adapter  text NOT NULL DEFAULT 'salesforce',
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- =========================================================================
-- API keys
-- =========================================================================
CREATE TABLE api_keys (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash    text NOT NULL UNIQUE,                -- sha256 hex
    key_prefix  text NOT NULL,                       -- e.g. 'reig_' (greppable)
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX api_keys_tenant_idx ON api_keys(tenant_id);

-- =========================================================================
-- Calls (one row per retell call_id per tenant)
-- =========================================================================
CREATE TABLE calls (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    call_id                 text NOT NULL,
    raw_transcript_encrypted bytea,                   -- pgcrypto ciphertext (PHI mode)
    metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, call_id)
);
CREATE INDEX calls_tenant_idx ON calls(tenant_id);
CREATE INDEX calls_call_id_idx ON calls(call_id);

-- =========================================================================
-- Processed events (dedup table; append-only)
-- =========================================================================
CREATE TABLE processed_events (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    call_id      text NOT NULL,
    event_type   text NOT NULL,                       -- call_started | call_ended | call_analyzed
    raw_payload  jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, call_id, event_type)
);
CREATE INDEX processed_events_tenant_idx ON processed_events(tenant_id);

-- =========================================================================
-- CRM writes (audit of every downstream upsert attempt)
-- =========================================================================
CREATE TABLE crm_writes (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    call_id        text NOT NULL,
    adapter        text NOT NULL,                     -- 'salesforce' | 'servicenow_stub'
    status         text NOT NULL CHECK (status IN ('pending','success','failed','dead')),
    sfdc_lead_id   text,
    error_context  jsonb,
    attempts       int NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX crm_writes_tenant_idx ON crm_writes(tenant_id);
CREATE INDEX crm_writes_call_id_idx ON crm_writes(call_id);

-- =========================================================================
-- OAuth / adapter credentials (refresh_token encrypted with pgcrypto)
-- =========================================================================
CREATE TABLE credentials (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    adapter                  text NOT NULL,
    access_token_cached      text,
    access_token_expires_at  timestamptz,
    refresh_token_encrypted  bytea,
    instance_url             text,
    updated_at               timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, adapter)
);
CREATE INDEX credentials_tenant_idx ON credentials(tenant_id);

-- =========================================================================
-- Audit log (append-only — UPDATE/DELETE revoked below)
-- =========================================================================
CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    tenant_id   uuid,                                 -- nullable: signature.failed before tenant known
    event_type  text NOT NULL,
    call_id     text,
    actor       text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace_id    text,
    source_ip   inet,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_tenant_idx ON audit_log(tenant_id);
CREATE INDEX audit_log_created_at_idx ON audit_log(created_at DESC);

-- =========================================================================
-- Row-Level Security
-- =========================================================================
-- `tenants` is the catalog — no RLS on it, tenant_id IS the PK.
-- Every other table has: ENABLE RLS + a policy keyed on app.tenant_id.
-- The `, true` in current_setting means "missing is NULL, not exception" —
-- default-deny when the app forgets to SET LOCAL.

ALTER TABLE api_keys         ENABLE ROW LEVEL SECURITY;
ALTER TABLE calls            ENABLE ROW LEVEL SECURITY;
ALTER TABLE processed_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE crm_writes       ENABLE ROW LEVEL SECURITY;
ALTER TABLE credentials      ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log        ENABLE ROW LEVEL SECURITY;

CREATE POLICY api_keys_tenant_isolation ON api_keys
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY calls_tenant_isolation ON calls
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY processed_events_tenant_isolation ON processed_events
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY crm_writes_tenant_isolation ON crm_writes
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY credentials_tenant_isolation ON credentials
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

CREATE POLICY audit_log_tenant_isolation ON audit_log
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

-- =========================================================================
-- Permissions for the application role
-- =========================================================================
GRANT USAGE ON SCHEMA public TO reig_app;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO reig_app;
GRANT UPDATE ON tenants, api_keys, calls, credentials, crm_writes TO reig_app;

-- Append-only tables: deny mutation even to the app role. CR-14.
REVOKE UPDATE, DELETE ON audit_log         FROM reig_app;
REVOKE UPDATE, DELETE ON processed_events  FROM reig_app;

-- Sequences: app needs USAGE to insert into bigserial (audit_log.id).
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO reig_app;
