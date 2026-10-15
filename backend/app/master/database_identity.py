from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.master.models import MasterTenantDatabase


def tenant_database_identity(database_url: str) -> tuple[str, str, int | None, str]:
    """Return a credential-free identity for a tenant database destination."""

    url = make_url(database_url)
    backend = url.get_backend_name().lower()
    database = url.database or ""
    if backend == "sqlite":
        if database and database != ":memory:":
            database = str(Path(database).expanduser().resolve())
        return backend, "", None, database

    host = (url.host or "").strip().lower().rstrip(".")
    # Neon exposes direct and pooled hostnames for the same endpoint.
    host = host.replace("-pooler.", ".", 1)
    default_ports = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "mssql": 1433}
    port = url.port or default_ports.get(backend)
    return backend, host, port, database


def ensure_tenant_database_is_unassigned(
    master_db: Session,
    database_url: str,
    *,
    exclude_company_id: int | None = None,
) -> None:
    """Prevent multiple companies from sharing one operational database."""

    requested_identity = tenant_database_identity(database_url)
    statement = select(MasterTenantDatabase)
    if exclude_company_id is not None:
        statement = statement.where(MasterTenantDatabase.company_id != exclude_company_id)

    for tenant_database in master_db.scalars(statement):
        assigned_url = tenant_database.database_url
        if assigned_url and tenant_database_identity(assigned_url) == requested_identity:
            raise ValueError(
                "Esta base de datos ya está asignada a otra empresa. Configura una base propia para cada empresa."
            )
