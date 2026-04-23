"""Adapter registry + Protocol re-exports (CR-11).

Importing `adapters` is enough to populate `REGISTRY` with every concrete
adapter the process knows about:

    from adapters import REGISTRY, CRMAdapter, register
    # REGISTRY == {"salesforce": SalesforceAdapter, "servicenow_stub": ServiceNowAdapter}

The population happens as a side effect of the `@register("<name>")`
decorators in `adapters/salesforce.py` and `adapters/servicenow.py`,
which this module imports below. The order matters: the decorators
only run after the adapter modules are imported, so any caller that
imports `adapters` (or any submodule via `from adapters import ...`)
gets a fully-populated registry.

The `register` decorator is typed with a TypeVar bound to `CRMAdapter`
so `mypy --strict` can verify at decoration time that the decorated
class actually satisfies the Protocol — a class that's missing
`upsert_record` cannot be decorated, fails the type-check, and never
reaches the registry.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, TypeVar
from uuid import UUID

from adapters.base import (
    CallActivityPayload,
    ContactPayload,
    CRMAdapter,
    LeadUpsertPayload,
    LookupResult,
    UpsertResult,
)

if TYPE_CHECKING:
    import asyncpg
    import httpx

    from app.config import Settings

__all__ = [
    "CRMAdapter",
    "AdapterFactory",
    "CallActivityPayload",
    "ContactPayload",
    "LeadUpsertPayload",
    "LookupResult",
    "REGISTRY",
    "UpsertResult",
    "register",
]


class AdapterFactory(Protocol):
    """Constructor shape every registered adapter class must expose.

    `type[CRMAdapter]` by itself doesn't describe `__init__`, so when
    the resolver does `cls(tenant_id=..., db_pool=..., settings=...,
    http_client=...)` mypy has no way to verify the call. This Protocol
    pins the four-arg constructor shape explicitly — every concrete
    adapter's `__init__` matches this signature, and `REGISTRY` is typed
    as `dict[str, AdapterFactory]` so the resolver's call is fully
    type-checked.
    """

    def __call__(
        self,
        tenant_id: UUID,
        db_pool: asyncpg.Pool | None,
        settings: Settings,
        http_client: httpx.AsyncClient | None = ...,
    ) -> CRMAdapter: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# Populated by `@register("<name>")` at import time. Value type is
# `AdapterFactory` (a callable/class that takes the standard 4 args and
# returns a CRMAdapter instance) — NOT bare `type[CRMAdapter]`, because
# the Protocol itself has no __init__ and that would lose the constructor
# shape.
REGISTRY: dict[str, AdapterFactory] = {}


# TypeVar bound to CRMAdapter so mypy verifies the decorated class satisfies
# the Protocol at decoration time. The bound also preserves the exact subtype
# through the decorator — callers get back `SalesforceAdapter`, not the
# widened `type[CRMAdapter]`.
_T = TypeVar("_T", bound=CRMAdapter)


def register(name: str) -> Callable[[type[_T]], type[_T]]:
    """Class decorator — add `cls` to `REGISTRY` under `name`.

    Usage:
        @register("salesforce")
        class SalesforceAdapter:
            ...

    The decorator is a closure over `name` so you can stack it on any
    class that structurally satisfies `CRMAdapter`. Duplicate names
    overwrite silently — the resolver contract is last-registration-wins,
    which matters for test harnesses that swap in a mocked adapter.

    The `AdapterFactory` cast on the REGISTRY write is safe because the
    TypeVar bound guarantees `cls` is a CRMAdapter subclass AND the
    concrete classes expose the 4-arg __init__ that AdapterFactory's
    Protocol describes — verified at `mypy --strict` time.
    """

    def _wrap(cls: type[_T]) -> type[_T]:
        # cls is type[CRMAdapter]; mypy can't see the __init__ shape on
        # the Protocol itself, but the concrete class DOES satisfy the
        # AdapterFactory protocol. This cast is the one place we assert
        # that property explicitly.
        REGISTRY[name] = cls  # type: ignore[assignment]
        return cls

    return _wrap


# ---------------------------------------------------------------------------
# Import concrete adapters for their decorator side effects.
# Keep these imports AT THE BOTTOM of the module — they depend on `register`
# being defined above, and some tooling (ruff I001) may otherwise try to
# re-sort them up to the top, which would break the closure.
# ---------------------------------------------------------------------------
from adapters import salesforce as _salesforce  # noqa: E402, F401
from adapters import servicenow as _servicenow  # noqa: E402, F401
