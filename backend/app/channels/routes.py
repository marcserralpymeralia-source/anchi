from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import current_user
from app.master.service import TenantUser
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.dashboard.service import agent_status_label, email_agent_status, email_has_pdf, email_origin
from app.db.models import Customer, Email, EmailAttachment, EmailSettings, InboundMessage, InputChannel, MessageAttachment, Order, User
from app.settings.service import get_or_create_settings
from app.tenancy.database import get_tenant_db

router = APIRouter(prefix="/channels", tags=["channels"])
CHANNELS_LIST_LIMIT = 250


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


def _is_processed_item(item: dict) -> bool:
    return item["status_key"] in {"processed", "order_detected"} or bool(item.get("order_id"))


def _build_email_item(email: Email, order: Order | None, inbound: InboundMessage | None, channel_name: str) -> dict:
    channel_key = "email"
    score = order.score if order and order.score is not None else None
    if score is None:
        if email.agent_status == "processed_order_detected":
            score = 93
        elif email.agent_status == "processed_doubtful":
            score = 62
        elif email.agent_status == "processed_no_order":
            score = 24
        elif email.agent_status == "processing_error":
            score = 0
    confidence_key, confidence_label = _score_bucket(score)
    status_key = email_agent_status(email, order)
    status_label = agent_status_label(status_key)
    subject = email.subject or (inbound.subject if inbound else "")
    body = email.body or (inbound.original_content if inbound else "") or (email.extracted_text or "")
    customer_name = (order.validated_customer or order.customer).fiscal_name if order and (order.validated_customer or order.customer) else (order.customer_detected_name if order else "")
    customer_id = (order.validated_customer_id or order.customer_id) if order else (inbound.customer_id if inbound else None)
    attachments = list(email.attachments or [])
    return {
        "kind": "email",
        "channel_key": channel_key,
        "channel_name": channel_name,
        "entry_id": f"email-{email.id}",
        "email_id": email.id,
        "message_id": inbound.id if inbound else None,
        "order_id": order.id if order else None,
        "customer_id": customer_id,
        "customer_name": customer_name or "Cliente no identificado",
        "sender": email.sender,
        "recipient": inbound.recipient if inbound else "",
        "subject": subject or "Sin asunto",
        "content_text": body or subject or "",
        "summary": _clip(body or subject or "Entrada sin contenido"),
        "received_at": email.received_at,
        "received_label": _format_dt(email.received_at),
        "status_key": status_key,
        "status_label": status_label,
        "score": score,
        "confidence_key": confidence_key,
        "confidence_label": confidence_label,
        "origin": email_origin(email),
        "source_label": "Correo",
        "has_pdf": email_has_pdf(email),
        "has_attachments": bool(email.attachments),
        "attachments": attachments,
        "detail_href": f"/channels?focus=email-{email.id}",
        "order_href": f"/orders/{order.id}" if order else "",
        "preview_href": f"/channels/email/{email.id}/preview",
        "download_href": f"/channels/email/{email.id}/download",
    }


