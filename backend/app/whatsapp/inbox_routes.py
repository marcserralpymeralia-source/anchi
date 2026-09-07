import hashlib
import logging
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import current_user
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.db.models import Conversation, Customer, InboundMessage, InputChannel, MessageAttachment
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db
from app.whatsapp.service import (
    WHATSAPP_SUPPORTED_AUDIO_MIME_TYPES,
    WHATSAPP_SUPPORTED_AUDIO_EXTENSIONS,
    WHATSAPP_SUPPORTED_DOCUMENT_EXTENSIONS,
    WHATSAPP_SUPPORTED_DOCUMENT_MIME_TYPES,
    download_whatsapp_media,
    send_manual_response,
    whatsapp_config,
    whatsapp_outbound_is_ready,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["whatsapp-inbox"])


def _channel(db: Session, company_id: int) -> InputChannel | None:
    return db.scalar(
        select(InputChannel).where(
            InputChannel.company_id == company_id,
            InputChannel.key == "whatsapp",
            InputChannel.is_active.is_(True),
        )
    )


def _attachment_kind(filename: str | None, content_type: str | None, *, is_audio: bool = False) -> str:
    extension = Path(filename or "").suffix.lower()
    mime = (content_type or "").lower().split(";", 1)[0]
    if is_audio or mime.startswith("audio/") or extension in {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".aac"}:
        return "audio"
    if mime == "application/pdf" or extension == ".pdf":
        return "pdf"
    if extension in {".docx", ".doc"} or mime in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }:
        return "doc"
    if extension in {".xlsx", ".xls"} or "spreadsheet" in mime or "excel" in mime:
        return "sheet"
    if mime.startswith("text/") or extension == ".txt":
        return "text"
    if mime.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"}:
        return "image"
    return "file"


def _format_bytes(size: int | None) -> str:
    value = int(size or 0)
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def _attachment_status_label(status: str | None) -> str:
    labels = {
        "pending": "Esperando descarga desde Meta",
        "downloaded": "Descargado; pendiente de procesar",
        "transcription_pending": "Guardado; pendiente de transcribir",
        "extraction_error": "No se pudo descargar o procesar",
        "storage_error": "No se pudo guardar",
        "unsupported": "Tipo no compatible",
    }
    normalized = str(status or "pending").strip().lower()
    return labels.get(normalized, "Procesando archivo")


def _message_date(message: InboundMessage):
    return getattr(message, "sent_at", None) or message.received_at or message.created_at


def _message_payload(message: InboundMessage) -> dict:
    outbound = message.direction == "outbound"
    attachments = []
    for attachment in message.attachments or []:
        available = bool(attachment.storage_path)
        attachments.append(
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "content_type": attachment.content_type or "Archivo",
                "kind": _attachment_kind(attachment.filename, attachment.content_type, is_audio=bool(attachment.is_audio)),
                "size": _format_bytes(attachment.size_bytes),
                "available": available,
                "href": f"/channels/inbound/{message.id}/attachments/{attachment.id}/preview" if available else "",
                "download_href": f"/channels/inbound/{message.id}/attachments/{attachment.id}" if available else "",
                "status": attachment.extraction_status or "pending",
                "status_label": _attachment_status_label(attachment.extraction_status),
                "error": (attachment.extraction_error or "")[:240],
            }
        )
    return {
        "id": message.id,
        "direction": "outbound" if outbound else "inbound",
        "speaker": "Anchi" if outbound else (message.sender or "Contacto"),
        "text": message.original_content or "",
        "date": _message_date(message),
        "status": str(message.status or "").lower(),
        "attachments": attachments,
    }


