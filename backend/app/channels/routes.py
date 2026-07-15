from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import defaultdict
from html import escape
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import and_, case, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, aliased

from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.dashboard.service import agent_status_label
from app.db.models import Customer, Email, EmailAttachment, EmailSettings, InboundMessage, InputChannel, MessageAttachment, Order
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/channels", tags=["channels"])
CHANNEL_EMAIL_STATUS_MAP = {
    "processed_order_detected": "order_detected",
    "processed_no_order": "no_order",
    "processed_doubtful": "doubtful",
    "processing_error": "error",
    "queued": "queued",
    "processing": "processing",
    "pending_reprocess": "pending_reprocess",
    "discarded": "discarded",
    "not_processed": "not_processed",
}
CHANNEL_INBOUND_STATUS_MAP = {
    "received": "not_processed",
    "queued": "queued",
    "processing": "processing",
    "processed": "processed",
    "matched": "order_detected",
    "order_detected": "order_detected",
    "no_order": "no_order",
    "doubtful": "doubtful",
    "error": "error",
}


def _score_bucket(score: float | None) -> tuple[str, str]:
    if score is None:
        return "without", "Sin analizar"
    if score >= 90:
        return "strong", "Alta"
    if score >= 75:
        return "soft", "Alta"
    if score >= 50:
        return "amber", "Revisable"
    if score >= 1:
        return "critical", "Baja"
    return "critical", "Baja"


def _format_dt(value: datetime | None) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else "--"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clip(text: str | None, limit: int = 180) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _attachment_kind(filename: str, content_type: str | None, *, is_pdf: bool = False, is_image: bool = False, is_audio: bool = False) -> str:
    lowered = (filename or "").lower()
    content_type = (content_type or "").lower()
    if is_pdf or content_type == "application/pdf" or lowered.endswith(".pdf"):
        return "pdf"
    if is_image or content_type.startswith("image/"):
        return "image"
    if is_audio or content_type.startswith("audio/"):
        return "audio"
    if lowered.endswith(".csv") or content_type in {"text/csv", "application/csv"}:
        return "csv"
    if lowered.endswith((".xls", ".xlsx", ".ods")) or content_type in {
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
    }:
        return "sheet"
    if content_type.startswith("text/") or lowered.endswith((".txt", ".log", ".md", ".json", ".eml")):
        return "text"
    if lowered.endswith((".doc", ".docx")):
        return "doc"
    return "unsupported"


