"""Bootstrap CLI for tenants and API keys.

Chicken-and-egg: you can't call `POST /admin/tenants/{id}/keys` without
first having a tenant and an API key. This CLI closes the loop by talking
directly to Postgres from inside the api container:

    docker compose run --rm api python -m scripts.cli create-tenant \\
        --name "Acme" --profile "consumer-lending"

    docker compose run --rm api python -m scripts.cli issue-key \\
        --tenant-id <uuid-from-above>

    docker compose run --rm api python -m scripts.cli list-tenants

`tenants` has no RLS (tenant_id IS the PK), so create/list run with no
SET LOCAL gymnastics. `api_keys` DOES have RLS, so `issue-key` opens a
transaction and sets both `app.tenant_id` AND `app.bootstrap='true'` so
the policy admits the INSERT's WITH CHECK.

Connection uses `settings.database_url` — which by default routes through
the non-superuser `reig_app` role inside docker-compose. That means
tenant/api_key writes go through the same RLS path as the live API, not
as a superuser bypass. CR-5 proven at the CLI level.
"""
from __future__ import annotations

import asyncio
from uuid import UUID

import asyncpg
import typer

from app.auth import generate_key
from app.config import get_settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="REIG bootstrap CLI — tenants and API keys.",
)


async def _connect() -> asyncpg.Connection:
    """Open a single asyncpg connection using the app's database URL."""
    settings = get_settings()
    return await asyncpg.connect(dsn=settings.database_url)


# ---------------------------------------------------------------------------
# create-tenant
# ---------------------------------------------------------------------------
async def _create_tenant(name: str, profile: str, active_adapter: str) -> UUID:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "INSERT INTO tenants (name, profile, active_adapter) "
            "VALUES ($1, $2, $3) RETURNING id",
            name,
            profile,
            active_adapter,
        )
        assert row is not None
        # asyncpg.Record is untyped upstream; the column is a uuid.
        new_id: UUID = row["id"]
        return new_id
    finally:
        await conn.close()


@app.command("create-tenant")
def create_tenant(
    name: str = typer.Option(..., "--name", help="Display name for the tenant."),
    profile: str = typer.Option(..., "--profile", help="Profile key, e.g. consumer-lending."),
    active_adapter: str = typer.Option(
        "salesforce",
        "--active-adapter",
        help="Default downstream CRM adapter.",
    ),
) -> None:
    """Create a new tenant and print its UUID."""
    tenant_id = asyncio.run(_create_tenant(name, profile, active_adapter))
    # Plain stdout — shell pipelines expect the UUID bare on the last line.
    typer.echo(f"tenant_id: {tenant_id}")


# ---------------------------------------------------------------------------
# issue-key
# ---------------------------------------------------------------------------
async def _issue_key(tenant_id: UUID) -> str:
    settings = get_settings()
    raw, stored_hash = generate_key(prefix=settings.tenant_api_key_prefix)

    conn = await _connect()
    try:
        async with conn.transaction():
            # Pin RLS to the target tenant AND opt into bootstrap for the
            # SELECT-side of the policy. Bootstrap alone would fail the
            # WITH CHECK; tenant_id alone would fail if the tenant was
            # touched via SELECT before INSERT.
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                str(tenant_id),
            )
            await conn.execute("SELECT set_config('app.bootstrap', 'true', true)")

            # Verify the tenant actually exists — better error than an FK
            # violation message.
            exists = await conn.fetchval(
                "SELECT 1 FROM tenants WHERE id = $1",
                tenant_id,
            )
            if exists is None:
                raise typer.BadParameter(f"tenant {tenant_id} does not exist")

            await conn.execute(
                "INSERT INTO api_keys (tenant_id, key_hash, key_prefix) "
                "VALUES ($1, $2, $3)",
                tenant_id,
                stored_hash,
                settings.tenant_api_key_prefix,
            )
    finally:
        await conn.close()

    return raw


@app.command("issue-key")
def issue_key(
    tenant_id: UUID = typer.Option(..., "--tenant-id", help="UUID of the target tenant."),
) -> None:
    """Issue a new API key for a tenant. Prints the raw key ONCE — store it."""
    raw = asyncio.run(_issue_key(tenant_id))
    typer.echo("=== STORE THIS KEY NOW — IT WILL NOT BE SHOWN AGAIN ===")
    typer.echo(raw)


# ---------------------------------------------------------------------------
# list-tenants
# ---------------------------------------------------------------------------
async def _list_tenants() -> list[asyncpg.Record]:
    conn = await _connect()
    try:
        rows: list[asyncpg.Record] = await conn.fetch(
            "SELECT id, name, profile, phi_mode FROM tenants ORDER BY created_at"
        )
        return rows
    finally:
        await conn.close()


@app.command("list-tenants")
def list_tenants() -> None:
    """Print all tenants as `id | name | profile | phi_mode`."""
    rows = asyncio.run(_list_tenants())
    if not rows:
        typer.echo("(no tenants)")
        return
    for r in rows:
        typer.echo(f"{r['id']} | {r['name']} | {r['profile']} | phi_mode={r['phi_mode']}")


if __name__ == "__main__":
    app()
