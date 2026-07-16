from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.dependencies import current_tenant_user, require_master_admin
from app.admin.diagnostics import company_diagnostics, company_diagnostics_overview
from app.core.metrics import snapshot_metrics
from app.master.database import get_master_db
from app.master.migrations import master_migration_report
from app.tenancy.database import get_tenant_db
from app.tenancy.database import tenant_db_session
from app.tenancy.migrations import tenant_migration_report
from app.workers.email_worker import is_email_sync_worker_started
from app.workers.jobs_worker import is_job_worker_started

router = APIRouter()


def _ping_db(db: Session) -> bool:
    db.execute(text("SELECT 1"))
    return True


@router.get("/health")
def health(request: Request, master_db: Session = Depends(get_master_db)):
    return health_ready(request, master_db)


@router.get("/health/live")
def health_live(request: Request):
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
        "metrics": snapshot_metrics(),
    }


@router.get("/health/ready")
def health_ready(request: Request, master_db: Session = Depends(get_master_db)):
    tenant = getattr(request.state, "tenant", None)
    master_schema = master_migration_report(master_db, persist=False)
    payload = {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "master": _ping_db(master_db),
        "master_schema_report": master_schema,
        "tenant": bool(tenant),
        "tenant_company_id": tenant.company.id if tenant else None,
        "tenant_slug": tenant.company.slug if tenant else None,
        "tenant_database_configured": bool(tenant and getattr(tenant.company, "database_url", None)),
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
        "metrics": snapshot_metrics(),
        "storage_ready": True,
        "workers_ready": {"email_sync": is_email_sync_worker_started(), "jobs": is_job_worker_started()},
    }
    if tenant and tenant.company.database_url:
        session_factory = tenant_db_session(tenant.company.database_url)
        tenant_db = session_factory()
        try:
            payload["tenant_ping"] = _ping_db(tenant_db)
            payload["tenant_schema_report"] = tenant_migration_report(tenant_db, tenant.company.id, persist=False)
        finally:
            tenant_db.close()
    master_schema_status = payload["master_schema_report"].get("status")
    master_schema_ok = master_schema_status in {None, "missing", "incomplete", "current"}
    payload["master_schema_ok"] = master_schema_ok
    payload["ok"] = bool(payload["master"] and (not tenant or payload.get("tenant_ping", True)) and master_schema_ok)
    if tenant and tenant.company.database_url:
        tenant_schema_status = payload["tenant_schema_report"].get("status")
        tenant_schema_ok = tenant_schema_status in {None, "missing", "incomplete", "current"}
        payload["tenant_schema_ok"] = tenant_schema_ok
        payload["ok"] = payload["ok"] and tenant_schema_ok
    return payload


@router.get("/health/master")
def master_health(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "master": _ping_db(master_db), "metrics": snapshot_metrics()}


@router.get("/health/tenant")
def tenant_health(db: Session = Depends(get_tenant_db), user=Depends(current_tenant_user)):
    schema_report = tenant_migration_report(db, user.company_id)
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "company_id": user.company_id,
        "tenant": _ping_db(db),
        "schema_report": schema_report,
        "metrics": snapshot_metrics(),
    }


@router.get("/admin/tenants/{company_id}/health")
def admin_tenant_health(company_id: int, master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return company_diagnostics(master_db, company_id)


@router.get("/admin/tenants")
def admin_tenants(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"items": company_diagnostics_overview(master_db)}


@router.get("/health/metrics")
def health_metrics():
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "metrics": snapshot_metrics()}
