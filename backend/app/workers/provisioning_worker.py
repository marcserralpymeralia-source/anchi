"""Background provisioning for companies created from the platform console."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.master.database import MasterSessionLocal
from app.master.models import (
    CompanyMembership,
    MasterCompany,
    MasterTenantDatabase,
    MasterUser,
    TenantProvisioningRun,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claim_run(master_db: Session) -> int | None:
    """Claim one pending run before touching the external tenant database."""

    run = master_db.scalar(
        select(TenantProvisioningRun)
        .where(TenantProvisioningRun.status.in_(("pending", "retry")))
        .order_by(TenantProvisioningRun.created_at, TenantProvisioningRun.id)
        .with_for_update()
    )
    if run is None:
        return None
    active_run = master_db.scalar(
        select(TenantProvisioningRun.id).where(
            TenantProvisioningRun.company_id == run.company_id,
            TenantProvisioningRun.status == "running",
            TenantProvisioningRun.id != run.id,
        )
    )
    if active_run is not None:
        return None
    run.status = "running"
    run.current_step = "schema"
    run.started_at = _now()
    run.error_message = None
    company = master_db.get(MasterCompany, run.company_id)
    if company is not None:
        company.active = False
        company.status = "provisioning"
        company.provisioning_status = "provisioning"
    master_db.commit()
    return run.id


def _set_step(master_db: Session, run_id: int, step: str) -> None:
    run = master_db.get(TenantProvisioningRun, run_id)
    if run is not None:
        run.current_step = step
        master_db.commit()


def _provision_run(run_id: int) -> bool:
    """Provision one run; all failure state is persisted in a fresh transaction."""

    master_db = MasterSessionLocal()
    try:
        run = master_db.get(TenantProvisioningRun, run_id)
        if run is None:
            return False
        company = master_db.get(MasterCompany, run.company_id)
        tenant_row = master_db.scalar(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.company_id == run.company_id,
                MasterTenantDatabase.is_active.is_(True),
            )
        )
        if company is None or tenant_row is None or not tenant_row.database_url:
            raise ValueError("tenant_configuration_missing")

        # Private helpers are imported lazily to avoid a service/worker import cycle.
        from app.superadmin.service import _engine_for, _ensure_local_actor, _provision_tenant_schema

        _set_step(master_db, run_id, "schema")
        _provision_tenant_schema(company, tenant_row.database_url, display_name=company.name)

        _set_step(master_db, run_id, "actors")
        memberships = master_db.scalars(
            select(CompanyMembership).where(
                CompanyMembership.company_id == company.id,
                CompanyMembership.is_active.is_(True),
            )
        ).all()
        engine = _engine_for(tenant_row.database_url)
        try:
            tenant_db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
            try:
                for membership in memberships:
                    master_user = master_db.get(MasterUser, membership.user_id)
                    if master_user is not None:
                        _ensure_local_actor(
                            tenant_db,
                            company.id,
                            master_user,
                            role_key=membership.role_key or "Operador",
                        )
                tenant_db.commit()
            finally:
                tenant_db.close()
        finally:
            engine.dispose()

        _set_step(master_db, run_id, "ready")
        tenant_row = master_db.get(MasterTenantDatabase, tenant_row.id)
        company = master_db.get(MasterCompany, company.id)
        run = master_db.get(TenantProvisioningRun, run_id)
        if tenant_row is not None:
            tenant_row.health_status = "ok"
            tenant_row.provisioned_at = tenant_row.provisioned_at or _now()
        if company is not None:
            company.active = True
            company.status = "active"
            company.provisioning_status = "ready"
        if run is not None:
            run.status = "completed"
            run.current_step = "ready"
            run.finished_at = _now()
        master_db.commit()
        logger.info("company.provisioning.completed company_id=%s run_id=%s", company.id if company else None, run_id)
        return True
    except Exception as exc:  # noqa: BLE001
        master_db.rollback()
        logger.exception("company.provisioning.failed run_id=%s", run_id)
        # Never persist provider URLs, passwords, or exception messages: the
        # platform only needs a stable error class and the current step.
        failed_db = MasterSessionLocal()
        try:
            run = failed_db.get(TenantProvisioningRun, run_id)
            if run is not None:
                run.status = "failed"
                run.current_step = "failed"
                run.error_message = exc.__class__.__name__
                run.finished_at = _now()
                company = failed_db.get(MasterCompany, run.company_id)
                if company is not None:
                    company.active = False
                    company.status = "error"
                    company.provisioning_status = "failed"
                failed_db.commit()
        finally:
            failed_db.close()
        return False
    finally:
        master_db.close()


def run_provisioning_cycle(*, max_runs: int = 1) -> dict[str, int]:
    """Claim and process a bounded number of provisioning operations."""

    summary = {"claimed": 0, "completed": 0, "failed": 0}
    for _ in range(max(0, int(max_runs))):
        master_db = MasterSessionLocal()
        try:
            run_id = _claim_run(master_db)
        finally:
            master_db.close()
        if run_id is None:
            break
        summary["claimed"] += 1
        if _provision_run(run_id):
            summary["completed"] += 1
        else:
            summary["failed"] += 1
    return summary
