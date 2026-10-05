"""Read-only schema gates shared by Web and standalone Worker startup."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.master.migrations import master_migration_report
from app.master.models import MasterCompany, MasterTenantDatabase
from app.tenancy.migrations import tenant_migration_report


class RuntimeSchemaError(RuntimeError):
    """Raised when a runtime database is unreachable or its schema is stale."""


class TenantSchemaNotCurrent(RuntimeSchemaError):
    """Raised when data-plane tenant provisioning must wait for operator migration."""


def assert_existing_database(database_url: str, *, scope: str) -> None:
    """Prevent SQLite connection attempts from creating a missing database file."""

    try:
        url = make_url(database_url)
    except Exception:
        raise RuntimeSchemaError(f"{scope} database URL is invalid") from None
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    if not Path(url.database).expanduser().exists():
        raise RuntimeSchemaError(f"{scope} database file is missing")


def _not_current_state(report: dict) -> str:
    status = str(report.get("status") or "unknown")
    return status if status in {"missing", "incomplete", "failed", "outdated"} else "outdated"


def validate_runtime_schema(
    *,
    master_session_factory=None,
    tenant_session_factory=None,
) -> dict[str, object]:
    """Validate the master and every active tenant ledger without writing."""

    if master_session_factory is None:
        from app.master.database import MasterSessionLocal

        master_session_factory = MasterSessionLocal
    if tenant_session_factory is None:
        from app.tenancy.database import tenant_db_session

        tenant_session_factory = tenant_db_session
    master_db = master_session_factory()
    try:
        try:
            master_bind = master_db.get_bind()
            assert_existing_database(str(master_bind.url), scope="master")
            master_report = master_migration_report(master_db, persist=False)
        except RuntimeSchemaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeSchemaError(f"master schema check unavailable ({exc.__class__.__name__})") from None
        if not master_report.get("is_current"):
            raise RuntimeSchemaError(f"master schema is not current ({_not_current_state(master_report)})")

        try:
            tenants = master_db.scalars(
                select(MasterTenantDatabase)
                .join(MasterCompany, MasterCompany.id == MasterTenantDatabase.company_id)
                .where(
                    MasterTenantDatabase.is_active.is_(True),
                    MasterCompany.active.is_(True),
                    MasterCompany.status == "active",
                )
            ).all()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeSchemaError(f"tenant registry check unavailable ({exc.__class__.__name__})") from None

        tenant_status: dict[int, str] = {}
        for tenant in tenants:
            if not isinstance(tenant.database_url, str) or not tenant.database_url.strip():
                raise RuntimeSchemaError(f"tenant schema unavailable (company_id={tenant.company_id}, database_url_missing)")
            tenant_db = None
            try:
                assert_existing_database(tenant.database_url, scope=f"tenant company_id={tenant.company_id}")
                tenant_db = tenant_session_factory(tenant.database_url)()
                report = tenant_migration_report(tenant_db, tenant.company_id, persist=False)
            except RuntimeSchemaError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RuntimeSchemaError(
                    f"tenant schema check unavailable (company_id={tenant.company_id}, {exc.__class__.__name__})"
                ) from None
            finally:
                if tenant_db is not None:
                    tenant_db.close()
            if not report.get("is_current"):
                raise RuntimeSchemaError(
                    f"tenant schema is not current (company_id={tenant.company_id}, {_not_current_state(report)})"
                )
            tenant_status[int(tenant.company_id)] = "current"
        return {"master": "current", "tenants": tenant_status}
    finally:
        master_db.close()
