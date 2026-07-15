from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import require_master_admin
from app.admin.diagnostics import company_diagnostics, company_diagnostics_overview
from app.core.metrics import snapshot_metrics
from app.core.templating import templates
from app.master.database import get_master_db

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/diagnostics")
def diagnostics_page(request: Request, master_db: Session = Depends(get_master_db), user=Depends(require_master_admin)):
    companies = company_diagnostics_overview(master_db)
    metrics = snapshot_metrics()
    return templates.TemplateResponse(
        "admin/diagnostics.html",
        {
            "request": request,
            "user": user,
            "companies": companies,
            "totals": {
                "companies": len(companies),
                "active": len([company for company in companies if company["company_active"]]),
                "imap_ready": len([company for company in companies if company["imap_ready"]]),
                "llm_ready": len([company for company in companies if company["llm_ready"]]),
                "tenant_ok": len([company for company in companies if company["tenant_database_status"] == "ok"]),
                "requests_total": metrics["requests_total"],
                "jobs_total": metrics["jobs_total"],
                "jobs_failed": metrics["jobs_by_status"].get("failed", 0),
                "avg_request_ms": metrics["requests_avg_duration_ms"],
            },
            "observability": metrics,
        },
    )


@router.get("/tenants")
def list_tenant_diagnostics(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return JSONResponse({"items": company_diagnostics_overview(master_db)})


@router.get("/tenants/{company_id}/diagnostics")
def tenant_diagnostics(company_id: int, master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return JSONResponse(company_diagnostics(master_db, company_id))
