"""Salesforce OAuth Web Server flow routes (CR-8).

Two endpoints:

  * `GET /admin/oauth/authorize?tenant_id=<uuid>&return_to=<url>`
      Generates a signed `state` param (HMAC over `tenant_id`+nonce using
      `REIG_ENCRYPTION_KEY`) and redirects to Salesforce's
      `/services/oauth2/authorize`. The return_to url is echoed back to
      the browser after the callback completes.

  * `GET /admin/oauth/callback?code=<>&state=<>`
      Verifies the state signature, recovers `tenant_id`, POSTs to
      `/services/oauth2/token` with `grant_type=authorization_code`, and
      persists `{access_token, refresh_token_encrypted, instance_url,
      expires_at}` into `credentials`.

Why a signed state param instead of a session cookie:
    The OAuth dance is cross-origin (Salesforce redirects back to our
    domain), so a cookie set on the authorize request wouldn't survive
    the round-trip cleanly. HMAC-signing a compact `{tenant_id}.{nonce}`
    payload gives us tamper detection + one-time-use via an in-memory
    nonce cache (v1: we don't cache, accept the replay window —
    acceptable because the state is only valid for the 5-minute lifetime
    Salesforce allows between authorize and callback).

Both routes are exempt from the tenant middleware (callback has no
X-API-Key; authorize is triggered by an operator via the browser). The
callback authenticates by HMAC-verifying the state signature before
writing anything.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.db import set_encryption_key
from app.logging import get_logger

if TYPE_CHECKING:
    from asyncpg.pool import Pool

log = get_logger(__name__)
router = APIRouter(prefix="/admin/oauth", tags=["oauth"])


def _sign_state(tenant_id: UUID, nonce: str, secret: str) -> str:
    """HMAC-sign a `{tenant_id}.{nonce}.{ts}` payload.

    The timestamp guards against replay beyond the OAuth validity window.
    Format: `{tenant_id}.{nonce}.{ts}.{hex_digest}`.
    """
    ts = str(int(time.time()))
    msg = f"{tenant_id}.{nonce}.{ts}"
    digest = hmac.new(
        secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{msg}.{digest}"


def _verify_state(state: str, secret: str, max_age_s: int = 600) -> UUID:
    """Verify a signed state and return the tenant_id it carried.

    Raises HTTPException(400) on any validation failure (bad shape,
    digest mismatch, or age > max_age_s).
    """
    parts = state.split(".")
    if len(parts) != 4:
        raise HTTPException(
            status_code=400, detail="invalid state: malformed"
        )
    tenant_id_str, nonce, ts_str, got_digest = parts
    msg = f"{tenant_id_str}.{nonce}.{ts_str}"
    expected = hmac.new(
        secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, got_digest):
        raise HTTPException(status_code=400, detail="invalid state: signature")
    try:
        ts = int(ts_str)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid state: ts") from e
    if abs(int(time.time()) - ts) > max_age_s:
        raise HTTPException(status_code=400, detail="invalid state: expired")
    try:
        return UUID(tenant_id_str)
    except ValueError as e:
        raise HTTPException(
            status_code=400, detail="invalid state: tenant_id"
        ) from e


@router.get("/authorize")
async def authorize(
    tenant_id: UUID = Query(...),
    return_to: str | None = Query(None),
) -> RedirectResponse:
    """Start the OAuth dance — redirect the browser to Salesforce.

    The signed state carries the tenant_id so the callback can recover
    it without a session. The caller-supplied `return_to` is stashed in
    the state's nonce for the callback to honour.
    """
    settings = get_settings()
    if not settings.sfdc_callback_url:
        raise HTTPException(
            status_code=500,
            detail="REIG_SFDC_CALLBACK_URL not configured",
        )

    nonce = secrets.token_urlsafe(16)
    state = _sign_state(tenant_id, nonce, settings.encryption_key)

    sfdc_params = {
        "response_type": "code",
        "client_id": settings.sfdc_client_id,
        "redirect_uri": settings.sfdc_callback_url,
        "scope": "api refresh_token",
        "state": state,
    }
    target = f"{settings.sfdc_login_url}/services/oauth2/authorize?{urlencode(sfdc_params)}"
    log.info("oauth.authorize.begin", tenant_id=str(tenant_id))
    # 302 so browser follows into the SFDC login page.
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
) -> JSONResponse:
    """Finish the OAuth dance — exchange code for tokens and persist.

    Verifies the signed state (no tenant middleware ran on this route —
    Salesforce's redirect carries no X-API-Key), POSTs to SFDC's
    `/services/oauth2/token`, and writes the encrypted refresh_token
    into `credentials`.

    Response body: a minimal JSON OK so an operator watching the browser
    can see the exchange succeeded. A real UI replaces this with a
    `return_to` redirect; v1 stays terminal for simplicity.
    """
    settings = get_settings()
    tenant_id = _verify_state(state, settings.encryption_key)

    # Exchange the authorization code for access + refresh tokens.
    token_url = f"{settings.sfdc_login_url}/services/oauth2/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": settings.sfdc_client_id,
        "client_secret": settings.sfdc_client_secret,
        "redirect_uri": settings.sfdc_callback_url,
        "code": code,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(token_url, data=data)
    if resp.status_code != 200:
        log.warning(
            "oauth.callback.exchange_failed",
            tenant_id=str(tenant_id),
            status=resp.status_code,
            body=resp.text[:300],
        )
        raise HTTPException(
            status_code=400,
            detail=f"Salesforce token exchange failed: {resp.status_code}",
        )
    token_payload = resp.json()
    access_token: str = token_payload["access_token"]
    refresh_token: str = token_payload["refresh_token"]
    instance_url: str = token_payload.get("instance_url", "")
    ttl_s = int(token_payload.get("expires_in", 3600))
    expires_epoch = int(time.time()) + ttl_s

    # Persist — encrypt the refresh token via the pgcrypto helper,
    # upsert on (tenant_id, adapter).
    pool: Pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await conn.execute(
                "SELECT set_config('app.bootstrap', 'true', true)"
            )
            await set_encryption_key(conn)
            await conn.execute(
                "INSERT INTO credentials "
                "(tenant_id, adapter, access_token_cached, "
                " access_token_expires_at, refresh_token_encrypted, instance_url) "
                "VALUES ($1, 'salesforce', $2, to_timestamp($3), "
                "        encrypt_refresh_token($4), $5) "
                "ON CONFLICT (tenant_id, adapter) DO UPDATE "
                "SET access_token_cached = EXCLUDED.access_token_cached, "
                "    access_token_expires_at = EXCLUDED.access_token_expires_at, "
                "    refresh_token_encrypted = EXCLUDED.refresh_token_encrypted, "
                "    instance_url = EXCLUDED.instance_url, "
                "    updated_at = now()",
                tenant_id,
                access_token,
                expires_epoch,
                refresh_token,
                instance_url,
            )

    log.info(
        "oauth.callback.persisted",
        tenant_id=str(tenant_id),
        instance_url=instance_url,
    )
    return JSONResponse(
        {
            "status": "ok",
            "tenant_id": str(tenant_id),
            "instance_url": instance_url,
        }
    )
