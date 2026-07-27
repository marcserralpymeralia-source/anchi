from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import current_user
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.dashboard.service import email_workbench_item
from app.db.models import Email, EmailSettings, Order
from app.jobs.service import enqueue_job
from app.logs.service import log_action
from app.master.service import TenantUser
from app.settings.email_config import email_config_status
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/mail", tags=["mail"])


def _redirect_back(request: Request, fallback: str = "/mail") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _mail_cutoff(date_range: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if date_range == "today":
        return datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    if date_range == "7d":
        return now - timedelta(days=7)
    if date_range == "30d":
        return now - timedelta(days=30)
    return None


def _mail_status_key(email: Email) -> str:
    if email.archived:
        return "archived"
    if email.status.startswith("error") or email.agent_status == "error":
        return "error"
    if email.status == "no_pedido" or email.detected_type == "no_pedido" or email.agent_status == "processed_no_order":
        return "no_order"
    if email.agent_status in {"processed_doubtful", "pending_reprocess"} or email.status == "dudoso":
        return "review"
    if email.agent_status in {"processed_order_detected", "processed"} or email.status in {"processed", "cerrado", "pedido_confirmado", "pedido_validado"}:
        return "processed"
    if email.is_read:
        return "pending"
    return "unread"


def _apply_mail_filters(stmt, filters: dict) -> object:
    cutoff = _mail_cutoff(filters.get("date_range", "30d"))
    if cutoff:
        stmt = stmt.where(Email.received_at >= cutoff)
    search = (filters.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Email.sender.ilike(like), Email.subject.ilike(like), Email.body.ilike(like)))
    status = filters.get("status") or "all"
    if status == "unread":
        stmt = stmt.where(Email.is_read.is_(False), Email.archived.is_(False))
    elif status == "pending":
        stmt = stmt.where(Email.is_read.is_(False), Email.agent_status == "not_processed", Email.archived.is_(False))
    elif status == "processed":
        stmt = stmt.where(Email.agent_status.in_(("processed", "processed_order_detected", "processed_no_order", "processed_doubtful")), Email.archived.is_(False))
    elif status == "review":
        stmt = stmt.where(or_(Email.status == "dudoso", Email.agent_status.in_(("processed_doubtful", "pending_reprocess"))), Email.archived.is_(False))
    elif status == "no_order":
        stmt = stmt.where(or_(Email.status == "no_pedido", Email.detected_type == "no_pedido", Email.agent_status == "processed_no_order"), Email.archived.is_(False))
    elif status == "error":
        stmt = stmt.where(or_(Email.status.like("error%"), Email.agent_status == "error"))
    elif status == "archived":
        stmt = stmt.where(Email.archived.is_(True))
    else:
        stmt = stmt.where(Email.archived.is_(False))
    return stmt


def _order_map(db: Session, company_id: int, email_ids: list[int]) -> dict[int, Order]:
    if not email_ids:
        return {}
    orders = db.scalars(
        select(Order)
        .where(Order.company_id == company_id, Order.email_id.in_(email_ids))
        .options(
            selectinload(Order.customer),
            selectinload(Order.validated_customer),
            selectinload(Order.lines),
            selectinload(Order.email).selectinload(Email.attachments),
        )
    ).all()
    return {order.email_id: order for order in orders if order.email_id}