def _conversation_card(conversation: Conversation, customers: dict[int, Customer]) -> dict:
    messages = sorted(
        conversation.messages or [],
        key=_message_date,
    )
    latest = messages[-1] if messages else None
    latest_inbound = next((message for message in reversed(messages) if message.direction == "inbound"), None)
    customer = customers.get(conversation.customer_id or 0)
    contact_name = (
        customer.commercial_name or customer.fiscal_name
        if customer
        else (latest_inbound.sender if latest_inbound else conversation.external_thread_id or "Contacto sin identificar")
    )
    contact_detail = latest_inbound.sender if latest_inbound and customer else conversation.external_thread_id or ""
    if contact_detail and contact_detail.strip() == str(contact_name).strip():
        contact_detail = ""
    preview = (latest.original_content or "") if latest else "Sin mensajes todavía"
    if not preview and latest and latest.attachments:
        preview = latest.attachments[0].filename or "Archivo adjunto"
    unread = any(
        message.direction == "inbound" and message.status in {"received", "queued", "processing"}
        for message in messages
    )
    latest_outbound = (latest.direction == "outbound") if latest else False
    latest_status = str(latest.status or "").lower() if latest and latest_outbound else ""
    return {
        "id": conversation.id,
        "name": contact_name,
        "detail": contact_detail,
        "preview": preview,
        "date": _message_date(latest) if latest else conversation.last_activity_at,
        "unread": unread,
        "message_count": len(messages),
        "latest_outbound": latest_outbound,
        "latest_status": latest_status,
        "messages": [_message_payload(message) for message in messages],
    }


