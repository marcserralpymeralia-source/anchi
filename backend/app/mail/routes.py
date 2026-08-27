from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import current_user
from app.core.entry_workflow import queue_email_processing, mark_email_no_order
from app.core.templating import templates
from app.dashboard.service import email_workbench_item
from app.db.models import Email, Order
from app.logs.service import log_action
from app.master.database import get_master_db
from app.master.models import EmailSyncState
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/mail", tags=["mail"])
logger = logging.getLogger(__name__)


def _redirect_back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _active_mail_scope(master_db: Session, company_id: int) -> tuple[str | None, str | None]:
    state = master_db.scalar(
        select(EmailSyncState).where(
            EmailSyncState.company_id == company_id,
            EmailSyncState.channel_key == "email",
        )
    )
    if not state:
        return None, None
    return (state.mailbox or None), (state.uidvalidity or None)


def _latest_email_scope(db: Session, company_id: int) -> tuple[str | None, str | None]:
    row = db.execute(
        select(Email.imap_mailbox, Email.imap_uidvalidity)
        .where(
            Email.company_id == company_id,
            Email.archived.is_(False),
            Email.imap_mailbox.is_not(None),
        )
        .order_by(Email.received_at.desc(), Email.id.desc())
        .limit(1)
    ).first()
    if not row:
        return None, None
    mailbox, uidvalidity = row
    return (mailbox or None), (uidvalidity or None)


def _resolve_mail_scope(master_db: Session, db: Session, company_id: int) -> tuple[str | None, str | None]:
    try:
        scope = _active_mail_scope(master_db, company_id)
    except SQLAlchemyError:
        logger.warning(
            "mail.scope_resolution.master_error",
            extra={
                "event": "mail.scope_resolution.master_error",
                "company_id": company_id,
            },
            exc_info=True,
        )
        scope = None
    if scope and any(scope):
        return scope
    fallback_scope = _latest_email_scope(db, company_id)
    if fallback_scope and any(fallback_scope):
        logger.info(
            "mail.scope_resolution.fallback_scope",
            extra={
                "event": "mail.scope_resolution.fallback_scope",
                "company_id": company_id,
                "mailbox": fallback_scope[0],
                "uidvalidity": fallback_scope[1],
            },
        )
        return fallback_scope
    return None, None


def _email_matches_active_scope(email: Email, scope: tuple[str | None, str | None]) -> bool:
    mailbox, uidvalidity = scope
    if mailbox and email.imap_mailbox != mailbox:
        return False
    if uidvalidity and email.imap_uidvalidity != uidvalidity:
        return False
    return True


@router.get("")
def mail_inbox(request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    return RedirectResponse("/", status_code=303)


@router.get("/{email_id}")
def mail_detail(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    active_scope = _resolve_mail_scope(master_db, db, user.company_id)
    email = db.scalar(
        select(Email)
        .where(Email.id == email_id, Email.company_id == user.company_id)
        .options(selectinload(Email.attachments))
    )
    if not email or not _email_matches_active_scope(email, active_scope):
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
def mail_process(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    active_scope = _resolve_mail_scope(master_db, db, user.company_id)
    email = db.get(Email, email_id)
    if not email or email.company_id != user.company_id or not _email_matches_active_scope(email, active_scope):
        return PlainTextResponse("No encontrado", status_code=404)
    job = queue_email_processing(db, company_id=user.company_id, user_id=user.id, email_id=email_id)
    log_action(db, company_id=user.company_id, user=user, action="mail.process", entity_type="job", entity_id=job.id, message=f"Correo encolado para procesar: {email_id}")
    return _redirect_back(request)


@router.post("/{email_id}/reprocess")
def mail_reprocess(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    active_scope = _resolve_mail_scope(master_db, db, user.company_id)
    email = db.get(Email, email_id)
    if not email or email.company_id != user.company_id or not _email_matches_active_scope(email, active_scope):
        return PlainTextResponse("No encontrado", status_code=404)
    job = queue_email_processing(db, company_id=user.company_id, user_id=user.id, email_id=email_id, force=True)
    log_action(db, company_id=user.company_id, user=user, action="mail.reprocess", entity_type="job", entity_id=job.id, message=f"Correo reencolado: {email_id}")
    return _redirect_back(request)


@router.post("/{email_id}/mark-read")
def mail_mark_read(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    active_scope = _resolve_mail_scope(master_db, db, user.company_id)
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id and _email_matches_active_scope(email, active_scope):
        email.is_read = True
        email.archived = False
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="mail.mark_read", entity_type="email", entity_id=email.id, message="Correo marcado como leído")
    return _redirect_back(request)


@router.post("/{email_id}/archive")
def mail_archive(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    active_scope = _resolve_mail_scope(master_db, db, user.company_id)
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id and _email_matches_active_scope(email, active_scope):
        email.is_read = True
        email.archived = True
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="mail.archive", entity_type="email", entity_id=email.id, message="Correo archivado")
    return _redirect_back(request)


@router.post("/{email_id}/mark-no-order")
def mail_mark_no_order(email_id: int, request: Request, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    active_scope = _resolve_mail_scope(master_db, db, user.company_id)
    email = db.get(Email, email_id)
    if email and email.company_id == user.company_id and _email_matches_active_scope(email, active_scope):
        email.is_read = True
        mark_email_no_order(db, company_id=user.company_id, user_id=user.id, email_id=email.id)
        log_action(db, company_id=user.company_id, user=user, action="mail.mark_no_order", entity_type="email", entity_id=email.id, message="Correo marcado como sin pedido")
    return _redirect_back(request)


@router.post("/{email_id}/link-order")
def mail_link_order(email_id: int, request: Request, order_id: int = Form(...), db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user), master_db: Session = Depends(get_master_db)):
    active_scope = _resolve_mail_scope(master_db, db, user.company_id)
    email = db.get(Email, email_id)
    order = db.get(Order, order_id)
    if email and order and email.company_id == user.company_id and order.company_id == user.company_id and _email_matches_active_scope(email, active_scope):
        order.email_id = email.id
        email.is_read = True
        email.archived = False
        db.commit()
        log_action(db, company_id=user.company_id, user=user, action="mail.link_order", entity_type="order", entity_id=order.id, message=f"Correo {email.id} vinculado al pedido {order.id}")
    return _redirect_back(request)
