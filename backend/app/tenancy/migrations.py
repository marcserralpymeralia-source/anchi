from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TenantSchemaMigration

CURRENT_TENANT_SCHEMA_VERSION = "2026.07.10.1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_tenant_migration_record(db: Session, company_id: int, *, notes: str | None = None) -> TenantSchemaMigration:
    migration = db.scalar(select(TenantSchemaMigration).where(TenantSchemaMigration.company_id == company_id))
    now = _now()
    if not migration:
        migration = TenantSchemaMigration(
            company_id=company_id,
            version=CURRENT_TENANT_SCHEMA_VERSION,
            status="current",
            applied_at=now,
            last_checked_at=now,
            notes=notes,
        )
        db.add(migration)
    else:
        migration.version = CURRENT_TENANT_SCHEMA_VERSION
        migration.status = "current"
        migration.applied_at = migration.applied_at or now
        migration.last_checked_at = now
        if notes:
            migration.notes = notes
        migration.last_error = None
    migration.updated_at = now
    db.commit()
    return migration


def record_tenant_migration_failure(db: Session, company_id: int, error_message: str) -> TenantSchemaMigration:
    migration = db.scalar(select(TenantSchemaMigration).where(TenantSchemaMigration.company_id == company_id))
    now = _now()
    if not migration:
        migration = TenantSchemaMigration(
            company_id=company_id,
            version="0",
            status="failed",
            last_error=error_message,
            last_checked_at=now,
            updated_at=now,
        )
        db.add(migration)
    else:
        migration.status = "failed"
        migration.last_error = error_message
        migration.last_checked_at = now
        migration.updated_at = now
    db.commit()
    return migration


def tenant_migration_report(db: Session, company_id: int, *, persist: bool = False) -> dict:
    migration = db.scalar(select(TenantSchemaMigration).where(TenantSchemaMigration.company_id == company_id))
    now = _now()
    if not migration:
        return {
            "version": None,
            "current_version": CURRENT_TENANT_SCHEMA_VERSION,
            "status": "missing",
            "last_checked_at": None,
            "applied_at": None,
            "last_error": None,
            "is_current": False,
        }
    if persist:
        migration.last_checked_at = now
        migration.updated_at = now
        if migration.status != "failed":
            migration.status = "current" if migration.version == CURRENT_TENANT_SCHEMA_VERSION else "outdated"
        db.commit()
    expected = CURRENT_TENANT_SCHEMA_VERSION
    return {
        "version": migration.version,
        "current_version": expected,
        "status": migration.status,
        "last_checked_at": migration.last_checked_at,
        "applied_at": migration.applied_at,
        "last_error": migration.last_error,
        "checksum": migration.checksum,
        "notes": migration.notes,
        "is_current": migration.version == expected and migration.status == "current",
    }
