from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
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


def _schema_is_current(report: dict | None) -> bool:
    return bool(report and report.get("status") == "current" and report.get("is_current") is True)


def _schema_error_report(error: SQLAlchemyError) -> dict:
    return {"status": "error", "is_current": False, "error_type": type(error).__name__}


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
    try:
        master_ping = _ping_db(master_db)
    except SQLAlchemyError as exc:
        master_ping = False
        master_error = type(exc).__name__
    else:
        master_error = None
    try:
        master_schema = master_migration_report(master_db, persist=False)
    except SQLAlchemyError as exc:
        master_schema = _schema_error_report(exc)
    payload = {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "master": master_ping,
        "master_schema_report": master_schema,
        "tenant": bool(tenant),
        "tenant_evaluated": bool(tenant),
        "tenant_ready": None,
        "tenant_schema_ok": None,
        "tenant_company_id": tenant.company.id if tenant else None,
        "tenant_slug": tenant.company.slug if tenant else None,
        "tenant_database_configured": bool(tenant and getattr(tenant.company, "database_url", None)),
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
        "metrics": snapshot_metrics(),
        "storage_ready": True,
        "workers_ready": {"email_sync": is_email_sync_worker_started(), "jobs": is_job_worker_started()},
    }
    if master_error:
        payload["master_error"] = master_error
    if tenant and tenant.company.database_url:
        session_factory = tenant_db_session(tenant.company.database_url)
        tenant_db = session_factory()
        try:
            try:
                payload["tenant_ping"] = _ping_db(tenant_db)
            except SQLAlchemyError as exc:
                payload["tenant_ping"] = False
                payload["tenant_error"] = type(exc).__name__
            try:
                payload["tenant_schema_report"] = tenant_migration_report(tenant_db, tenant.company.id, persist=False)
            except SQLAlchemyError as exc:
                payload["tenant_schema_report"] = _schema_error_report(exc)
        finally:
            tenant_db.close()
    master_schema_ok = _schema_is_current(payload["master_schema_report"])
    payload["master_schema_ok"] = master_schema_ok
    payload["platform_ready"] = bool(payload["master"] and master_schema_ok)
    payload["ok"] = payload["platform_ready"]
    if tenant and tenant.company.database_url:
        tenant_schema_ok = _schema_is_current(payload["tenant_schema_report"])
        payload["tenant_schema_ok"] = tenant_schema_ok
        payload["tenant_ready"] = bool(payload.get("tenant_ping") and tenant_schema_ok)
        payload["ok"] = payload["ok"] and payload["tenant_ready"]
    elif tenant:
        payload["tenant_ready"] = False
        payload["tenant_schema_ok"] = False
    return payload


@router.get("/health/master")
def master_health(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "master": _ping_db(master_db), "metrics": snapshot_metrics()}


@router.get("/health/tenant")
def tenant_health(db: Session = Depends(get_tenant_db), user=Depends(current_tenant_user)):
    schema_report = tenant_migration_report(db, user.company_id)
    try:
        tenant_ping = _ping_db(db)
    except SQLAlchemyError as exc:
        tenant_ping = False
        tenant_error = type(exc).__name__
    else:
        tenant_error = None
    schema_ok = _schema_is_current(schema_report)
    payload = {
        "ok": bool(tenant_ping and schema_ok),
        "timestamp": datetime.now(timezone.utc),
        "company_id": user.company_id,
        "tenant_evaluated": True,
        "tenant": tenant_ping,
        "tenant_schema_ok": schema_ok,
        "schema_report": schema_report,
        "metrics": snapshot_metrics(),
    }
    if tenant_error:
        payload["tenant_error"] = tenant_error
    return payload


@router.get("/admin/tenants/{company_id}/health")
def admin_tenant_health(company_id: int, master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return company_diagnostics(master_db, company_id)


@router.get("/admin/tenants")
def admin_tenants(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"items": company_diagnostics_overview(master_db)}


@router.get("/health/metrics")
def health_metrics():
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "metrics": snapshot_metrics()}
