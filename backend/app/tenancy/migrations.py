from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import Base
from app.db.models import TenantSchemaMigration
from app.migrations.helpers import ensure_columns, existing_columns, table_exists
from app.migrations.registry import (
    CURRENT_TENANT_SCHEMA_CHECKSUM,
    CURRENT_TENANT_SCHEMA_NAME,
    CURRENT_TENANT_SCHEMA_VERSION,
    SUPPORTED_TENANT_LEGACY_VERSIONS,
    TENANT_MIGRATION_COLUMNS,
    TENANT_SCHEMA_MIGRATIONS,
)
from app.migrations.runner import migration_summary, run_migration_plan


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_state(db: Session, company_id: int | None) -> TenantSchemaMigration | None:
    if company_id is not None:
        state = db.scalar(
            select(TenantSchemaMigration)
            .where(TenantSchemaMigration.company_id == company_id)
            .order_by(TenantSchemaMigration.applied_at.desc().nullslast(), TenantSchemaMigration.id.desc())
        )
        if state:
            return state
    return db.scalar(select(TenantSchemaMigration).order_by(TenantSchemaMigration.applied_at.desc().nullslast(), TenantSchemaMigration.id.desc()))


def ensure_tenant_migration_record(
    db: Session,
    company_id: int | None,
    *,
    notes: str | None = None,
    application_version: str | None = None,
) -> TenantSchemaMigration:
    state = _latest_state(db, company_id)
    now = _now()
    if not state:
        state = TenantSchemaMigration()
        db.add(state)
    if company_id is not None:
        state.company_id = company_id
    state.version = CURRENT_TENANT_SCHEMA_VERSION
    state.name = CURRENT_TENANT_SCHEMA_NAME
    state.checksum = CURRENT_TENANT_SCHEMA_CHECKSUM
    state.execution_ms = 0
    state.application_version = application_version
    state.status = "current"
    state.applied_at = state.applied_at or now
    state.last_checked_at = now
    state.last_error = None
    if notes:
        state.notes = notes
    state.updated_at = now
    db.commit()
    return state


def record_tenant_migration_failure(db: Session, company_id: int | None, error_message: str) -> TenantSchemaMigration:
    state = _latest_state(db, company_id)
    now = _now()
    if not state:
        state = TenantSchemaMigration()
        db.add(state)
    if company_id is not None:
        state.company_id = company_id
    state.version = state.version or "0"
    state.name = state.name or "schema failure"
    state.status = "failed"
    state.last_error = error_message
    state.last_checked_at = now
    state.updated_at = now
    db.commit()
    return state


def upgrade_tenant_schema(
    engine,
    *,
    company_id: int | None = None,
    application_version: str | None = None,
    dry_run: bool = False,
    baseline: bool = False,
) -> dict:
    if not dry_run:
        Base.metadata.create_all(bind=engine)
        ensure_columns(engine, "schema_migrations", TENANT_MIGRATION_COLUMNS, dry_run=False)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        summary = run_migration_plan(
            engine,
            db,
            TenantSchemaMigration,
            TENANT_SCHEMA_MIGRATIONS,
            application_version=application_version,
            company_id=company_id,
            allowed_legacy_versions=SUPPORTED_TENANT_LEGACY_VERSIONS,
            baseline=baseline,
            dry_run=dry_run,
        )
        if company_id is not None and not dry_run and table_exists(db.get_bind(), "schema_migrations"):
            state = db.scalar(
                select(TenantSchemaMigration)
                .where(TenantSchemaMigration.company_id == company_id)
                .order_by(TenantSchemaMigration.applied_at.desc().nullslast(), TenantSchemaMigration.id.desc())
            )
            if state:
                summary.update(
                    migration_summary(
                        state,
                        current_version=CURRENT_TENANT_SCHEMA_VERSION,
                        current_name=CURRENT_TENANT_SCHEMA_NAME,
                        current_checksum=CURRENT_TENANT_SCHEMA_CHECKSUM,
                    )
                )
        return summary
    finally:
        db.close()


def tenant_migration_report(db: Session, company_id: int | None, *, persist: bool = False) -> dict:
    if not table_exists(db.get_bind(), "schema_migrations"):
        return {
            "version": None,
            "name": None,
            "checksum": None,
            "execution_ms": None,
            "application_version": None,
            "current_version": CURRENT_TENANT_SCHEMA_VERSION,
            "current_name": CURRENT_TENANT_SCHEMA_NAME,
            "current_checksum": CURRENT_TENANT_SCHEMA_CHECKSUM,
            "status": "missing",
            "last_checked_at": None,
            "applied_at": None,
            "last_error": None,
            "notes": None,
            "is_current": False,
        }
    required_columns = {"version", "name", "checksum", "execution_ms", "application_version", "status", "applied_at", "last_checked_at", "last_error", "notes"}
    if not required_columns.issubset(existing_columns(db.get_bind(), "schema_migrations")):
        return {
            "version": None,
            "name": None,
            "checksum": None,
            "execution_ms": None,
            "application_version": None,
            "current_version": CURRENT_TENANT_SCHEMA_VERSION,
            "current_name": CURRENT_TENANT_SCHEMA_NAME,
            "current_checksum": CURRENT_TENANT_SCHEMA_CHECKSUM,
            "status": "incomplete",
            "last_checked_at": None,
            "applied_at": None,
            "last_error": None,
            "notes": None,
            "is_current": False,
        }
    state = _latest_state(db, company_id)
    now = _now()
    if not state:
        return {
            "version": None,
            "name": None,
            "checksum": None,
            "execution_ms": None,
            "application_version": None,
            "current_version": CURRENT_TENANT_SCHEMA_VERSION,
            "current_name": CURRENT_TENANT_SCHEMA_NAME,
            "current_checksum": CURRENT_TENANT_SCHEMA_CHECKSUM,
            "status": "missing",
            "last_checked_at": None,
            "applied_at": None,
            "last_error": None,
            "notes": None,
            "is_current": False,
        }
    if persist:
        state.last_checked_at = now
        state.updated_at = now
        if state.status != "failed":
            state.status = "current" if state.version == CURRENT_TENANT_SCHEMA_VERSION and state.checksum == CURRENT_TENANT_SCHEMA_CHECKSUM else "outdated"
        db.commit()
    expected = CURRENT_TENANT_SCHEMA_VERSION
    return {
        "version": state.version,
        "name": state.name,
        "checksum": state.checksum,
        "execution_ms": state.execution_ms,
        "application_version": state.application_version,
        "current_version": expected,
        "current_name": CURRENT_TENANT_SCHEMA_NAME,
        "current_checksum": CURRENT_TENANT_SCHEMA_CHECKSUM,
        "status": state.status,
        "last_checked_at": state.last_checked_at,
        "applied_at": state.applied_at,
        "last_error": state.last_error,
        "notes": state.notes,
        "is_current": state.version == expected and state.checksum == CURRENT_TENANT_SCHEMA_CHECKSUM and state.status == "current",
    }


def ensure_tenant_schema(database_url: str, *, company_id: int | None = None, application_version: str | None = None) -> dict:
    from app.tenancy.database import get_tenant_engine

    engine = get_tenant_engine(database_url)
    return upgrade_tenant_schema(engine, company_id=company_id, application_version=application_version)
