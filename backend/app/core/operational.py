from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import OrderDelivery
from app.master.models import OperationalHeartbeat

HEARTBEAT_STALE_AFTER = timedelta(minutes=5)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_heartbeat(
    db: Session,
    *,
    component: str,
    name: str,
    status: str,
    owner: str | None = None,
    success: bool = False,
    error: Exception | None = None,
    duration_ms: int | None = None,
) -> OperationalHeartbeat:
    now = utcnow()
    row = db.scalar(
        select(OperationalHeartbeat).where(
            OperationalHeartbeat.component == component,
            OperationalHeartbeat.name == name,
        )
    )
    if row is None:
        row = OperationalHeartbeat(component=component, name=name)
        db.add(row)
    row.status = status
    row.owner = owner
    row.last_seen_at = now
    row.last_duration_ms = duration_ms
    row.updated_at = now
    if success:
        row.last_success_at = now
        row.last_error_at = None
        row.last_error_type = None
        row.last_error_message = None
    elif error is not None:
        row.last_error_at = now
        row.last_error_type = error.__class__.__name__
        row.last_error_message = str(error)[:500]
    try:
        db.commit()
    except IntegrityError:
        # Two scheduler/worker instances may publish their first heartbeat at
        # the same time.  The unique key makes the insert deterministic; the
        # losing session refreshes the row and applies its observation.
        db.rollback()
        row = db.scalar(
            select(OperationalHeartbeat).where(
                OperationalHeartbeat.component == component,
                OperationalHeartbeat.name == name,
            )
        )
        if row is None:
            raise
        row.status = status
        row.owner = owner
        row.last_seen_at = now
        row.last_duration_ms = duration_ms
        row.updated_at = now
        if success:
            row.last_success_at = now
            row.last_error_at = None
            row.last_error_type = None
            row.last_error_message = None
        elif error is not None:
            row.last_error_at = now
            row.last_error_type = error.__class__.__name__
            row.last_error_message = str(error)[:500]
        db.commit()
    return row


def _age(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max((now - value.astimezone(timezone.utc)).total_seconds(), 0.0)


def _component_status(row: OperationalHeartbeat | None, *, now: datetime, required: bool) -> dict[str, object]:
    last_success = row.last_success_at if row else None
    last_seen = row.last_seen_at if row else None
    age = _age(last_success or last_seen, now)
    healthy = bool(row and age is not None and age <= HEARTBEAT_STALE_AFTER.total_seconds() and row.status in {"healthy", "running"})
    if not row:
        status = "unknown"
    elif row.status == "error":
        status = "error"
    elif age is not None and age > HEARTBEAT_STALE_AFTER.total_seconds():
        status = "stale"
    elif healthy:
        status = "healthy"
    else:
        status = "unknown"
    return {
        "status": status,
        "required": required,
        "healthy": healthy,
        "owner": row.owner if row else None,
        "last_seen": last_seen,
        "last_success": last_success,
        "last_error": row.last_error_message if row else None,
        "age_seconds": age,
    }


def required_scheduler_jobs() -> list[str]:
    configured = os.getenv("PILOT_REQUIRED_SCHEDULER_JOBS", "jobs")
    return [item.strip() for item in configured.split(",") if item.strip()]


def operational_status(
    master_db: Session,
    *,
    tenant_db: Session | None = None,
    storage: dict[str, object] | None = None,
    database_ok: bool = True,
) -> dict[str, object]:
    now = utcnow()
    rows = master_db.scalars(select(OperationalHeartbeat)).all()
    by_key = {(row.component, row.name): row for row in rows}
    worker = _component_status(by_key.get(("worker", "jobs")), now=now, required=True)
    scheduler_items = {
        name: _component_status(by_key.get(("scheduler", name)), now=now, required=True)
        for name in required_scheduler_jobs()
    }
    scheduler_healthy = bool(scheduler_items) and all(item["healthy"] for item in scheduler_items.values())
    scheduler_last = max(
        (item["last_success"] for item in scheduler_items.values() if item["last_success"] is not None),
        default=None,
    )
    failed_deliveries = None
    if tenant_db is not None:
        failed_deliveries = int(
            tenant_db.scalar(select(func.count(OrderDelivery.id)).where(OrderDelivery.status == "failed")) or 0
        )
    return {
        "web": {"status": "healthy"},
        "database": {"status": "healthy" if database_ok else "unavailable"},
        "storage": {
            "status": "healthy" if not storage or storage.get("ok") else "unavailable",
            **(storage or {}),
        },
        "worker": worker,
        "scheduler": {
            "status": "healthy" if scheduler_healthy else ("unknown" if not scheduler_items else "stale"),
            "healthy": scheduler_healthy,
            "required_jobs": scheduler_items,
            "last_required_job": scheduler_last,
        },
        "failed_deliveries": failed_deliveries,
    }
