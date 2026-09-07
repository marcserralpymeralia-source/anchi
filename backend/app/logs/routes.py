from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_tenant_role
from app.core.timezones import DEFAULT_TIMEZONE, format_local_datetime, resolve_timezone_name
from app.core.templating import templates
from app.db.models import AuditLog, Company
from app.logs.service import audit_log_text, parse_audit_log_message
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/logs", tags=["logs"])

VIEW_LOG_ROLES = ("Administrador", "Superadmin", "Supervisor")
DELETE_LOG_ROLES = ("Administrador", "Superadmin")
LOG_PAGE_LIMIT = 200
LOG_DOWNLOAD_LIMIT = 50_000


def _logs_statement(
    company_id: int,
    *,
    action: str = "",
    entity_type: str = "",
    request_id: str = "",
    correlation_id: str = "",
    flow_id: str = "",
    search: str = "",
):
    stmt = select(AuditLog).where(AuditLog.company_id == company_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if request_id:
        stmt = stmt.where(AuditLog.message.ilike(f"%{request_id}%"))
    if correlation_id:
        stmt = stmt.where(AuditLog.message.ilike(f"%{correlation_id}%"))
    if flow_id:
        stmt = stmt.where(AuditLog.message.ilike(f"%{flow_id}%"))
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            or_(
                AuditLog.action.ilike(like),
                AuditLog.message.ilike(like),
                AuditLog.entity_type.ilike(like),
            )
        )
    return stmt


def _filter_values(
    *,
    action: str,
    entity_type: str,
    request_id: str,
    correlation_id: str,
    flow_id: str,
    search: str,
) -> dict[str, str]:
    return {
        "action": action,
        "entity_type": entity_type,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "flow_id": flow_id,
        "search": search,
    }


def _download_url(filters: dict[str, str]) -> str:
    query = urlencode({key: value for key, value in filters.items() if value})
    return f"/logs/download?{query}" if query else "/logs/download"


def _derive_status(action: str, message: str, explicit_status: str | None) -> tuple[str, str, str]:
    if explicit_status:
        s = explicit_status.lower().strip()
        if s in ("error", "failed", "failure", "ko"):
            return "error", "Error", "failed"
        if s in ("success", "completed", "persisted", "ok", "done"):
            return "success", "Correcto", "success"
        if s in ("started", "processing", "running", "in_progress"):
            return "running", "Iniciado", "running"
        if s in ("queued", "enqueued", "pending"):
            return "queued", "En cola", "queued"
        if s in ("info", "notice"):
            return "info", "Info", "neutral"

    act = (action or "").lower()
    msg = (message or "").lower()

    if "error" in act or "failed" in act or "fail" in act or "error" in msg or "exception" in msg or "errno" in msg or "fallo" in msg:
        return "error", "Error", "failed"
    if "started" in act or "iniciad" in msg or "running" in act or "processing" in act or "iniciando" in msg:
        return "running", "Iniciado", "running"
    if "queued" in act or "enqueued" in act:
        return "queued", "En cola", "queued"
    if any(k in act for k in ("update", "add", "create", "delete", "save", "sync", "complete", "valid", "finish", "export", "success")):
        return "success", "Correcto", "success"

    return "info", "Info", "neutral"


def _company_timezone_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return resolve_timezone_name(getattr(company, "timezone", None) if company else DEFAULT_TIMEZONE)


def _serialize_audit_log(log: AuditLog, *, timezone_name: str = DEFAULT_TIMEZONE) -> dict:
    parsed = parse_audit_log_message(log.message)
    context = parsed.get("context") or {}
    metadata = parsed.get("metadata") or {}
    flow_id = metadata.get("flow_id") or context.get("flow_id")
    raw_status = metadata.get("status")
    status_key, status_label, status_class = _derive_status(
        action=log.action,
        message=parsed.get("message") or "",
        explicit_status=raw_status,
    )
    return {
        "id": log.id,
        "created_at": log.created_at,
        "created_label": format_local_datetime(log.created_at, timezone_name, "%d/%m/%Y %H:%M:%S", ""),
        "action": log.action,
        "event": metadata.get("event") or log.action,
        "stage": metadata.get("stage") or "audit",
        "status": status_key,
        "status_label": status_label,
        "status_class": status_class,
        "flow_id": flow_id,
        "entity_type": log.entity_type,
        "entity_id": log.entity_id,
        "message": parsed.get("message") or "",
        "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str) if metadata else "",
        "prompt_execution_id": metadata.get("prompt_execution_id"),
        "request_id": context.get("request_id"),
        "correlation_id": context.get("correlation_id"),
        "tenant_id": context.get("tenant_id"),
        "user_id": context.get("user_id"),
        "membership_id": context.get("membership_id"),
        "job_id": context.get("job_id"),
        "worker_id": context.get("worker_id"),
    }


@router.get("/download", response_class=PlainTextResponse)
def logs_download(
    action: str = "",
    entity_type: str = "",
    request_id: str = "",
    correlation_id: str = "",
    flow_id: str = "",
    search: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*VIEW_LOG_ROLES)),
):
    filters = _filter_values(
        action=action,
        entity_type=entity_type,
        request_id=request_id,
        correlation_id=correlation_id,
        flow_id=flow_id,
        search=search,
    )
    logs = db.scalars(
        _logs_statement(user.company_id, **filters)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .limit(LOG_DOWNLOAD_LIMIT)
    ).all()
    timezone_name = _company_timezone_name(db, user.company_id)
    return PlainTextResponse(
        audit_log_text(logs, timezone_name=timezone_name),
        headers={
            "Content-Disposition": 'attachment; filename="anchi-flow-logs.log"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/delete")
def delete_logs(
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*DELETE_LOG_ROLES)),
):
    result = db.execute(delete(AuditLog).where(AuditLog.company_id == user.company_id))
    db.commit()
    logger.warning(
        "audit_logs.deleted",
        extra={
            "event": "audit_logs.deleted",
            "company_id": user.company_id,
            "user_id": user.id,
            "deleted_count": result.rowcount,
        },
    )
    return RedirectResponse("/logs?deleted=1", status_code=303)


@router.get("")
def logs_page(
    request: Request,
    action: str = "",
    entity_type: str = "",
    request_id: str = "",
    correlation_id: str = "",
    flow_id: str = "",
    search: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(require_tenant_role(*VIEW_LOG_ROLES)),
):
    filters = _filter_values(
        action=action,
        entity_type=entity_type,
        request_id=request_id,
        correlation_id=correlation_id,
        flow_id=flow_id,
        search=search,
    )
    logs = db.scalars(
        _logs_statement(user.company_id, **filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(LOG_PAGE_LIMIT)
    ).all()
    timezone_name = _company_timezone_name(db, user.company_id)
    items = [_serialize_audit_log(log, timezone_name=timezone_name) for log in logs]
    return templates.TemplateResponse(
        "logs/list.html",
        {
            "request": request,
            "user": user,
            "logs": items,
            "filters": filters,
            "download_url": _download_url(filters),
            "can_delete_logs": user.role.name in DELETE_LOG_ROLES,
            "deleted": request.query_params.get("deleted") == "1",
        },
    )
