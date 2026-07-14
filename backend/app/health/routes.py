from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.dependencies import current_tenant_user, require_master_admin
from app.admin.diagnostics import company_diagnostics, company_diagnostics_overview
from app.master.database import get_master_db
from app.master.models import MasterTenantDatabase
from app.tenancy.database import get_tenant_db
from app.tenancy.migrations import tenant_migration_report

router = APIRouter()


def _ping_db(db: Session) -> bool:
    db.execute(text("SELECT 1"))
    return True


@router.get("/health")
def health(request: Request, master_db: Session = Depends(get_master_db)):
    tenant = getattr(request.state, "tenant", None)
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "master": _ping_db(master_db),
        "tenant": bool(tenant),
        "tenant_company_id": tenant.company.id if tenant else None,
        "tenant_slug": tenant.company.slug if tenant else None,
        "tenant_database_key": tenant.company.database_key if tenant and getattr(tenant.company, "database_key", None) else None,
        "tenant_database_configured": bool(tenant and getattr(tenant.company, "database_url", None)),
    }


@router.get("/health/master")
def master_health(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "master": _ping_db(master_db)}


@router.get("/health/tenant")
def tenant_health(db: Session = Depends(get_tenant_db), user=Depends(current_tenant_user)):
    schema_report = tenant_migration_report(db, user.company_id)
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "company_id": user.company_id,
        "tenant": _ping_db(db),
        "schema_report": schema_report,
    }


@router.get("/admin/tenants/{company_id}/health")
def admin_tenant_health(company_id: int, master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return company_diagnostics(master_db, company_id)


@router.get("/admin/tenants")
def admin_tenants(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"items": company_diagnostics_overview(master_db)}
