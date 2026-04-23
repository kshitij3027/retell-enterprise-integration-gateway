-- migrations/0003_pgcrypto_helpers.sql
-- C7: Helper functions for pgcrypto-backed refresh-token encryption (CR-7).
--
-- The key is NOT stored in the DB — it lives in the app's environment as
-- REIG_ENCRYPTION_KEY and is flowed into each tenant-scoped transaction
-- via `SELECT set_config('app.encryption_key', $1, true)`. That keeps the
-- key out of backups, `pg_dump` output, and the credentials table itself;
-- a DB-only compromise cannot decrypt refresh tokens without also getting
-- the running process's env.
--
-- Why two thin wrappers instead of inlining `pgp_sym_encrypt/decrypt`:
--   * Central place to enforce that app.encryption_key is set (the
--     `current_setting(..., false)` variant throws if missing — callers
--     can't accidentally no-op encryption by forgetting the SET LOCAL).
--   * Single point to rotate to a different cipher later without
--     touching every adapter that writes a refresh token.
--
-- Both functions are SECURITY INVOKER (the default) so they run with the
-- caller's role permissions and respect RLS on credentials.

CREATE OR REPLACE FUNCTION encrypt_refresh_token(plain_text text)
RETURNS bytea
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT pgp_sym_encrypt(plain_text, current_setting('app.encryption_key', false));
$$;

CREATE OR REPLACE FUNCTION decrypt_refresh_token(ciphertext bytea)
RETURNS text
LANGUAGE SQL
IMMUTABLE
AS $$
    SELECT pgp_sym_decrypt(ciphertext, current_setting('app.encryption_key', false));
$$;

GRANT EXECUTE ON FUNCTION encrypt_refresh_token(text) TO reig_app;
GRANT EXECUTE ON FUNCTION decrypt_refresh_token(bytea) TO reig_app;

-- Track every CRM write's outcome. Index helps the "last attempts for
-- this call_id" query that the demo dashboard will make in C10.
CREATE INDEX IF NOT EXISTS crm_writes_status_idx ON crm_writes(status);
