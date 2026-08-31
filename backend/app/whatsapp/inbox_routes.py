from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import current_user
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.db.models import Conversation, Customer, InboundMessage, InputChannel
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db
from app.whatsapp.service import (
    WHATSAPP_SUPPORTED_AUDIO_MIME_TYPES,
    WHATSAPP_SUPPORTED_AUDIO_EXTENSIONS,
    WHATSAPP_SUPPORTED_DOCUMENT_EXTENSIONS,
    WHATSAPP_SUPPORTED_DOCUMENT_MIME_TYPES,
    send_manual_response,
    whatsapp_config,
    whatsapp_outbound_is_ready,
)


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
    if is_audio or mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf" or extension == ".pdf":
        return "pdf"
    if extension == ".docx" or mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "doc"
    if mime.startswith("text/") or extension == ".txt":
        return "text"
    if mime.startswith("image/"):
        return "image"
    return "file"


def _format_bytes(size: int | None) -> str:
    value = int(size or 0)
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


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
                "status": attachment.extraction_status or "pending",
            }
        )
    return {
        "id": message.id,
        "direction": "outbound" if outbound else "inbound",
        "speaker": "Anchi" if outbound else (message.sender or "Contacto"),
        "text": message.original_content or "",
        "date": _message_date(message),
        "status": message.status,
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
    preview = (latest.original_content or "") if latest else "Sin mensajes todavía"
    if not preview and latest and latest.attachments:
        preview = latest.attachments[0].filename or "Archivo adjunto"
    unread = any(
        message.direction == "inbound" and message.status in {"received", "queued", "processing"}
        for message in messages
    )
    return {
        "id": conversation.id,
        "name": contact_name,
        "detail": contact_detail,
        "preview": preview,
        "date": _message_date(latest) if latest else conversation.last_activity_at,
        "unread": unread,
        "message_count": len(messages),
        "messages": [_message_payload(message) for message in messages],
    }


def _redirect_to_conversation(conversation_id: int, *, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {"conversation_id": str(conversation_id)}
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    return RedirectResponse(f"/whatsapp/inbox?{urlencode(params)}", status_code=303)


@router.get("/whatsapp/inbox")
def whatsapp_inbox(
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
    conditions = [
        Conversation.company_id == user.company_id,
        Conversation.channel_id == channel.id,
    ]
    if normalized_search:
        like = f"%{normalized_search}%"
        conditions.append(
            or_(
                Conversation.subject.ilike(like),
                Conversation.external_thread_id.ilike(like),
                exists(
                    select(1).where(
                        InboundMessage.conversation_id == Conversation.id,
                        InboundMessage.company_id == user.company_id,
                        or_(InboundMessage.sender.ilike(like), InboundMessage.original_content.ilike(like)),
                    )
                ),
            )
        )

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
                select(Customer).where(Customer.company_id == user.company_id, Customer.id.in_(customer_ids))
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

    ready_to_send = whatsapp_outbound_is_ready(db, user.company_id, config=config)
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
                "conversations": total_items,
                "unread": sum(1 for card in cards if card["unread"]),
            },
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total_items,
                "total_pages": total_pages,
                "has_previous": page > 1,
                "has_next": page < total_pages,
                "start_item": start + 1 if total_items else 0,
                "end_item": min(start + page_size, total_items),
                "allowed_page_sizes": (15, 30, 50, 100),
            },
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


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
