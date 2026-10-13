import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from markupsafe import Markup, escape
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import Session, aliased, selectinload

from app.auth.dependencies import current_user
from app.core.pagination import normalize_page
from app.core.templating import templates
from app.customers.matching import build_customer_phone_index, customer_id_from_phone
from app.db.models import Conversation, Customer, InboundMessage, InputChannel, MessageAttachment, utcnow
from app.master.service import TenantUser
from app.tenancy.database import get_tenant_db
from app.whatsapp.service import (
    WHATSAPP_SUPPORTED_AUDIO_MIME_TYPES,
    WHATSAPP_SUPPORTED_AUDIO_EXTENSIONS,
    WHATSAPP_SUPPORTED_DOCUMENT_EXTENSIONS,
    WHATSAPP_SUPPORTED_DOCUMENT_MIME_TYPES,
    WHATSAPP_SUPPORTED_IMAGE_EXTENSIONS,
    WHATSAPP_SUPPORTED_IMAGE_MIME_TYPES,
    download_whatsapp_media,
    send_manual_response,
    whatsapp_config,
    whatsapp_outbound_is_ready,
    whatsapp_response_window_status,
    WhatsAppEmbeddedSignupError,
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


def _conversation_remote_phone(conversation: Conversation, messages: list[InboundMessage]) -> str | None:
    latest_inbound = next((message for message in reversed(messages) if message.direction == "inbound"), None)
    if latest_inbound and latest_inbound.sender:
        return latest_inbound.sender
    latest_outbound = next((message for message in reversed(messages) if message.direction == "outbound"), None)
    if latest_outbound and latest_outbound.recipient:
        return latest_outbound.recipient.split(",", 1)[0].strip()
    return conversation.external_thread_id


def _whatsapp_message_context(message: InboundMessage) -> dict:
    try:
        metadata = json.loads(message.raw_payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(metadata, dict):
        return {}
    payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else metadata.get("context")
    return context if isinstance(context, dict) else {}


def _message_preview(message: InboundMessage) -> str:
    text = " ".join(str(message.original_content or "").split())
    if text:
        return text[:180] + ("…" if len(text) > 180 else "")
    attachment = next(iter(message.attachments or []), None)
    if attachment:
        return str(attachment.filename or "Archivo adjunto")
    return "Mensaje sin texto"


def _highlight_search_text(text: str | None, search: str) -> Markup:
    value = str(text or "")
    term = search.strip()
    if not term:
        return Markup(escape(value))

    parts: list[Markup] = []
    cursor = 0
    for match in re.finditer(re.escape(term), value, flags=re.IGNORECASE):
        parts.append(Markup(escape(value[cursor:match.start()])))
        parts.append(Markup(f'<mark class="wa-search-highlight">{escape(match.group(0))}</mark>'))
        cursor = match.end()
    parts.append(Markup(escape(value[cursor:])))
    return Markup("").join(parts)


def _message_matches_search(message: InboundMessage, search: str) -> bool:
    term = search.strip().casefold()
    if not term:
        return False
    return term in str(message.original_content or "").casefold()


def _message_payload(message: InboundMessage, *, search: str = "") -> dict:
    outbound = message.direction == "outbound"
    search_match = _message_matches_search(message, search)
    reply_context = _whatsapp_message_context(message)
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
        "search_match": search_match,
        "text_html": _highlight_search_text(message.original_content, search) if search_match else "",
        # Incoming Meta webhooks use ``id`` while locally persisted outbound
        # replies use the Graph API's ``message_id`` key. Accept both shapes
        # so a reply remains visible after the page is reloaded.
        "reply_to_external_id": str(
            reply_context.get("id") or reply_context.get("message_id") or ""
        ).strip() or None,
        "reply_preview": _message_preview(message),
        "attachments": attachments,
    }


def _conversation_card(
    conversation: Conversation,
    customers: dict[int, Customer],
    phone_index: dict[str, int | None] | None = None,
    *,
    search: str = "",
    search_index: int | None = None,
) -> dict:
    messages = sorted(
        conversation.messages or [],
        key=_message_date,
    )
    latest = messages[-1] if messages else None
    latest_inbound = next((message for message in reversed(messages) if message.direction == "inbound"), None)
    customer_id = conversation.customer_id
    if not customer_id and phone_index is not None:
        customer_id = customer_id_from_phone(_conversation_remote_phone(conversation, messages), phone_index)
    customer = customers.get(customer_id or 0)
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
    latest_outbound_index = max(
        (index for index, message in enumerate(messages) if message.direction == "outbound"),
        default=-1,
    )
    unread_count = sum(
        1
        for index, message in enumerate(messages)
        if index > latest_outbound_index
        and message.direction == "inbound"
        and message.status in {"received", "queued", "processing"}
        and message.read_at is None
    )
    unread = unread_count > 0
    latest_outbound = (latest.direction == "outbound") if latest else False
    latest_status = str(latest.status or "").lower() if latest and latest_outbound else ""
    message_payloads = [_message_payload(message, search=search) for message in messages]
    messages_by_external_id = {
        str(message.source_external_id).strip(): message
        for message in messages
        if str(message.source_external_id or "").strip()
    }
    for message, payload in zip(messages, message_payloads):
        reply_to_external_id = payload["reply_to_external_id"]
        if not reply_to_external_id:
            payload["reply_to"] = None
            continue
        quoted = messages_by_external_id.get(reply_to_external_id)
        if quoted:
            payload["reply_to"] = {
                "id": quoted.id,
                "speaker": "Anchi" if quoted.direction == "outbound" else (quoted.sender or "Cliente"),
                "text": _message_preview(quoted),
                "direction": quoted.direction,
            }
        else:
            payload["reply_to"] = {
                "id": None,
                "speaker": "WhatsApp",
                "text": "Mensaje citado no disponible",
                "direction": "inbound",
            }
    search_matches = [payload for payload in message_payloads if payload["search_match"]]
    if search_matches:
        active_search_index = len(search_matches) - 1 if search_index is None else min(max(search_index, 0), len(search_matches) - 1)
        for match_index, payload in enumerate(search_matches):
            payload["search_match_index"] = match_index
    else:
        active_search_index = -1
    return {
        "id": conversation.id,
        "name": contact_name,
        "detail": contact_detail,
        "preview": preview,
        "date": _message_date(latest) if latest else conversation.last_activity_at,
        "unread": unread,
        "unread_count": unread_count,
        "latest_outbound": latest_outbound,
        "latest_status": latest_status,
        "customer_id": customer.id if customer else None,
        "customer_verified": bool(customer),
        "messages": message_payloads,
        "search_match_count": len(search_matches),
        "search_match_index": active_search_index,
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
                    select(1)
                    .select_from(InboundMessage)
                    .where(
                        InboundMessage.conversation_id == Conversation.id,
                        InboundMessage.company_id == company_id,
                        or_(InboundMessage.sender.ilike(like), InboundMessage.original_content.ilike(like)),
                    )
                    .correlate(Conversation)
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
    search_index: int | None,
) -> dict:
    conditions = _inbox_conditions(company_id=company_id, channel_id=channel_id, search=search)
    page, page_size = normalize_page(page, page_size)
    total_items = int(db.scalar(select(func.count(Conversation.id)).where(*conditions)) or 0)
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    start = (page - 1) * page_size

    page_conversations = db.scalars(
        select(Conversation)
        .where(*conditions)
        .options(selectinload(Conversation.messages).selectinload(InboundMessage.attachments))
        .order_by(Conversation.last_activity_at.desc(), Conversation.id.desc())
        .offset(start)
        .limit(page_size)
    ).unique().all()

    selected_conversation = None
    if conversation_id is not None:
        selected_conversation = db.scalar(
            select(Conversation)
            .where(*conditions, Conversation.id == conversation_id)
            .options(selectinload(Conversation.messages).selectinload(InboundMessage.attachments))
        )

    conversations = list(page_conversations)
    if selected_conversation is not None and all(item.id != selected_conversation.id for item in conversations):
        conversations.insert(0, selected_conversation)

    phone_index = build_customer_phone_index(db, company_id) if conversations else {}
    customer_ids = {conversation.customer_id for conversation in conversations if conversation.customer_id}
    customer_ids.update(
        matched_id
        for conversation in conversations
        if not conversation.customer_id
        for matched_id in [customer_id_from_phone(_conversation_remote_phone(conversation, conversation.messages or []), phone_index)]
        if matched_id
    )
    customers = {}
    if customer_ids:
        customers = {
            customer.id: customer
            for customer in db.scalars(
                select(Customer).where(Customer.company_id == company_id, Customer.id.in_(customer_ids))
            ).all()
        }
    cards = [
        _conversation_card(
            conversation,
            customers,
            phone_index,
            search=search,
            search_index=search_index,
        )
        for conversation in conversations
    ]
    page_card_ids = {conversation.id for conversation in page_conversations}
    page_cards = [card for card in cards if card["id"] in page_card_ids]

    selected = next((card for card in cards if card["id"] == conversation_id), None)
    if selected is None and page_cards:
        selected = page_cards[0]
    if selected is not None and selected["id"] not in page_card_ids:
        page_cards = [selected, *page_cards]

    latest_outbound = aliased(InboundMessage)
    latest_outbound_at = (
        select(func.max(latest_outbound.received_at))
        .where(
            latest_outbound.conversation_id == Conversation.id,
            latest_outbound.direction == "outbound",
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    unread_count = int(
        db.scalar(
            select(func.count(InboundMessage.id))
            .join(Conversation, InboundMessage.conversation_id == Conversation.id)
            .where(
                *conditions,
                InboundMessage.direction == "inbound",
                InboundMessage.status.in_(("received", "queued", "processing")),
                InboundMessage.read_at.is_(None),
                or_(latest_outbound_at.is_(None), InboundMessage.received_at > latest_outbound_at),
            )
        )
        or 0
    )
    return {
        "conditions": conditions,
        "cards": page_cards,
        "page_cards": page_cards,
        "selected": selected,
        "customers": customers,
        "phone_index": phone_index,
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "start": start,
        "unread_count": unread_count,
        "normalized_search": search,
    }


def _mark_conversation_as_read(
    db: Session,
    *,
    company_id: int,
    channel_id: int,
    conversation_id: int,
) -> bool:
    """Mark visible inbound WhatsApp messages as read without changing processing state."""

    result = db.execute(
        update(InboundMessage)
        .where(
            InboundMessage.company_id == company_id,
            InboundMessage.channel_id == channel_id,
            InboundMessage.conversation_id == conversation_id,
            InboundMessage.direction == "inbound",
            InboundMessage.read_at.is_(None),
        )
        .values(read_at=utcnow())
    )
    if not result.rowcount:
        return False
    db.commit()
    return True


def _inbox_live_revision(db: Session, conditions: list) -> str:
    row = db.execute(
        select(
            func.count(func.distinct(Conversation.id)),
            func.max(Conversation.updated_at),
            func.max(InboundMessage.updated_at),
            func.max(InboundMessage.last_processed_at),
            func.max(InboundMessage.read_at),
            func.max(InboundMessage.id),
            func.max(MessageAttachment.created_at),
            func.max(MessageAttachment.storage_path),
            func.max(MessageAttachment.extraction_status),
        )
        .select_from(Conversation)
        .outerjoin(InboundMessage, InboundMessage.conversation_id == Conversation.id)
        .outerjoin(MessageAttachment, MessageAttachment.inbound_message_id == InboundMessage.id)
        .where(*conditions)
    ).one()
    source = "|".join(str(value or "") for value in row)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _whatsapp_compose_context(
    db: Session,
    user: TenantUser,
    selected: dict | None,
    config,
) -> dict:
    ready_to_send = whatsapp_outbound_is_ready(db, user.company_id, config=config)
    response_window = (
        whatsapp_response_window_status(
            db,
            company_id=user.company_id,
            conversation_id=selected["id"],
            response_window_minutes=config.response_window_minutes,
        )
        if selected
        else {"is_open": False, "reason": "no_conversation", "remaining_seconds": 0}
    )
    return {
        "ready_to_send": ready_to_send,
        "response_window": response_window,
        "can_send": ready_to_send and response_window["is_open"],
    }


def _inbox_partial_context(
    request: Request,
    user: TenantUser,
    channel: InputChannel,
    data: dict,
    revision: str,
    compose_context: dict,
) -> dict:
    return {
        "request": request,
        "user": user,
        "channel": channel,
        **compose_context,
        "conversations": data["page_cards"],
        "selected": data["selected"],
        "search": data["normalized_search"],
        "summary": {
            "conversations": data["total_items"],
            "unread": data["unread_count"],
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
    search_index: int | None = None,
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    channel = _channel(db, user.company_id)
    if not channel:
        return PlainTextResponse("El canal WhatsApp no está activo para este tenant.", status_code=404)

    config = whatsapp_config(db, user.company_id)
    normalized_search = search.strip()
    if conversation_id is not None:
        _mark_conversation_as_read(
            db,
            company_id=user.company_id,
            channel_id=channel.id,
            conversation_id=conversation_id,
        )
    data = _load_inbox_data(
        db,
        company_id=user.company_id,
        channel_id=channel.id,
        conversation_id=conversation_id,
        search=normalized_search,
        page=page,
        page_size=page_size,
        search_index=search_index,
    )
    page_cards = data["page_cards"]
    selected = data["selected"]

    compose_context = _whatsapp_compose_context(db, user, selected, config)
    live_revision = _inbox_live_revision(db, data["conditions"])
    return templates.TemplateResponse(
        "whatsapp/inbox.html",
        {
            "request": request,
            "user": user,
            "title": "Buzón de WhatsApp",
            "channel": channel,
            "config": config,
            **compose_context,
            "conversations": page_cards,
            "selected": selected,
            "search": normalized_search,
            "summary": {
                "conversations": data["total_items"],
                "unread": data["unread_count"],
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
    search_index: int | None = None,
    since: str = "",
    db: Session = Depends(get_tenant_db),
    user: TenantUser = Depends(current_user),
):
    channel = _channel(db, user.company_id)
    if not channel:
        return PlainTextResponse("El canal WhatsApp no está activo para este tenant.", status_code=404)

    if conversation_id is not None:
        _mark_conversation_as_read(
            db,
            company_id=user.company_id,
            channel_id=channel.id,
            conversation_id=conversation_id,
        )
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
        search_index=search_index,
    )

    config = whatsapp_config(db, user.company_id)
    compose_context = _whatsapp_compose_context(db, user, data["selected"], config)
    context = _inbox_partial_context(request, user, channel, data, revision, compose_context)
    response = templates.TemplateResponse("whatsapp/_inbox_live.html", context)
    response.headers.update(headers)
    return response


@router.post("/whatsapp/inbox/{conversation_id}/reply")
async def whatsapp_inbox_reply(
    conversation_id: int,
    body: str = Form(""),
    reply_to_message_id: str = Form(""),
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
    response_window = whatsapp_response_window_status(
        db,
        company_id=user.company_id,
        conversation_id=conversation_id,
        response_window_minutes=config.response_window_minutes,
    )
    if not response_window["is_open"]:
        return _redirect_to_conversation(conversation_id, error=response_window["reason"])

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
        is_image = content_type in WHATSAPP_SUPPORTED_IMAGE_MIME_TYPES or extension in WHATSAPP_SUPPORTED_IMAGE_EXTENSIONS
        if not (is_audio or is_document or is_image):
            return _redirect_to_conversation(conversation_id, error="attachment_type_not_supported")
        attachment_payloads.append(
            {
                "filename": Path(upload.filename).name[:200],
                "content_type": content_type,
                "content": payload,
                "is_audio": is_audio,
                "is_image": is_image,
            }
        )

    clean_body = body.strip()
    if not clean_body and not attachment_payloads:
        return _redirect_to_conversation(conversation_id, error="empty_message")
    try:
        parsed_reply_to_message_id = int(reply_to_message_id) if str(reply_to_message_id or "").strip() else None
    except (TypeError, ValueError):
        return _redirect_to_conversation(conversation_id, error="invalid_reply")
    try:
        await send_manual_response(
            db,
            company_id=user.company_id,
            conversation_id=conversation_id,
            body=clean_body,
            user_id=user.actor_id,
            attachments=attachment_payloads,
            idempotency_key=header_idempotency_key or form_idempotency_key,
            reply_to_message_id=parsed_reply_to_message_id,
        )
    except WhatsAppEmbeddedSignupError as exc:
        error_type = getattr(exc, "error_type", "send_failed")
        if error_type in {"customer_has_not_replied", "response_window_expired", "response_window_unknown", "recipient_not_found", "invalid_reply"}:
            return _redirect_to_conversation(conversation_id, error=error_type)
        return _redirect_to_conversation(conversation_id, error="send_failed")
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
    attachment = db.scalar(
        select(MessageAttachment).where(
            MessageAttachment.id == attachment_id,
            MessageAttachment.company_id == user.company_id,
        )
    )
    if attachment and attachment.inbound_message_id:
        try:
            await download_whatsapp_media(db, company_id=user.company_id, inbound_message_id=attachment.inbound_message_id)
        except Exception as exc:
            logger.warning("Manual sync of whatsapp media failed: %s", exc)
    return RedirectResponse(f"/whatsapp/inbox?conversation_id={conversation_id}", status_code=303)
