-- migrations/0002_api_keys_bootstrap_policy.sql
-- C2: Allow the auth middleware to look up api_keys by key_hash
-- without a tenant context, by SET LOCAL app.bootstrap='true' inside the tx.
--
-- The C1 policy required app.tenant_id to already be set — but the auth
-- middleware needs to look up (tenant_id, key_hash) BEFORE any tenant is
-- known (chicken-and-egg). Instead of a second DB pool or a second policy,
-- we extend the single api_keys policy to allow SELECT when the transaction
-- opts into bootstrap mode via `SET LOCAL app.bootstrap = 'true'`.
--
-- SELECT path  : OR allows bootstrap.
-- INSERT path  : WITH CHECK still requires app.tenant_id = row.tenant_id, so
--                nobody can insert into api_keys without picking a tenant.
--
-- Why NULLIF? Custom GUCs in Postgres don't revert to NULL after a
-- committed `SET LOCAL` — they revert to the EMPTY STRING ''. So
-- `current_setting('app.tenant_id', true)` returns '' (not NULL) on a
-- pooled connection that has ever been through a tenant-scoped tx. The
-- cast `''::uuid` then raises InvalidTextRepresentationError. Wrapping
-- with NULLIF(..., '') normalises both "unset" and "empty after revert"
-- back to NULL, so the `= NULL` comparison yields NULL (i.e. not TRUE)
-- and the row is excluded from the non-bootstrap branch cleanly.

DROP POLICY IF EXISTS api_keys_tenant_isolation ON api_keys;

CREATE POLICY api_keys_tenant_isolation ON api_keys
    USING (
        current_setting('app.bootstrap', true) = 'true'
        OR tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    )
    WITH CHECK (
        tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    );

-- Note: tenants has no RLS (tenant_id IS the PK there), so the CLI can
-- INSERT INTO tenants without any SET LOCAL gymnastics. This comment is
-- just intent documentation — no DDL change required.
