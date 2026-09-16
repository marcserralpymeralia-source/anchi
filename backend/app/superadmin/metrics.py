from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from time import perf_counter

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import BackgroundJob, Email, InboundMessage, Order, User
from app.master.models import MasterCompany, MasterTenantDatabase, TenantHealthSnapshot, TenantUsageDaily
from app.tenancy.database import tenant_db_session
from app.tenancy.migrations import tenant_migration_report


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _count(db: Session, model, company_id: int, *where) -> int:  # noqa: ANN001
    return int(
        db.scalar(
            select(func.count(model.id)).where(model.company_id == company_id, *where)
        )
        or 0
    )


def collect_tenant_snapshot(master_db: Session, tenant: MasterTenantDatabase) -> TenantHealthSnapshot:
    """Collect one bounded sample outside the request/render path.

    Errors are represented as a sanitized status and error type.  Connection
    strings and database error messages never enter the platform dashboard.
    """

    started = perf_counter()
    checked_at = _now()
    values = {
        "company_id": tenant.company_id,
        "status": "ok",
        "checked_at": checked_at,
        "latency_ms": None,
        "schema_version": None,
        "orders_total": 0,
        "messages_total": 0,
        "email_messages_total": 0,
        "whatsapp_messages_total": 0,
        "active_users_total": 0,
        "pending_jobs_total": 0,
        "error_code": None,
        "error_message": None,
    }
    if not isinstance(tenant.database_url, str) or not tenant.database_url.strip():
        values.update(status="error", error_code="missing_database_url", error_message="Base de datos no configurada")
    else:
        db = None
        try:
            db = tenant_db_session(tenant.database_url)()
            schema_report = tenant_migration_report(db, tenant.company_id)
            values["schema_version"] = schema_report.get("version")
            if not schema_report.get("is_current"):
                values.update(
                    status="error",
                    error_code="schema_not_ready",
                    error_message="El esquema operativo no está actualizado",
                )
            else:
                values["orders_total"] = _count(db, Order, tenant.company_id)
                values["email_messages_total"] = _count(db, Email, tenant.company_id)
                values["whatsapp_messages_total"] = _count(db, InboundMessage, tenant.company_id)
                values["messages_total"] = values["email_messages_total"] + values["whatsapp_messages_total"]
                values["active_users_total"] = _count(db, User, tenant.company_id, User.is_active.is_(True))
                values["pending_jobs_total"] = _count(
                    db,
                    BackgroundJob,
                    tenant.company_id,
                    BackgroundJob.status.in_(("queued", "running", "retry")),
                )
        except Exception as exc:  # noqa: BLE001
            values.update(
                status="error",
                error_code=exc.__class__.__name__,
                error_message="No se pudo consultar la base operativa",
            )
        finally:
            if db is not None:
                db.close()

    values["latency_ms"] = max(int((perf_counter() - started) * 1000), 0)
    snapshot = TenantHealthSnapshot(
        company_id=values["company_id"],
        status=values["status"],
        checked_at=values["checked_at"],
        latency_ms=values["latency_ms"],
        schema_version=values["schema_version"],
        orders_total=values["orders_total"],
        messages_total=values["messages_total"],
        email_messages_total=values["email_messages_total"],
        whatsapp_messages_total=values["whatsapp_messages_total"],
        active_users_total=values["active_users_total"],
        pending_jobs_total=values["pending_jobs_total"],
        error_code=values["error_code"],
        error_message=values["error_message"],
    )
    master_db.add(snapshot)
    tenant.health_status = values["status"]
    tenant.last_health_check_at = checked_at

    usage_row = master_db.scalar(
        select(TenantUsageDaily).where(
            TenantUsageDaily.company_id == tenant.company_id,
            TenantUsageDaily.usage_date == checked_at.date(),
        )
    )
    if usage_row is None:
        usage_row = TenantUsageDaily(company_id=tenant.company_id, usage_date=checked_at.date())
        master_db.add(usage_row)
    usage_row.orders_total = values["orders_total"]
    usage_row.messages_total = values["messages_total"]
    usage_row.email_messages_total = values["email_messages_total"]
    usage_row.whatsapp_messages_total = values["whatsapp_messages_total"]
    usage_row.active_users_total = values["active_users_total"]
    usage_row.updated_at = checked_at
    master_db.commit()
    return snapshot


def collect_platform_snapshots(master_db: Session, *, limit: int | None = None) -> dict:
    tenants = master_db.scalars(
        select(MasterTenantDatabase)
        .join(MasterTenantDatabase.company)
        .where(MasterTenantDatabase.is_active.is_(True), MasterCompany.active.is_(True), MasterCompany.status == "active")
        .order_by(MasterTenantDatabase.company_id)
        .limit(limit)
        if limit
        else select(MasterTenantDatabase).join(MasterTenantDatabase.company).where(MasterTenantDatabase.is_active.is_(True), MasterCompany.active.is_(True), MasterCompany.status == "active").order_by(MasterTenantDatabase.company_id)
    ).all()
    result = {"checked": 0, "ok": 0, "errors": 0, "tenants": []}
    retention_cutoff = _now() - timedelta(days=365)
    master_db.execute(delete(TenantHealthSnapshot).where(TenantHealthSnapshot.checked_at < retention_cutoff))
    master_db.commit()
    for tenant in tenants:
        snapshot = collect_tenant_snapshot(master_db, tenant)
        ok = snapshot.status == "ok"
        result["checked"] += 1
        result["ok"] += int(ok)
        result["errors"] += int(not ok)
        result["tenants"].append(
            {
                "company_id": tenant.company_id,
                "status": snapshot.status,
                "latency_ms": snapshot.latency_ms,
            }
        )
    return result


def latest_health_snapshots(master_db: Session, company_ids: list[int] | None = None) -> list[TenantHealthSnapshot]:
    latest_ids = select(func.max(TenantHealthSnapshot.id).label("latest_id")).group_by(TenantHealthSnapshot.company_id)
    if company_ids:
        latest_ids = latest_ids.where(TenantHealthSnapshot.company_id.in_(company_ids))
    return master_db.scalars(
        select(TenantHealthSnapshot)
        .where(TenantHealthSnapshot.id.in_(latest_ids))
        .order_by(TenantHealthSnapshot.company_id)
    ).all()