def _load_mail_rows(db: Session, company_id: int, filters: dict) -> tuple[list[Email], dict[int, Order], dict[str, int]]:
    stmt = select(Email).where(Email.company_id == company_id).options(selectinload(Email.attachments))
    stmt = _apply_mail_filters(stmt, filters)
    emails = db.scalars(stmt.order_by(Email.received_at.desc())).unique().all()
    orders_by_email = _order_map(db, company_id, [email.id for email in emails])
    sort = (filters.get("sort") or "date_desc").strip()
    if sort == "date_asc":
        emails.sort(key=lambda email: email.received_at or datetime.min.replace(tzinfo=timezone.utc))
    elif sort == "status_asc":
        emails.sort(key=lambda email: (_mail_status_key(email), email.received_at or datetime.min.replace(tzinfo=timezone.utc)))
    elif sort == "score_desc":
        emails.sort(
            key=lambda email: (
                orders_by_email.get(email.id).score if orders_by_email.get(email.id) else -1,
                email.received_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
    counts = {
        "total": len(emails),
        "unread": len([email for email in emails if _mail_status_key(email) == "unread"]),
        "pending": len([email for email in emails if _mail_status_key(email) == "pending"]),
        "processed": len([email for email in emails if _mail_status_key(email) == "processed"]),
        "review": len([email for email in emails if _mail_status_key(email) == "review"]),
        "no_order": len([email for email in emails if _mail_status_key(email) == "no_order"]),
        "error": len([email for email in emails if _mail_status_key(email) == "error"]),
        "archived": len([email for email in emails if _mail_status_key(email) == "archived"]),
    }
    return emails, orders_by_email, counts


@router.get("")
def mail_inbox(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    filters = {
        "status": request.query_params.get("status", "all"),
        "date_range": request.query_params.get("date_range", "30d"),
        "search": request.query_params.get("search", ""),
        "sort": request.query_params.get("sort", "date_desc"),
        "page": request.query_params.get("page", "1"),
        "page_size": request.query_params.get("page_size", "25"),
    }
    emails, orders_by_email, counts = _load_mail_rows(db, user.company_id, filters)
    page, page_size = normalize_page(int(filters["page"] or 1), int(filters["page_size"] or 25))
    total_items = len(emails)
    start = (page - 1) * page_size
    paged_emails = emails[start : start + page_size]
    pagination = {
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": (total_items + page_size - 1) // page_size if total_items else 1,
        "allowed_page_sizes": [10, 25, 50, 100],
    }
    return templates.TemplateResponse(
        "mail/list.html",
        {
            "request": request,
            "user": user,
            "title": "Bandeja de entrada",
            "emails": paged_emails,
            "orders_by_email": orders_by_email,
            "filters": filters,
            "pagination": pagination,
            "counts": counts,
            "mail_settings": get_or_create_settings(db, EmailSettings, user.company_id),
            "email_status": email_config_status(get_or_create_settings(db, EmailSettings, user.company_id)),
        },
    )


@router.get("/{email_id}")
def mail_detail(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    email = db.scalar(
        select(Email)
        .where(Email.id == email_id, Email.company_id == user.company_id)
        .options(selectinload(Email.attachments))
    )
    if not email:
        return PlainTextResponse("No encontrado", status_code=404)
    order = db.scalar(
        select(Order)
        .where(Order.company_id == user.company_id, Order.email_id == email.id)
        .options(
            selectinload(Order.lines),
            selectinload(Order.customer),
            selectinload(Order.validated_customer),
        )
    )
    candidates = db.scalars(select(Order).where(Order.company_id == user.company_id).order_by(Order.created_at.desc()).limit(50)).all()
    item = email_workbench_item(email)
    if order:
        item["order_id"] = order.id
        item["order_status"] = order.status
        item["order_status_label"] = order.status
        item["score"] = order.score
        item["customer_name"] = (order.validated_customer or order.customer).fiscal_name if (order.validated_customer or order.customer) else order.customer_detected_name or item["customer_name"]
    return templates.TemplateResponse(
        "mail/detail.html",
        {
            "request": request,
            "user": user,
            "title": "Detalle de correo",
            "email": email,
            "order": order,
            "candidate_orders": candidates,
            "item": item,
        },
    )


@router.post("/{email_id}/process")
def mail_process(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    job = enqueue_job(db, company_id=user.company_id, job_type="process_email", payload={"email_id": email_id}, created_by_user_id=user.id)
    log_action(db, company_id=user.company_id, user=user, action="mail.process", entity_type="job", entity_id=job.id, message=f"Correo encolado para procesar: {email_id}")
    return _redirect_back(request)


@router.post("/{email_id}/reprocess")
def mail_reprocess(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    job = enqueue_job(db, company_id=user.company_id, job_type="process_email", payload={"email_id": email_id, "force": True}, created_by_user_id=user.id)
    log_action(db, company_id=user.company_id, user=user, action="mail.reprocess", entity_type="job", entity_id=job.id, message=f"Correo reencolado: {email_id}")
    return _redirect_back(request)


@router.post("/{email_id}/mark-read")
def mail_mark_read(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id:
        email.is_read = True
        email.archived = False
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="mail.mark_read", entity_type="email", entity_id=email.id, message="Correo marcado como leído")
    return _redirect_back(request)


@router.post("/{email_id}/archive")
def mail_archive(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id:
        email.is_read = True
        email.archived = True
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="mail.archive", entity_type="email", entity_id=email.id, message="Correo archivado")
    return _redirect_back(request)


@router.post("/{email_id}/mark-no-order")
def mail_mark_no_order(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id:
        email.status = "no_pedido"
        email.agent_status = "processed_no_order"
        email.detected_type = "no_pedido"
        email.is_read = True
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="mail.mark_no_order", entity_type="email", entity_id=email.id, message="Correo marcado como sin pedido")
    return _redirect_back(request)


@router.post("/{email_id}/link-order")
def mail_link_order(email_id: int, request: Request, order_id: int = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    email = db.get(Email, email_id)
    order = db.get(Order, order_id)
    if email and order and email.company_id == user.company_id and order.company_id == user.company_id:
        order.email_id = email.id
        email.is_read = True
        email.archived = False
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="mail.link_order", entity_type="order", entity_id=order.id, message=f"Correo {email.id} vinculado al pedido {order.id}")
    return _redirect_back(request)