def _build_inbound_item(message: InboundMessage, order: Order | None, channel: InputChannel | None) -> dict:
    score = message.score if message.score is not None else None
    confidence_key, confidence_label = _score_bucket(score)
    status_map = {
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
    status_key = status_map.get(message.status, "processed" if order else "not_processed")
    status_label = agent_status_label(status_key)
    attachments = list(message.attachments or [])
    body = message.original_content or message.normalized_text or message.extraction_json or ""
    customer_name = (order.validated_customer or order.customer).fiscal_name if order and (order.validated_customer or order.customer) else ""
    channel_key = channel.key if channel else "message"
    channel_name = channel.name if channel else "Entrada"
    return {
        "kind": "inbound",
        "channel_key": channel_key,
        "channel_name": channel_name,
        "entry_id": f"inbound-{message.id}",
        "message_id": message.id,
        "email_id": None,
        "order_id": order.id if order else message.order_id,
        "customer_id": message.customer_id or (order.customer_id if order else None),
        "customer_name": customer_name or "Cliente no identificado",
        "sender": message.sender or "",
        "recipient": message.recipient or "",
        "subject": message.subject or "Sin asunto",
        "content_text": body or message.subject or "",
        "summary": _clip(body or message.subject or "Entrada sin contenido"),
        "received_at": message.received_at,
        "received_label": _format_dt(message.received_at),
        "status_key": status_key,
        "status_label": status_label,
        "score": score,
        "confidence_key": confidence_key,
        "confidence_label": confidence_label,
        "origin": channel_name,
        "source_label": channel_name,
        "has_pdf": bool(message.has_pdf or any(att.is_pdf for att in attachments)),
        "has_attachments": bool(attachments),
        "attachments": attachments,
        "detail_href": f"/channels?focus=inbound-{message.id}",
        "order_href": f"/orders/{order.id}" if order else "",
        "preview_href": f"/channels/message/{message.id}/preview",
        "download_href": f"/channels/message/{message.id}/download",
    }


def _build_rows(db: Session, company_id: int, *, limit: int = CHANNELS_LIST_LIMIT) -> list[dict]:
    channels = db.scalars(select(InputChannel).where(InputChannel.company_id == company_id)).all()
    channels_by_key = {channel.key: channel for channel in channels}
    channels_by_id = {channel.id: channel for channel in channels}

    emails = db.scalars(
        select(Email)
        .where(Email.company_id == company_id)
        .options(selectinload(Email.attachments))
        .order_by(Email.received_at.desc())
        .limit(limit)
    ).all()
    inbounds = db.scalars(
        select(InboundMessage)
        .where(InboundMessage.company_id == company_id)
        .options(selectinload(InboundMessage.attachments))
        .order_by(InboundMessage.received_at.desc())
        .limit(limit)
    ).all()

    orders_by_email = {}
    email_ids = [email.id for email in emails]
    if email_ids:
        for order in db.scalars(select(Order).where(Order.company_id == company_id, Order.email_id.in_(email_ids))).all():
            orders_by_email[order.email_id] = order

    order_ids = [message.order_id for message in inbounds if message.order_id]
    orders_by_id = {}
    if order_ids:
        for order in db.scalars(select(Order).where(Order.company_id == company_id, Order.id.in_(order_ids))).all():
            orders_by_id[order.id] = order

    inbound_by_external = {message.source_external_id: message for message in inbounds if message.source_external_id}
    seen_inbound_ids: set[int] = set()

    rows: list[dict] = []
    for email in emails:
        inbound = inbound_by_external.get(email.external_id or "")
        if inbound:
            seen_inbound_ids.add(inbound.id)
        order = orders_by_email.get(email.id)
        rows.append(_build_email_item(email, order, inbound, channels_by_key.get("email").name if channels_by_key.get("email") else "Email"))
    for message in inbounds:
        if message.id in seen_inbound_ids:
            continue
        channel = channels_by_id.get(message.channel_id)
        order = orders_by_id.get(message.order_id) if message.order_id else None
        rows.append(_build_inbound_item(message, order, channel))

    return rows


def _filter_rows(rows: list[dict], *, tab: str, date_range: str, search: str, customer_id: int, score_min: float | None, score_max: float | None) -> list[dict]:
    now = datetime.now(timezone.utc)
    filtered = rows

    if tab != "all":
        if tab in {"email", "whatsapp", "voice", "social"}:
            filtered = [row for row in filtered if row["channel_key"] == tab]
        elif tab == "pending":
            filtered = [row for row in filtered if row["status_key"] in {"not_processed", "queued", "processing", "pending_reprocess"}]
        elif tab == "processed":
            filtered = [row for row in filtered if _is_processed_item(row)]
        elif tab == "review":
            filtered = [row for row in filtered if row["status_key"] in {"doubtful", "no_order"} or (row["confidence_key"] in {"amber", "low", "critical"} and row["score"] is not None)]
        elif tab == "error":
            filtered = [row for row in filtered if row["status_key"] == "error"]

    if date_range and date_range != "all":
        if date_range == "today":
            cutoff = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
        elif date_range == "7d":
            cutoff = now - timedelta(days=7)
        elif date_range == "30d":
            cutoff = now - timedelta(days=30)
        else:
            cutoff = None
        if cutoff:
            filtered = [row for row in filtered if row["received_at"] >= cutoff]

    if customer_id:
        filtered = [row for row in filtered if row["customer_id"] == customer_id]

    if search:
        needle = search.lower().strip()
        filtered = [
            row
            for row in filtered
            if needle in (row["customer_name"] or "").lower()
            or needle in (row["sender"] or "").lower()
            or needle in (row["subject"] or "").lower()
            or needle in (row["summary"] or "").lower()
        ]

    if score_min is not None:
        filtered = [row for row in filtered if row["score"] is not None and row["score"] >= score_min]
    if score_max is not None:
        filtered = [row for row in filtered if row["score"] is not None and row["score"] <= score_max]

    return filtered


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


def _build_summary(rows: list[dict]) -> dict:
    channel_counts: dict[str, int] = {}
    for row in rows:
        channel_counts[row["channel_key"]] = channel_counts.get(row["channel_key"], 0) + 1
    return {
        "total": len(rows),
        "emails": sum(1 for row in rows if row["kind"] == "email"),
        "inbound": sum(1 for row in rows if row["kind"] == "inbound"),
        "processed": sum(1 for row in rows if _is_processed_item(row)),
        "pending": sum(1 for row in rows if row["status_key"] in {"not_processed", "queued", "processing", "pending_reprocess"}),
        "review": sum(1 for row in rows if row["status_key"] in {"doubtful", "no_order"}),
        "error": sum(1 for row in rows if row["status_key"] == "error"),
        "with_order": sum(1 for row in rows if row["order_id"]),
        "channel_counts": channel_counts,
    }


def _paginate_rows(rows: list[dict], page: int, page_size: int) -> tuple[list[dict], dict]:
    page, page_size = normalize_page(page, page_size)
    total_items = len(rows)
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    if total_pages and page > total_pages:
        page = total_pages
    offset = (page - 1) * page_size
    items = rows[offset : offset + page_size]
    return items, {
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1,
        "start_item": offset + 1 if total_items else 0,
        "end_item": min(offset + page_size, total_items),
        "allowed_page_sizes": (10, 25, 50, 100),
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
    rows = _build_rows(db, user.company_id)
    filtered_rows = _filter_rows(
        rows,
        tab=tab,
        date_range=date_range,
        search=search,
        customer_id=customer_id,
        score_min=score_min_value,
        score_max=score_max_value,
    )
    filtered_rows = sorted(filtered_rows, key=lambda row: row["received_at"], reverse=True)
    visible_rows, pagination = _paginate_rows(filtered_rows, page, page_size)
    customer_rows = db.scalars(select(Customer).where(Customer.company_id == user.company_id).order_by(Customer.fiscal_name.asc())).all()
    counts = _build_summary(rows)
    tabs = [
        ("all", "Todos", counts["total"]),
        ("email", "Email", counts["channel_counts"].get("email", 0)),
        ("whatsapp", "WhatsApp", counts["channel_counts"].get("whatsapp", 0)),
        ("voice", "Voz", counts["channel_counts"].get("voice", 0)),
        ("social", "Redes", counts["channel_counts"].get("social", 0)),
        ("pending", "Pendientes", counts["pending"]),
        ("processed", "Procesados", counts["processed"]),
        ("review", "Revisión", counts["review"]),
        ("error", "Errores", counts["error"]),
    ]
    focus_key = request.query_params.get("focus", "")
    return templates.TemplateResponse(
        "channels/list.html",
        {
            "request": request,
            "user": user,
            "channels": visible_rows,
            "all_channels": rows,
            "summary": counts,
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
