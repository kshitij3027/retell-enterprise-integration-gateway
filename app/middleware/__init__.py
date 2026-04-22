"""HTTP middleware for REIG.

Currently exports:
  * TenantResolutionMiddleware — maps X-API-Key → tenant_id on request.state,
    enforces path-param tenant binding, and exempts public paths (health,
    webhooks, OAuth callback).
"""
from __future__ import annotations

from app.middleware.tenant import TenantResolutionMiddleware

__all__ = ["TenantResolutionMiddleware"]