def _parse_score(value: str | None) -> float | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _channel_date_cutoff(date_range: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if date_range == "today":
        return datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    if date_range == "7d":
        return now - timedelta(days=7)
    if date_range == "30d":
        return now - timedelta(days=30)
    return None


def _email_status_key(row: dict) -> str:
    raw_status = (row.get("raw_status") or "").strip()
    if raw_status in CHANNEL_EMAIL_STATUS_MAP:
        return CHANNEL_EMAIL_STATUS_MAP[raw_status]
    email_status = (row.get("email_status") or "").strip()
    if email_status == "pending":
        return "not_processed"
    if email_status == "descartado":
        return "discarded"
    if email_status.startswith("error"):
        return "error"
    if row.get("order_id"):
        return "order_detected"
    if row.get("detected_type") == "no_pedido":
        return "no_order"
    if row.get("detected_type") == "dudoso":
        return "doubtful"
    return "processed"


def _inbound_status_key(row: dict) -> str:
    raw_status = (row.get("raw_status") or "").strip()
    if raw_status in CHANNEL_INBOUND_STATUS_MAP:
        return CHANNEL_INBOUND_STATUS_MAP[raw_status]
    return "processed" if row.get("order_id") else "not_processed"


def _row_status_label(row: dict) -> str:
    return agent_status_label(row["status_key"])


def _row_score(row: dict) -> float | None:
    score = row.get("score")
    if score is not None:
        return score
    if row.get("kind") == "email":
        status_key = row.get("status_key")
        if status_key == "order_detected":
            return 93
        if status_key == "doubtful":
            return 62
        if status_key == "no_order":
            return 24
        if status_key == "error":
            return 0
    return score


def _row_origin(row: dict) -> str:
    if row["kind"] == "email":
        has_pdf = bool(row.get("has_pdf"))
        has_attachments = bool(row.get("has_attachments"))
        has_body = bool(row.get("content_text"))
        if has_pdf and has_body:
            return "PDF + Email"
        if has_pdf:
            return "PDF"
        if has_attachments:
            return "Adjunto"
        if has_body:
            return "Email"
        return "Sin adjunto"
    return row.get("channel_name") or "Entrada"


def _row_confidence(score: float | None) -> tuple[str, str]:
    return _score_bucket(score)


def _channel_union_subquery(company_id: int, *, email_channel_name: str) -> object:
    email_customer = aliased(Customer)
    email_validated_customer = aliased(Customer)
    inbound_customer = aliased(Customer)
    inbound_validated_customer = aliased(Customer)
    inbound_message_customer = aliased(Customer)
    input_channel = aliased(InputChannel)

    email_attachment_stats = (
        select(
            EmailAttachment.email_id.label("source_id"),
            func.count(EmailAttachment.id).label("attachment_count"),
            func.coalesce(
                func.max(
                    case(
                        (
                            or_(
                                EmailAttachment.is_pdf.is_(True),
                                func.lower(EmailAttachment.content_type).like("application/pdf"),
                                func.lower(EmailAttachment.filename).like("%.pdf"),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("has_pdf_flag"),
        )
        .where(EmailAttachment.company_id == company_id)
        .group_by(EmailAttachment.email_id)
        .subquery()
    )
    inbound_attachment_stats = (
        select(
            MessageAttachment.inbound_message_id.label("source_id"),
            func.count(MessageAttachment.id).label("attachment_count"),
            func.coalesce(
                func.max(
                    case(
                        (
                            or_(
                                MessageAttachment.is_pdf.is_(True),
                                func.lower(MessageAttachment.content_type).like("application/pdf"),
                                func.lower(MessageAttachment.filename).like("%.pdf"),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("has_pdf_flag"),
        )
        .where(MessageAttachment.company_id == company_id)
        .group_by(MessageAttachment.inbound_message_id)
        .subquery()
    )

    email_orders = (
        select(
            literal("email").label("kind"),
            literal("email").label("channel_key"),
            literal(email_channel_name or "Email").label("channel_name"),
            Email.id.label("source_id"),
            Email.id.label("email_id"),
            literal(None).label("message_id"),
            Email.received_at.label("received_at"),
            Email.sender.label("sender"),
            literal("").label("recipient"),
            Email.subject.label("subject"),
            Email.conversation_id.label("conversation_id"),
            func.coalesce(Email.body, Email.extracted_text, literal("")).label("content_text"),
            Email.agent_status.label("raw_status"),
            Email.status.label("email_status"),
            Email.detected_type.label("detected_type"),
            Order.id.label("order_id"),
            func.coalesce(email_validated_customer.id, email_customer.id, Order.customer_id).label("customer_id"),
            func.coalesce(
                email_validated_customer.fiscal_name,
                email_customer.fiscal_name,
                Order.customer_detected_name,
                literal("Cliente no identificado"),
            ).label("customer_name"),
            Order.score.label("score"),
            Order.status.label("order_status"),
            Order.customer_identification_method.label("customer_identification_method"),
            func.coalesce(email_attachment_stats.c.attachment_count, 0).label("attachment_count"),
            func.coalesce(email_attachment_stats.c.has_pdf_flag, 0).label("has_pdf_flag"),
            case(
                (
                    or_(Order.id.is_not(None), Email.agent_status.in_(("processed_order_detected", "processed_doubtful", "processed_no_order", "processed"))),
                    1,
                ),
                else_=0,
            ).label("processed_flag"),
            case(
                (
                    or_(
                        Email.agent_status.in_(("not_processed", "queued", "processing", "pending_reprocess")),
                        Email.status == "pending",
                    ),
                    1,
                ),
                else_=0,
            ).label("pending_flag"),
            case(
                (
                    or_(
                        Email.agent_status.in_(("processed_doubtful", "processed_no_order")),
                        and_(Order.score.is_not(None), Order.score < 75),
                    ),
                    1,
                ),
                else_=0,
            ).label("review_flag"),
            case(
                (
                    or_(Email.agent_status == "processing_error", Email.status.startswith("error")),
                    1,
                ),
                else_=0,
            ).label("error_flag"),
        )
        .select_from(Email)
        .outerjoin(Order, and_(Order.company_id == company_id, Order.email_id == Email.id))
        .outerjoin(email_validated_customer, Order.validated_customer_id == email_validated_customer.id)
        .outerjoin(email_customer, Order.customer_id == email_customer.id)
        .outerjoin(email_attachment_stats, email_attachment_stats.c.source_id == Email.id)
        .where(Email.company_id == company_id)
    )

    inbound_rows = (
        select(
            literal("inbound").label("kind"),
            func.coalesce(input_channel.key, literal("message")).label("channel_key"),
            func.coalesce(input_channel.name, literal("Entrada")).label("channel_name"),
            InboundMessage.id.label("source_id"),
            literal(None).label("email_id"),
            InboundMessage.id.label("message_id"),
            InboundMessage.received_at.label("received_at"),
            func.coalesce(InboundMessage.sender, literal("")).label("sender"),
            func.coalesce(InboundMessage.recipient, literal("")).label("recipient"),
            func.coalesce(InboundMessage.subject, literal("Sin asunto")).label("subject"),
            InboundMessage.conversation_id.label("conversation_id"),
            func.coalesce(InboundMessage.original_content, InboundMessage.normalized_text, InboundMessage.extraction_json, literal("")).label("content_text"),
            InboundMessage.status.label("raw_status"),
            InboundMessage.status.label("email_status"),
            InboundMessage.detected_type.label("detected_type"),
            func.coalesce(Order.id, InboundMessage.order_id).label("order_id"),
            func.coalesce(inbound_validated_customer.id, inbound_customer.id, inbound_message_customer.id, InboundMessage.customer_id).label("customer_id"),
            func.coalesce(
                inbound_validated_customer.fiscal_name,
                inbound_customer.fiscal_name,
                inbound_message_customer.fiscal_name,
                literal("Cliente no identificado"),
            ).label("customer_name"),
            InboundMessage.score.label("score"),
            Order.status.label("order_status"),
            Order.customer_identification_method.label("customer_identification_method"),
            func.coalesce(inbound_attachment_stats.c.attachment_count, 0).label("attachment_count"),
            func.coalesce(inbound_attachment_stats.c.has_pdf_flag, 0).label("has_pdf_flag"),
            case(
                (
                    or_(InboundMessage.order_id.is_not(None), InboundMessage.status.in_(("processed", "matched", "order_detected"))),
                    1,
                ),
                else_=0,
            ).label("processed_flag"),
            case(
                (
                    InboundMessage.status.in_(("received", "queued", "processing")),
                    1,
                ),
                else_=0,
            ).label("pending_flag"),
            case(
                (
                    or_(
                        InboundMessage.status.in_(("doubtful", "no_order")),
                        and_(InboundMessage.score.is_not(None), InboundMessage.score < 75),
                    ),
                    1,
                ),
                else_=0,
            ).label("review_flag"),
            case((InboundMessage.status == "error", 1), else_=0).label("error_flag"),
        )
        .select_from(InboundMessage)
        .outerjoin(input_channel, and_(input_channel.id == InboundMessage.channel_id, input_channel.company_id == company_id))
        .outerjoin(Order, and_(Order.company_id == company_id, Order.id == InboundMessage.order_id))
        .outerjoin(inbound_validated_customer, Order.validated_customer_id == inbound_validated_customer.id)
        .outerjoin(inbound_customer, Order.customer_id == inbound_customer.id)
        .outerjoin(inbound_message_customer, InboundMessage.customer_id == inbound_message_customer.id)
        .outerjoin(inbound_attachment_stats, inbound_attachment_stats.c.source_id == InboundMessage.id)
        .where(InboundMessage.company_id == company_id)
    )

    return union_all(email_orders, inbound_rows).subquery("channels_base")


def _channel_filters(base, *, tab: str, date_range: str, search: str, customer_id: int, score_min: float | None, score_max: float | None):
    clauses = []
    if tab != "all":
        if tab in {"email", "whatsapp", "voice", "social"}:
            clauses.append(base.c.channel_key == tab)
        elif tab == "pending":
            clauses.append(base.c.pending_flag == 1)
        elif tab == "processed":
            clauses.append(base.c.processed_flag == 1)
        elif tab == "review":
            clauses.append(base.c.review_flag == 1)
        elif tab == "error":
            clauses.append(base.c.error_flag == 1)

    cutoff = _channel_date_cutoff(date_range) if date_range and date_range != "all" else None
    if cutoff is not None:
        clauses.append(base.c.received_at >= cutoff)

    if customer_id:
        clauses.append(base.c.customer_id == customer_id)

    if search:
        needle = f"%{search.lower().strip()}%"
        clauses.append(
            or_(
                func.lower(func.coalesce(base.c.customer_name, literal(""))).like(needle),
                func.lower(func.coalesce(base.c.sender, literal(""))).like(needle),
                func.lower(func.coalesce(base.c.subject, literal(""))).like(needle),
                func.lower(func.coalesce(base.c.content_text, literal(""))).like(needle),
            )
        )

    if score_min is not None:
        clauses.append(base.c.score.is_not(None))
        clauses.append(base.c.score >= score_min)
    if score_max is not None:
        clauses.append(base.c.score.is_not(None))
        clauses.append(base.c.score <= score_max)
    return clauses


def _channel_summary(db: Session, base, *, tab: str, date_range: str, search: str, customer_id: int, score_min: float | None, score_max: float | None) -> dict:
    filtered = select(base).where(*_channel_filters(base, tab=tab, date_range=date_range, search=search, customer_id=customer_id, score_min=score_min, score_max=score_max)).subquery("channels_filtered")
    summary_row = db.execute(
        select(
            func.count().label("total"),
            func.sum(case((filtered.c.kind == "email", 1), else_=0)).label("emails"),
            func.sum(case((filtered.c.kind == "inbound", 1), else_=0)).label("inbound"),
            func.sum(case((filtered.c.processed_flag == 1, 1), else_=0)).label("processed"),
            func.sum(case((filtered.c.pending_flag == 1, 1), else_=0)).label("pending"),
            func.sum(case((filtered.c.review_flag == 1, 1), else_=0)).label("review"),
            func.sum(case((filtered.c.error_flag == 1, 1), else_=0)).label("error"),
            func.sum(case((filtered.c.order_id.is_not(None), 1), else_=0)).label("with_order"),
            func.sum(case((filtered.c.channel_key == "email", 1), else_=0)).label("email_count"),
            func.sum(case((filtered.c.channel_key == "whatsapp", 1), else_=0)).label("whatsapp_count"),
            func.sum(case((filtered.c.channel_key == "voice", 1), else_=0)).label("voice_count"),
            func.sum(case((filtered.c.channel_key == "social", 1), else_=0)).label("social_count"),
        ).select_from(filtered)
    ).one()
    return {
        "total": int(summary_row.total or 0),
        "emails": int(summary_row.emails or 0),
        "inbound": int(summary_row.inbound or 0),
        "processed": int(summary_row.processed or 0),
        "pending": int(summary_row.pending or 0),
        "review": int(summary_row.review or 0),
        "error": int(summary_row.error or 0),
        "with_order": int(summary_row.with_order or 0),
        "channel_counts": {
            "email": int(summary_row.email_count or 0),
            "whatsapp": int(summary_row.whatsapp_count or 0),
            "voice": int(summary_row.voice_count or 0),
            "social": int(summary_row.social_count or 0),
        },
    }


def _attachment_rows(db: Session, *, company_id: int, source_kind: str, source_ids: list[int]) -> dict[int, list[dict]]:
    if not source_ids:
        return {}
    if source_kind == "email":
        rows = db.execute(
            select(
                EmailAttachment.id,
                EmailAttachment.email_id.label("source_id"),
                EmailAttachment.filename,
                EmailAttachment.content_type,
                EmailAttachment.size_bytes,
                EmailAttachment.storage_path,
                EmailAttachment.extracted_text,
                EmailAttachment.is_pdf,
            ).where(EmailAttachment.company_id == company_id, EmailAttachment.email_id.in_(source_ids)).order_by(EmailAttachment.email_id, EmailAttachment.id)
        ).mappings().all()
    else:
        rows = db.execute(
            select(
                MessageAttachment.id,
                MessageAttachment.inbound_message_id.label("source_id"),
                MessageAttachment.filename,
                MessageAttachment.content_type,
                MessageAttachment.size_bytes,
                MessageAttachment.storage_path,
                MessageAttachment.extracted_text,
                MessageAttachment.ocr_text,
                MessageAttachment.transcription_text,
                MessageAttachment.is_pdf,
                MessageAttachment.is_image,
                MessageAttachment.is_audio,
            ).where(MessageAttachment.company_id == company_id, MessageAttachment.inbound_message_id.in_(source_ids)).order_by(MessageAttachment.inbound_message_id, MessageAttachment.id)
        ).mappings().all()
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        filename = row["filename"] or "Adjunto"
        content_type = row.get("content_type")
        attachment = {
            "id": row["id"],
            "filename": filename,
            "content_type": content_type,
            "size_bytes": row.get("size_bytes") or 0,
            "is_pdf": bool(row.get("is_pdf")),
            "is_image": bool(row.get("is_image")),
            "is_audio": bool(row.get("is_audio")),
            "extracted_text": row.get("extracted_text") or "",
            "ocr_text": row.get("ocr_text") or "",
            "transcription_text": row.get("transcription_text") or "",
        }
        grouped[int(row["source_id"])].append(attachment)
    return grouped


def _channel_item_from_row(row: dict, attachments: list[dict], *, email_channel_name: str) -> dict:
    score = _row_score(row)
    confidence_key, confidence_label = _row_confidence(score)
    status_key = _email_status_key(row) if row["kind"] == "email" else _inbound_status_key(row)
    row["status_key"] = status_key
    status_label = _row_status_label(row)
    content_text = row.get("content_text") or row.get("subject") or ""
    attachment_count = int(row.get("attachment_count") or 0)
    has_attachments = bool(attachment_count or row.get("has_attachments") or attachments)
    has_pdf = bool(row.get("has_pdf_flag") or row.get("has_pdf") or any(att.get("is_pdf") for att in attachments))
    channel_name = row.get("channel_name") or (email_channel_name if row["kind"] == "email" else "Entrada")
    customer_name = row.get("customer_name") or "Cliente no identificado"
    received_at = row.get("received_at")
    source_id = row.get("source_id")
    item = {
        "kind": row["kind"],
        "channel_key": row.get("channel_key") or ("email" if row["kind"] == "email" else "message"),
        "channel_name": channel_name,
        "entry_id": f"{row['kind']}-{source_id}",
        "email_id": row.get("email_id"),
        "message_id": row.get("message_id"),
        "conversation_id": row.get("conversation_id"),
        "order_id": row.get("order_id"),
        "customer_id": row.get("customer_id"),
        "customer_name": customer_name,
        "sender": row.get("sender") or "",
        "recipient": row.get("recipient") or "",
        "subject": row.get("subject") or "Sin asunto",
        "content_text": content_text,
        "summary": _clip(content_text or row.get("subject") or "Entrada sin contenido"),
        "received_at": _aware(received_at),
        "received_label": _format_dt(received_at),
        "status_key": status_key,
        "status_label": status_label,
        "score": score,
        "confidence_key": confidence_key,
        "confidence_label": confidence_label,
        "origin": _row_origin(row),
        "source_label": "Correo" if row["kind"] == "email" else channel_name,
        "has_pdf": has_pdf,
        "has_attachments": has_attachments,
        "attachment_count": attachment_count,
        "attachments": attachments,
        "detail_href": f"/channels?focus={row['kind']}-{source_id}",
        "order_href": f"/orders/{row['order_id']}" if row.get("order_id") else "",
        "preview_href": f"/channels/{row['kind']}/{source_id}/attachments/{attachments[0]['id']}/preview" if attachments else "",
        "download_href": f"/channels/{row['kind']}/{source_id}/attachments/{attachments[0]['id']}" if attachments else "",
    }
    return item


def _paginate(total_items: int, page: int, page_size: int) -> dict:
    page, page_size = normalize_page(page, page_size)
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    if total_pages and page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    return {
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
        "start_item": offset + 1 if total_items else 0,
        "end_item": min(offset + page_size, total_items),
        "allowed_page_sizes": (10, 25, 50, 100),
        "offset": offset,
    }


@router.get("")
def channels_page(
    request: Request,
    tab: str = "all",
    date_range: str = "all",
    customer_id: int = 0,
    search: str = "",
    score_min: str | None = "",
    score_max: str | None = "",
    page: int = 1,
    page_size: int = 12,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    settings = get_or_create_settings(db, EmailSettings, user.company_id)
    query_params = dict(request.query_params)
    score_min_value = _parse_score(score_min)
    score_max_value = _parse_score(score_max)
    if "date_range" not in query_params and settings.default_date_range:
        date_range = settings.default_date_range
    if "page_size" not in query_params and settings.default_page_size:
        page_size = settings.default_page_size
    channel_rows = db.execute(select(InputChannel.key, InputChannel.name).where(InputChannel.company_id == user.company_id)).mappings().all()
    channels_by_key = {row["key"]: row["name"] for row in channel_rows}
    base = _channel_union_subquery(user.company_id, email_channel_name=channels_by_key.get("email", "Email"))
    summary = _channel_summary(
        db,
        base,
        tab=tab,
        date_range=date_range,
        search=search,
        customer_id=customer_id,
        score_min=score_min_value,
        score_max=score_max_value,
    )
    filtered = select(base).where(*_channel_filters(base, tab=tab, date_range=date_range, search=search, customer_id=customer_id, score_min=score_min_value, score_max=score_max_value)).subquery("channels_filtered")
    pagination = _paginate(summary["total"], page, page_size)
    visible_raw_rows = db.execute(
        select(filtered)
        .order_by(filtered.c.received_at.desc(), filtered.c.source_id.desc())
        .limit(pagination["page_size"])
        .offset(pagination["offset"])
    ).mappings().all()
    email_ids = [int(row["source_id"]) for row in visible_raw_rows if row["kind"] == "email" and row["source_id"] is not None]
    message_ids = [int(row["source_id"]) for row in visible_raw_rows if row["kind"] == "inbound" and row["source_id"] is not None]
    email_attachment_map = _attachment_rows(db, company_id=user.company_id, source_kind="email", source_ids=email_ids)
    message_attachment_map = _attachment_rows(db, company_id=user.company_id, source_kind="inbound", source_ids=message_ids)
    visible_rows = []
    for raw_row in visible_raw_rows:
        row = dict(raw_row)
        attachments = email_attachment_map.get(int(row["source_id"]), []) if row["kind"] == "email" else message_attachment_map.get(int(row["source_id"]), [])
        visible_rows.append(_channel_item_from_row(row, attachments, email_channel_name=channels_by_key.get("email", "Email")))
    customer_rows = tuple(
        db.execute(
            select(Customer.id, Customer.code, Customer.fiscal_name)
            .where(Customer.company_id == user.company_id)
            .order_by(Customer.fiscal_name.asc())
        ).mappings().all()
    )
    tabs = tuple(
        [
            ("all", "Todos", summary["total"]),
            ("email", "Email", summary["channel_counts"].get("email", 0)),
            ("whatsapp", "WhatsApp", summary["channel_counts"].get("whatsapp", 0)),
            ("voice", "Voz", summary["channel_counts"].get("voice", 0)),
            ("social", "Redes", summary["channel_counts"].get("social", 0)),
            ("pending", "Pendientes", summary["pending"]),
            ("processed", "Procesados", summary["processed"]),
            ("review", "Revisión", summary["review"]),
            ("error", "Errores", summary["error"]),
        ]
    )
    focus_key = request.query_params.get("focus", "")
    return templates.TemplateResponse(
        "channels/list.html",
        {
            "request": request,
            "user": user,
            "channels": visible_rows,
            "summary": summary,
            "tabs": tabs,
            "filters": {
                "tab": tab,
                "date_range": date_range,
                "customer_id": customer_id,
                "search": search,
                "score_min": score_min_value if score_min_value is not None else "",
                "score_max": score_max_value if score_max_value is not None else "",
            },
            "customers": customer_rows,
            "pagination": pagination,
            "focus_key": focus_key,
            "email_settings": settings,
        },
    )


def _attachment_path(attachment) -> Path:
    return Path(attachment.storage_path or "")


def _preview_html(title: str, body: str, download_href: str) -> str:
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:12px;background:#f7f9f8;color:#172026;font-size:12px;line-height:1.35}}
.wrap{{display:grid;gap:10px}}
.head{{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:10px 11px;border:1px solid #dbe2e6;border-radius:8px;background:#fff}}
.head strong{{font-size:14px}}
.muted{{color:#63717b}}
.table-wrap{{overflow:auto;border:1px solid #dbe2e6;border-radius:8px;background:#fff}}
table{{border-collapse:collapse;width:100%;font-size:11px}}
th,td{{padding:6px 8px;border-bottom:1px solid #dbe2e6;text-align:left;vertical-align:top}}
th{{position:sticky;top:0;background:#eef3f4}}
pre{{margin:0;white-space:pre-wrap;word-break:break-word;padding:10px 11px;border:1px solid #dbe2e6;border-radius:8px;background:#fff}}
a.button{{display:inline-flex;align-items:center;justify-content:center;padding:6px 10px;border-radius:6px;background:#0f766e;color:#fff;text-decoration:none;font-weight:700}}
</style></head>
<body><div class="wrap">
<div class="head"><div><strong>{escape(title)}</strong><div class="muted">Vista rápida integrada</div></div><a class="button" href="{escape(download_href)}" target="_blank">Descargar</a></div>
{body}
</div></body></html>"""


def _read_csv_preview(path: Path) -> str:
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            frame = pd.read_csv(path, dtype=str, nrows=12, encoding=encoding).fillna("")
            return frame.to_html(index=False, escape=True)
        except Exception:
            continue
    raise ValueError("CSV no legible")


def _read_sheet_preview(path: Path) -> str:
    frame = pd.read_excel(path, dtype=str, nrows=12).fillna("")
    if frame.empty:
        return "<p class='muted'>La hoja está vacía.</p>"
    return frame.to_html(index=False, escape=True)


@router.get("/{source_kind}/{source_id}/attachments/{attachment_id}/preview")
def preview_attachment(source_kind: str, source_id: int, attachment_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    attachment = None
    title = "Adjunto"
    download_href = f"/channels/{source_kind}/{source_id}/attachments/{attachment_id}"
    if source_kind == "email":
        source = db.get(Email, source_id)
        attachment = db.get(EmailAttachment, attachment_id)
        title = attachment.filename if attachment else title
        if not source or not attachment or source.company_id != user.company_id or attachment.email_id != source.id:
            return PlainTextResponse("No encontrado", status_code=404)
    else:
        source = db.get(InboundMessage, source_id)
        attachment = db.get(MessageAttachment, attachment_id)
        title = attachment.filename if attachment else title
        if not source or not attachment or source.company_id != user.company_id or attachment.inbound_message_id != source.id:
            return PlainTextResponse("No encontrado", status_code=404)
    path = _attachment_path(attachment)
    if not path.exists() or not path.is_file():
        return HTMLResponse(_preview_html(title, "<p class='muted'>No se puede previsualizar este archivo.</p>", download_href), status_code=404)

    kind = _attachment_kind(attachment.filename, attachment.content_type, is_pdf=getattr(attachment, "is_pdf", False), is_image=getattr(attachment, "is_image", False), is_audio=getattr(attachment, "is_audio", False))
    media_type = attachment.content_type or "application/octet-stream"
    if kind in {"pdf", "image", "audio"}:
        return FileResponse(path, media_type=media_type, headers={"Content-Disposition": f'inline; filename="{attachment.filename}"'})
    if kind == "csv":
        try:
            table_html = _read_csv_preview(path)
            return HTMLResponse(_preview_html(title, f"<div class='table-wrap'>{table_html}</div>", download_href))
        except Exception:
            pass
    if kind == "sheet":
        try:
            table_html = _read_sheet_preview(path)
            return HTMLResponse(_preview_html(title, f"<div class='table-wrap'>{table_html}</div>", download_href))
        except Exception:
            pass
    text = attachment.extracted_text or attachment.ocr_text or attachment.transcription_text
    if text is None and kind == "text":
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            try:
                text = path.read_text(encoding="latin-1")
            except Exception:
                text = ""
    if text:
        return HTMLResponse(_preview_html(title, f"<pre>{escape(text[:12000])}</pre>", download_href))
    return HTMLResponse(_preview_html(title, "<p class='muted'>No se puede previsualizar este archivo.</p>", download_href))


@router.get("/{source_kind}/{source_id}/attachments/{attachment_id}")
def download_attachment(source_kind: str, source_id: int, attachment_id: int, db: Session = Depends(get_tenant_db), user: TenantUser = Depends(current_user)):
    attachment = None
    if source_kind == "email":
        source = db.get(Email, source_id)
        attachment = db.get(EmailAttachment, attachment_id)
        if not source or not attachment or source.company_id != user.company_id or attachment.email_id != source.id:
            return PlainTextResponse("No encontrado", status_code=404)
    else:
        source = db.get(InboundMessage, source_id)
        attachment = db.get(MessageAttachment, attachment_id)
        if not source or not attachment or source.company_id != user.company_id or attachment.inbound_message_id != source.id:
            return PlainTextResponse("No encontrado", status_code=404)
    path = _attachment_path(attachment)
    if not path.exists() or not path.is_file():
        return PlainTextResponse("Archivo no disponible", status_code=404)
    return FileResponse(path, media_type=attachment.content_type or "application/octet-stream", filename=attachment.filename)


@router.get("/legacy")
def legacy_mail_redirect(request: Request):
    query = request.url.query
    suffix = f"?{query}" if query else ""
    return RedirectResponse(f"/channels{suffix}", status_code=307)
