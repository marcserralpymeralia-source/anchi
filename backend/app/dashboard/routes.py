from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.dashboard.service import dashboard_summary, operational_summary
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary")
def dashboard_summary_endpoint(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    return JSONResponse(jsonable_encoder(dashboard_summary(db, user.company_id, dict(request.query_params))))


@router.get("/dashboard/operational-summary")
def dashboard_operational_summary_endpoint(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    payload = operational_summary(db, user.company_id, dict(request.query_params))
    payload.pop("orders", None)
    return JSONResponse(jsonable_encoder(payload))
