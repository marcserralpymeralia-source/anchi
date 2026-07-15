from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.core.templating import templates
from app.db.models import AuditLog
from app.logs.service import parse_audit_log_message
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/logs", tags=["logs"])


def _serialize_audit_log(log: AuditLog) -> dict:
    parsed = parse_audit_log_message(log.message)
    context = parsed.get("context") or {}
    return {
        "id": log.id,
        "created_at": log.created_at,
        "created_label": log.created_at.strftime("%d/%m %H:%M") if log.created_at else "",
        "action": log.action,
        "entity_type": log.entity_type,
        "entity_id": log.entity_id,
        "message": parsed.get("message") or "",
        "request_id": context.get("request_id"),
        "correlation_id": context.get("correlation_id"),
        "tenant_id": context.get("tenant_id"),
        "user_id": context.get("user_id"),
        "membership_id": context.get("membership_id"),
        "job_id": context.get("job_id"),
        "worker_id": context.get("worker_id"),
    }


@router.get("")
def logs_page(
    request: Request,
    action: str = "",
    entity_type: str = "",
    request_id: str = "",
    correlation_id: str = "",
    search: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    stmt = select(AuditLog).where(AuditLog.company_id == user.company_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if request_id:
        stmt = stmt.where(AuditLog.message.ilike(f"%{request_id}%"))
    if correlation_id:
        stmt = stmt.where(AuditLog.message.ilike(f"%{correlation_id}%"))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(AuditLog.action.ilike(like), AuditLog.message.ilike(like), AuditLog.entity_type.ilike(like)))
    logs = db.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(200)).all()
    items = [_serialize_audit_log(log) for log in logs]
    return templates.TemplateResponse(
        "logs/list.html",
        {
            "request": request,
            "user": user,
            "logs": items,
            "filters": {
                "action": action,
                "entity_type": entity_type,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "search": search,
            },
        },
    )

