"""Salesforce CRM adapter — C7 fills in OAuth + Lead upsert + retries.

C5 shipped the skeleton (structural Protocol conformance + a live
`map_fields`). C7 now wires the real write path:

  * `authenticate()` refreshes the cached access_token when missing or
    within 60 s of expiry. Reads a pgcrypto-encrypted refresh_token
    from `credentials` via the `decrypt_refresh_token()` SQL helper
    (CR-7) — the key never leaves the app's environment.

  * `upsert_record()` PATCHes
    `/services/data/{api_version}/sobjects/Lead/External_Call_Id__c/{id}`.
    Response codes map to:
      201 → UpsertResult(status="created")
      204 → UpsertResult(status="updated")
      401 INVALID_SESSION_ID → refresh once + retry once (not counted
           against the tenacity budget — this is an in-method recovery).
      4xx other → PermanentError (NOT retried).
      429 / 5xx → TransientError (retried by tenacity).

  * Retry policy (CR-9): N attempts (`settings.retry_max_attempts`), exp
    backoff (base * 2^n s, capped at `retry_backoff_max_seconds`) + random
    jitter, only on `TransientError`. On exhaustion `tenacity.RetryError`
    bubbles out — the pipeline layer catches it and writes a
    `crm_writes.status='failed'` row with the wrapped exception so
    forensics has every attempt's error context.

The live pipeline in `app/call_pipeline.py` calls
`authenticate()` + `upsert_record(payload)` inside a tx that has
`app.tenant_id` + `app.encryption_key` pinned — without both, the
`decrypt_refresh_token()` helper raises.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from adapters import register
from adapters.base import LeadUpsertPayload, UpsertResult
from adapters.errors import PermanentError, TransientError
from app.logging import get_logger

if TYPE_CHECKING:
    import asyncpg

    from app.config import Settings

log = get_logger(__name__)


@register("salesforce")
class SalesforceAdapter:
    """Salesforce REST adapter (CR-8 + CR-9).

    Construction stores the passed-in deps. `authenticate()` and
    `upsert_record()` assert the DB pool / HTTP client are present so a
    forgotten wiring produces a clear error.
    """

    def __init__(
        self,
        tenant_id: UUID,
        db_pool: asyncpg.Pool | None,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Stash construction args for use by authenticate / upsert_record.

        Args:
            tenant_id:   Owner of every downstream write. Also the RLS key
                         for the `credentials` table.
            db_pool:     asyncpg pool. C7's `authenticate` pins
                         `app.tenant_id` + `app.encryption_key` on each
                         acquired conn. None is tolerated so tests that
                         only exercise `map_fields` don't need a pool.
            settings:    App settings — reads sfdc_* and retry_* values.
            http_client: Optional injected client (for respx mocking).
                         None means "build a default client on first use".
        """
        self.tenant_id = tenant_id
        self.db_pool = db_pool
        self.settings = settings
        self.http_client = http_client

        # Populated by authenticate() so upsert_record can reuse the token
        # without a second DB round-trip.
        self._access_token: str | None = None
        self._instance_url: str | None = None

    async def _http(self) -> httpx.AsyncClient:
        """Return the adapter's httpx client, lazily building a default."""
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=30.0)
        return self.http_client

    async def _load_creds(
        self, conn: asyncpg.Connection
    ) -> tuple[str | None, int | None, str | None, str | None]:
        """Fetch cached access + decrypted refresh + instance_url for this tenant.

        Returns `(access_token, expires_epoch_s, refresh_token, instance_url)`.
        Any field may be None if OAuth hasn't completed. Caller MUST have
        `app.encryption_key` pinned on `conn`.
        """
        row = await conn.fetchrow(
            "SELECT access_token_cached, "
            "       EXTRACT(EPOCH FROM access_token_expires_at)::bigint AS expires_epoch, "
            "       decrypt_refresh_token(refresh_token_encrypted) AS refresh_token, "
            "       instance_url "
            "FROM credentials "
            "WHERE tenant_id = $1 AND adapter = 'salesforce'",
            self.tenant_id,
        )
        if row is None:
            return None, None, None, None
        return (
            row["access_token_cached"],
            row["expires_epoch"],
            row["refresh_token"],
            row["instance_url"],
        )

    async def _update_access_token(
        self,
        conn: asyncpg.Connection,
        access_token: str,
        expires_epoch: int,
    ) -> None:
        """Persist a freshly-refreshed access_token back to `credentials`."""
        await conn.execute(
            "UPDATE credentials "
            "SET access_token_cached = $2, "
            "    access_token_expires_at = to_timestamp($3), "
            "    updated_at = now() "
            "WHERE tenant_id = $1 AND adapter = 'salesforce'",
            self.tenant_id,
            access_token,
            expires_epoch,
        )

    async def _exchange_refresh_token(self, refresh_token: str) -> tuple[str, int]:
        """POST /services/oauth2/token with grant_type=refresh_token.

        Returns `(access_token, expires_epoch_s)`. Salesforce's refresh-
        token grant doesn't carry expires_in, so we pick a conservative
        1-hour TTL and let the next cache-expiry check trigger a refresh.
        """
        client = await self._http()
        token_url = f"{self.settings.sfdc_login_url}/services/oauth2/token"
        data = {
            "grant_type": "refresh_token",
            "client_id": self.settings.sfdc_client_id,
            "client_secret": self.settings.sfdc_client_secret,
            "refresh_token": refresh_token,
        }
        resp = await client.post(token_url, data=data)
        if resp.status_code != 200:
            raise PermanentError(
                f"Salesforce token refresh failed: {resp.status_code} "
                f"{resp.text[:300]}"
            )
        payload = resp.json()
        access_token: str = payload["access_token"]
        ttl_s = int(payload.get("expires_in", 3600))
        expires_epoch = int(time.time()) + ttl_s
        return access_token, expires_epoch

    async def authenticate(self) -> None:
        """Ensure `self._access_token` is fresh (CR-8).

        Load creds; if the cached token is still good (>60 s to expiry),
        reuse it. Otherwise exchange the decrypted refresh_token for a
        new access_token and persist it.
        """
        assert self.db_pool is not None, "SalesforceAdapter.authenticate needs db_pool"
        from app.db import set_encryption_key

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(self.tenant_id),
                )
                await set_encryption_key(conn)

                access, expires_epoch, refresh, instance_url = (
                    await self._load_creds(conn)
                )
                if refresh is None or instance_url is None:
                    raise PermanentError(
                        f"No Salesforce credentials for tenant {self.tenant_id}; "
                        "complete OAuth via /admin/oauth/authorize first."
                    )

                now_epoch = int(time.time())
                if (
                    access is not None
                    and expires_epoch is not None
                    and expires_epoch - now_epoch > 60
                ):
                    self._access_token = access
                    self._instance_url = instance_url
                    log.debug(
                        "sfdc.authenticate.cached",
                        tenant_id=str(self.tenant_id),
                        expires_in=expires_epoch - now_epoch,
                    )
                    return

                new_access, new_expires = await self._exchange_refresh_token(refresh)
                await self._update_access_token(conn, new_access, new_expires)
                self._access_token = new_access
                self._instance_url = instance_url
                log.info(
                    "sfdc.authenticate.refreshed",
                    tenant_id=str(self.tenant_id),
                    expires_epoch=new_expires,
                )

    def _classify_response(
        self, resp: httpx.Response
    ) -> tuple[str | None, str | None]:
        """Map SFDC response → (kind | None, body_detail).

        Returns:
          (None, None)                    on 201 / 204 (happy path).
          ("invalid_session", detail)     on 401 + INVALID_SESSION_ID in body.
          ("transient", detail)           on 429 or 5xx.
          ("permanent", detail)           on 4xx other than the above.
        """
        sc = resp.status_code
        if sc in (201, 204):
            return None, None
        body_text = resp.text[:500]
        if sc == 401 and "INVALID_SESSION_ID" in body_text:
            return "invalid_session", body_text
        if sc == 429 or 500 <= sc < 600:
            return "transient", body_text
        return "permanent", body_text

    def _build_sfdc_body(self, payload: LeadUpsertPayload) -> dict[str, Any]:
        """Translate LeadUpsertPayload → SFDC Lead JSON body.

        Salesforce Lead fields are CamelCase; we carry snake_case
        internally. LastName is required by SFDC — fall back to phone
        number if we haven't inferred a real last name yet.
        """
        body: dict[str, Any] = {}
        if payload.first_name is not None:
            body["FirstName"] = payload.first_name
        if payload.last_name is not None:
            body["LastName"] = payload.last_name
        if payload.phone is not None:
            body["Phone"] = payload.phone
        if payload.email is not None:
            body["Email"] = payload.email
        body["Company"] = payload.company
        body["LeadSource"] = payload.lead_source
        if payload.description is not None:
            body["Description"] = payload.description
        # SFDC Lead requires LastName.
        if "LastName" not in body:
            body["LastName"] = payload.phone or "Unknown Caller"
        return body

    async def _do_upsert(
        self, payload: LeadUpsertPayload, access_token: str, instance_url: str
    ) -> httpx.Response:
        """One PATCH attempt — no retry, no refresh."""
        client = await self._http()
        url = (
            f"{instance_url}/services/data/{self.settings.sfdc_api_version}"
            f"/sobjects/Lead/External_Call_Id__c/{payload.external_call_id}"
        )
        sfdc_body = self._build_sfdc_body(payload)
        resp = await client.patch(
            url,
            json=sfdc_body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp

    def _parse_success(
        self, resp: httpx.Response, external_call_id: str
    ) -> UpsertResult:
        """Extract UpsertResult from a 201 or 204 response."""
        if resp.status_code == 201:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            record_id = (
                body.get("id") or body.get("Id") or external_call_id
            )
            return UpsertResult(record_id=record_id, status="created")
        # 204 — empty body; use external_call_id as the handle.
        return UpsertResult(record_id=external_call_id, status="updated")

    async def _upsert_once(self, payload: LeadUpsertPayload) -> UpsertResult:
        """Execute one upsert, handling the 401 INVALID_SESSION_ID branch.

        This is the retry-unit that tenacity wraps. A 401 INVALID_SESSION_ID
        refreshes the token once and retries once — that's NOT counted
        against the tenacity budget because it's a legitimate OAuth
        lifecycle event, not a failing downstream.

        Raises:
            TransientError: 429 / 5xx — tenacity retries.
            PermanentError: 4xx other than 401 INVALID_SESSION_ID.
        """
        if self._access_token is None or self._instance_url is None:
            await self.authenticate()
        assert self._access_token is not None and self._instance_url is not None

        resp = await self._do_upsert(
            payload, self._access_token, self._instance_url
        )
        kind, detail = self._classify_response(resp)

        if kind is None:
            return self._parse_success(resp, payload.external_call_id)

        if kind == "invalid_session":
            log.info(
                "sfdc.upsert.invalid_session.refreshing",
                tenant_id=str(self.tenant_id),
            )
            self._access_token = None
            await self.authenticate()
            assert self._access_token is not None and self._instance_url is not None
            resp2 = await self._do_upsert(
                payload, self._access_token, self._instance_url
            )
            kind2, detail2 = self._classify_response(resp2)
            if kind2 is None:
                return self._parse_success(resp2, payload.external_call_id)
            if kind2 == "transient":
                raise TransientError(f"Salesforce transient after refresh: {detail2}")
            raise PermanentError(f"Salesforce permanent after refresh: {detail2}")

        if kind == "transient":
            raise TransientError(f"Salesforce {resp.status_code}: {detail}")

        raise PermanentError(f"Salesforce {resp.status_code}: {detail}")

    async def upsert_record(self, payload: LeadUpsertPayload) -> UpsertResult:
        """Idempotent PATCH upsert of a Lead row keyed on External_Call_Id__c.

        Wraps `_upsert_once` in a tenacity retry decorator that only
        retries `TransientError`. `PermanentError` and non-adapter
        exceptions bubble out unchanged.

        Raises:
            tenacity.RetryError: transient budget exhausted; the pipeline
                                 layer catches this and writes
                                 `crm_writes.status='failed'`.
            PermanentError:      non-retryable SFDC error (4xx other than
                                 401 INVALID_SESSION_ID).
        """
        max_attempts = self.settings.retry_max_attempts
        base = self.settings.retry_backoff_base_seconds
        cap = self.settings.retry_backoff_max_seconds

        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=base, max=cap) + wait_random(0, 2),
            retry=retry_if_exception_type(TransientError),
            reraise=False,
        )
        async def _attempt() -> UpsertResult:
            return await self._upsert_once(payload)

        return await _attempt()

    async def describe_schema(self) -> dict[str, Any]:
        """Return the target object + external-id field + API version."""
        return {
            "object": "Lead",
            "external_id_field": "External_Call_Id__c",
            "api_version": self.settings.sfdc_api_version,
        }

    async def map_fields(self, call_payload: dict[str, Any]) -> LeadUpsertPayload:
        """Translate a raw Retell `call_analyzed` dict into LeadUpsertPayload.

        C6's pipeline wraps this with Presidio redaction before the adapter
        sees the transcript. `map_fields` itself stays PII-unaware so the
        adapter file doesn't need to care about Presidio's API.
        """
        call = call_payload.get("call", {})
        external_call_id = call["call_id"]

        from_number = call.get("from_number")
        transcript = call.get("transcript")

        return LeadUpsertPayload(
            external_call_id=external_call_id,
            first_name=None,
            last_name=None,
            phone=from_number,
            email=None,
            company="Unknown (inbound call)",
            lead_source="Retell Voice Agent",
            description=transcript,
        )
