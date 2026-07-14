from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.db.models import AuditLog, User
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("")
def logs_page(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    logs = db.scalars(select(AuditLog).where(AuditLog.company_id == user.company_id).order_by(AuditLog.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse("logs/list.html", {"request": request, "user": user, "logs": logs})

from app.core.templating import templates