def _redirect_to_conversation(conversation_id: int, *, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {"conversation_id": str(conversation_id)}
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    return RedirectResponse(f"/whatsapp/inbox?{urlencode(params)}", status_code=303)


def _inbox_conditions(*, company_id: int, channel_id: int, search: str) -> list:
    conditions = [
        Conversation.company_id == company_id,
        Conversation.channel_id == channel_id,
    ]
    if search:
        like = f"%{search}%"
        conditions.append(
            or_(
                Conversation.subject.ilike(like),
                Conversation.external_thread_id.ilike(like),
                exists(
                    select(1).where(
                        InboundMessage.conversation_id == Conversation.id,
                        InboundMessage.company_id == company_id,
                        or_(InboundMessage.sender.ilike(like), InboundMessage.original_content.ilike(like)),
                    )
                ),
            )
        )
    return conditions


def _load_inbox_data(
    db: Session,
    *,
    company_id: int,
    channel_id: int,
    conversation_id: int | None,
    search: str,
    page: int,
    page_size: int,
) -> dict:
    conditions = _inbox_conditions(company_id=company_id, channel_id=channel_id, search=search)
    conversations = db.scalars(
        select(Conversation)
        .where(*conditions)
        .options(selectinload(Conversation.messages).selectinload(InboundMessage.attachments))
        .order_by(Conversation.last_activity_at.desc(), Conversation.id.desc())
    ).unique().all()
    customer_ids = {conversation.customer_id for conversation in conversations if conversation.customer_id}
    customers = {}
    if customer_ids:
        customers = {
            customer.id: customer
            for customer in db.scalars(
                select(Customer).where(Customer.company_id == company_id, Customer.id.in_(customer_ids))
            ).all()
        }
    cards = [_conversation_card(conversation, customers) for conversation in conversations]
    page, page_size = normalize_page(page, page_size)
    total_items = len(cards)
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    start = (page - 1) * page_size
    page_cards = cards[start : start + page_size]

    selected = next((card for card in cards if card["id"] == conversation_id), None)
    if selected is None and page_cards:
        selected = page_cards[0]
    if selected is not None and selected not in page_cards:
        page_cards = [selected, *page_cards]
    return {
        "conditions": conditions,
        "cards": cards,
        "page_cards": page_cards,
        "selected": selected,
        "customers": customers,
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "start": start,
        "normalized_search": search,
    }


def _inbox_live_revision(db: Session, conditions: list) -> str:
    row = db.execute(
        select(
            func.count(func.distinct(Conversation.id)),
            func.max(Conversation.updated_at),
            func.max(InboundMessage.updated_at),
            func.max(InboundMessage.last_processed_at),
            func.max(InboundMessage.id),
        )
        .select_from(Conversation)
        .outerjoin(InboundMessage, InboundMessage.conversation_id == Conversation.id)
        .where(*conditions)
    ).one()
    source = "|".join(str(value or "") for value in row)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _inbox_partial_context(request: Request, user: TenantUser, channel: InputChannel, data: dict, revision: str) -> dict:
    return {
        "request": request,
        "user": user,
        "channel": channel,
        "conversations": data["page_cards"],
        "selected": data["selected"],
        "search": data["normalized_search"],
        "summary": {
            "conversations": data["total_items"],
            "unread": sum(1 for card in data["cards"] if card["unread"]),
        },
        "pagination": {
            "page": data["page"],
            "page_size": data["page_size"],
            "total_items": data["total_items"],
            "total_pages": data["total_pages"],
            "has_previous": data["page"] > 1,
            "has_next": data["page"] < data["total_pages"],
            "start_item": data["start"] + 1 if data["total_items"] else 0,
            "end_item": min(data["start"] + data["page_size"], data["total_items"]),
            "allowed_page_sizes": (15, 30, 50, 100),
        },
        "live_revision": revision,
    }


@router.get("/whatsapp/inbox")
async def whatsapp_inbox(
    request: Request,
    conversation_id: int | None = None,
    search: str = "",
    page: int = 1,
    page_size: int = 30,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    channel = _channel(db, user.company_id)
    if not channel:
        return PlainTextResponse("El canal WhatsApp no está activo para este tenant.", status_code=404)

    config = whatsapp_config(db, user.company_id)
    normalized_search = search.strip()
    data = _load_inbox_data(
        db,
        company_id=user.company_id,
        channel_id=channel.id,
        conversation_id=conversation_id,
        search=normalized_search,
        page=page,
        page_size=page_size,
    )
    cards = data["cards"]
    page_cards = data["page_cards"]
    selected = data["selected"]
    customers = data["customers"]

    if selected is not None and config.access_token and config.phone_number_id:
        selected_conv = db.get(Conversation, selected["id"])
        if selected_conv and selected_conv.messages:
            needs_refresh = False
            for msg in selected_conv.messages:
                has_pending = any(
                    not att.storage_path and (att.extraction_status or "pending") == "pending"
                    for att in (msg.attachments or [])
                )
                if has_pending:
                    try:
                        await download_whatsapp_media(db, company_id=user.company_id, inbound_message_id=msg.id)
                        needs_refresh = True
                    except Exception as exc:
                        logger.warning("Auto-downloading media for message %s failed: %s", msg.id, exc)
            if needs_refresh:
                db.expire_all()
                refreshed_conv = db.scalar(
                    select(Conversation)
                    .where(Conversation.id == selected["id"])
                    .options(selectinload(Conversation.messages).selectinload(InboundMessage.attachments))
                )
                if refreshed_conv:
                    selected = _conversation_card(refreshed_conv, customers)
                    page_cards = [selected if c["id"] == selected["id"] else c for c in page_cards]

    ready_to_send = whatsapp_outbound_is_ready(db, user.company_id, config=config)
    live_revision = _inbox_live_revision(db, data["conditions"])
    return templates.TemplateResponse(
        "whatsapp/inbox.html",
        {
            "request": request,
            "user": user,
            "title": "Buzón de WhatsApp",
            "channel": channel,
            "config": config,
            "ready_to_send": ready_to_send,
            "conversations": page_cards,
            "selected": selected,
            "search": normalized_search,
            "summary": {
                "conversations": data["total_items"],
                "unread": sum(1 for card in cards if card["unread"]),
            },
            "pagination": {
                "page": data["page"],
                "page_size": data["page_size"],
                "total_items": data["total_items"],
                "total_pages": data["total_pages"],
                "has_previous": data["page"] > 1,
                "has_next": data["page"] < data["total_pages"],
                "start_item": data["start"] + 1 if data["total_items"] else 0,
                "end_item": min(data["start"] + data["page_size"], data["total_items"]),
                "allowed_page_sizes": (15, 30, 50, 100),
            },
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "live_revision": live_revision,
        },
    )


@router.get("/whatsapp/inbox/updates", include_in_schema=False)
async def whatsapp_inbox_updates(
    request: Request,
    conversation_id: int | None = None,
    search: str = "",
    page: int = 1,
    page_size: int = 30,
    since: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    channel = _channel(db, user.company_id)
    if not channel:
        return PlainTextResponse("El canal WhatsApp no está activo para este tenant.", status_code=404)

    normalized_search = search.strip()
    conditions = _inbox_conditions(company_id=user.company_id, channel_id=channel.id, search=normalized_search)
    revision = _inbox_live_revision(db, conditions)
    headers = {"Cache-Control": "no-store", "ETag": f'"{revision}"'}
    if since and since == revision:
        return Response(status_code=304, headers=headers)

    data = _load_inbox_data(
        db,
        company_id=user.company_id,
        channel_id=channel.id,
        conversation_id=conversation_id,
        search=normalized_search,
        page=page,
        page_size=page_size,
    )

    context = _inbox_partial_context(request, user, channel, data, revision)
    response = templates.TemplateResponse("whatsapp/_inbox_live.html", context)
    response.headers.update(headers)
    return response


@router.post("/whatsapp/inbox/{conversation_id}/reply")
async def whatsapp_inbox_reply(
    conversation_id: int,
    body: str = Form(""),
    form_idempotency_key: str = Form("", alias="idempotency_key"),
    header_idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    files: list[UploadFile] | None = File(default=None),
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    channel = _channel(db, user.company_id)
    if not channel:
        return PlainTextResponse("El canal WhatsApp no está activo para este tenant.", status_code=404)
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.company_id == user.company_id,
            Conversation.channel_id == channel.id,
        )
    )
    if not conversation:
        return PlainTextResponse("Conversación no encontrada.", status_code=404)

    config = whatsapp_config(db, user.company_id)
    if not whatsapp_outbound_is_ready(db, user.company_id, config=config):
        return _redirect_to_conversation(conversation_id, error="whatsapp_not_ready")

    attachment_payloads = []
    total_bytes = 0
    for upload in files or []:
        if not upload or not upload.filename:
            continue
        payload = await upload.read()
        total_bytes += len(payload)
        if total_bytes > config.max_attachment_bytes:
            return _redirect_to_conversation(conversation_id, error="attachment_too_large")
        content_type = (upload.content_type or "application/octet-stream").split(";", 1)[0].lower()
        extension = Path(upload.filename).suffix.lower()
        is_audio = content_type in WHATSAPP_SUPPORTED_AUDIO_MIME_TYPES or extension in WHATSAPP_SUPPORTED_AUDIO_EXTENSIONS
        is_document = content_type in WHATSAPP_SUPPORTED_DOCUMENT_MIME_TYPES or extension in WHATSAPP_SUPPORTED_DOCUMENT_EXTENSIONS
        if not (is_audio or is_document):
            return _redirect_to_conversation(conversation_id, error="attachment_type_not_supported")
        attachment_payloads.append(
            {
                "filename": Path(upload.filename).name[:200],
                "content_type": content_type,
                "content": payload,
                "is_audio": is_audio,
            }
        )

    clean_body = body.strip()
    if not clean_body and not attachment_payloads:
        return _redirect_to_conversation(conversation_id, error="empty_message")
    try:
        await send_manual_response(
            db,
            company_id=user.company_id,
            conversation_id=conversation_id,
            body=clean_body,
            user_id=user.id,
            attachments=attachment_payloads,
            idempotency_key=header_idempotency_key or form_idempotency_key,
        )
    except Exception:  # noqa: BLE001
        return _redirect_to_conversation(conversation_id, error="send_failed")
    return _redirect_to_conversation(conversation_id, notice="sent")


@router.get("/whatsapp/inbox/{conversation_id}/sync-media/{attachment_id}")
async def sync_media_attachment(
    conversation_id: int,
    attachment_id: int,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    attachment = db.get(MessageAttachment, attachment_id)
    if attachment and attachment.inbound_message_id:
        try:
            await download_whatsapp_media(db, company_id=user.company_id, inbound_message_id=attachment.inbound_message_id)
        except Exception as exc:
            logger.warning("Manual sync of whatsapp media failed: %s", exc)
    return RedirectResponse(f"/whatsapp/inbox?conversation_id={conversation_id}", status_code=303)
