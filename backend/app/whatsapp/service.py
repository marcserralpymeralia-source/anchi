from __future__ import annotations

import hashlib
import hmac
import json
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import decrypt_secret
from app.channels.service import get_or_create_channel
from app.db.models import ChannelSetting, Conversation, InboundMessage, InputChannel, MessageAttachment
from app.jobs.service import enqueue_job
from app.logs.service import log_action
from app.messages.service import (
    NormalizedMessage,
    normalize_recipients,
    persist_normalized_message,
    upsert_inbound_message,
)
from app.master.models import MasterCompany, MasterTenantDatabase


WHATSAPP_CHANNEL_KEY = "whatsapp"
WHATSAPP_PROVIDER = "meta"


@dataclass(slots=True)
class WhatsAppTenantConfig:
    enabled: bool = False
    provider: str = "meta"
    phone_number_id: str = ""
    business_account_id: str = ""
    access_token: str = ""
    verify_token: str = ""
    app_secret: str = ""
    webhook_enabled: bool = False
    bot_enabled: bool = True
    default_language: str = "es"
    timezone: str = "Europe/Madrid"
    response_window_minutes: int = 24 * 60
    max_auto_messages: int = 3
    max_attachment_bytes: int = 10 * 1024 * 1024


def get_or_create_whatsapp_channel(db: Session, company_id: int) -> InputChannel:
    return get_or_create_channel(
        db,
        company_id,
        WHATSAPP_CHANNEL_KEY,
    )


def whatsapp_settings_map(db: Session, company_id: int) -> dict[str, str | None]:
    channel = get_or_create_whatsapp_channel(db, company_id)
    settings = db.scalars(select(ChannelSetting).where(ChannelSetting.company_id == company_id, ChannelSetting.channel_id == channel.id)).all()
    return {setting.key: setting.value for setting in settings}


def _secret_value(value: str | None) -> str:
    if not value:
        return ""
    return decrypt_secret(value) or value


def whatsapp_config(db: Session, company_id: int) -> WhatsAppTenantConfig:
    settings_map = whatsapp_settings_map(db, company_id)
    return WhatsAppTenantConfig(
        enabled=_as_bool(settings_map.get("enabled")),
        provider=(settings_map.get("provider") or WHATSAPP_PROVIDER).strip().lower() or WHATSAPP_PROVIDER,
        phone_number_id=(settings_map.get("phone_number_id") or "").strip(),
        business_account_id=(settings_map.get("business_account_id") or "").strip(),
        access_token=_secret_value(settings_map.get("access_token")).strip(),
        verify_token=_secret_value(settings_map.get("verify_token")).strip(),
        app_secret=_secret_value(settings_map.get("app_secret")).strip(),
        webhook_enabled=_as_bool(settings_map.get("webhook_enabled")),
        bot_enabled=_as_bool(settings_map.get("bot_enabled"), default=True),
        default_language=(settings_map.get("default_language") or "es").strip(),
        timezone=(settings_map.get("timezone") or "Europe/Madrid").strip(),
        response_window_minutes=_as_int(settings_map.get("response_window_minutes"), 24 * 60),
        max_auto_messages=_as_int(settings_map.get("max_auto_messages"), 3),
        max_attachment_bytes=_as_int(settings_map.get("max_attachment_bytes"), 10 * 1024 * 1024),
    )


def redact_whatsapp_config(config: WhatsAppTenantConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "provider": config.provider,
        "phone_number_id": config.phone_number_id,
        "business_account_id": config.business_account_id,
        "access_token": "••••••••" if config.access_token else "",
        "verify_token": "••••••••" if config.verify_token else "",
        "app_secret": "••••••••" if config.app_secret else "",
        "webhook_enabled": config.webhook_enabled,
        "bot_enabled": config.bot_enabled,
        "default_language": config.default_language,
        "timezone": config.timezone,
        "response_window_minutes": config.response_window_minutes,
        "max_auto_messages": config.max_auto_messages,
        "max_attachment_bytes": config.max_attachment_bytes,
    }


def resolve_company_from_slug(master_db: Session, company_slug: str) -> tuple[MasterCompany | None, MasterTenantDatabase | None]:
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == company_slug))
    if not company:
        return None, None
    tenant_db = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id, MasterTenantDatabase.is_active.is_(True)))
    return company, tenant_db


def verify_webhook_token(config: WhatsAppTenantConfig, verify_token: str | None) -> bool:
    return bool(config.verify_token and verify_token and hmac.compare_digest(config.verify_token, verify_token))


def verify_signature(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    if not app_secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)


def parse_payload_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
        changes = entry.get("changes", []) if isinstance(entry, dict) else []
        for change in changes:
            value = change.get("value", {}) if isinstance(change, dict) else {}
            metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
            phone_number_id = metadata.get("phone_number_id")
            business_account_id = metadata.get("display_phone_number") or metadata.get("business_account_id")
            contacts = value.get("contacts", []) if isinstance(value, dict) else []
            statuses = value.get("statuses", []) if isinstance(value, dict) else []
            messages = value.get("messages", []) if isinstance(value, dict) else []
            for message in messages:
                sender = message.get("from") or (contacts[0].get("wa_id") if contacts else None)
                text_content = _extract_message_text(message)
                events.append(
                    {
                        "kind": "message",
                        "external_id": message.get("id"),
                        "external_thread_id": message.get("context", {}).get("id") if isinstance(message.get("context"), dict) else message.get("id"),
                        "sender": sender,
                        "recipients": [business_account_id] if business_account_id else [],
                        "phone_number_id": phone_number_id,
                        "business_account_id": business_account_id,
                        "text_content": text_content,
                        "message_type": message.get("type"),
                        "metadata": {"payload": message, "metadata": metadata},
                        "attachments": _message_attachments(message),
                    }
                )
            for status in statuses:
                events.append(
                    {
                        "kind": "status",
                        "external_id": status.get("id"),
                        "external_thread_id": status.get("conversation", {}).get("id") if isinstance(status.get("conversation"), dict) else status.get("id"),
                        "sender": None,
                        "recipients": [status.get("recipient_id")] if status.get("recipient_id") else [],
                        "phone_number_id": phone_number_id,
                        "business_account_id": business_account_id,
                        "text_content": None,
                        "message_type": status.get("status"),
                        "metadata": {"payload": status, "metadata": metadata},
                        "attachments": [],
                    }
                )
    return events


def persist_event(db: Session, company_id: int, event: dict[str, Any], user=None) -> InboundMessage | None:
    channel = get_or_create_whatsapp_channel(db, company_id)
    metadata = dict(event.get("metadata") or {})
    metadata.setdefault("whatsapp", True)
    if event.get("kind") == "status":
        message = upsert_inbound_message(
            db,
            company_id=company_id,
            channel_key=WHATSAPP_CHANNEL_KEY,
            provider=WHATSAPP_PROVIDER,
            external_id=event.get("external_id"),
            sender=event.get("sender"),
            recipients=event.get("recipients"),
            subject="Estado WhatsApp",
            text_content=event.get("text_content"),
            external_thread_id=event.get("external_thread_id"),
            metadata=metadata,
            content_type="whatsapp_status",
            direction="outbound",
        )[0]
        status_value = str((metadata.get("payload") or {}).get("status") or "sent").lower()
        message.status = status_value
        message.processing_step = f"delivery_{status_value}"
        message.last_processed_at = datetime.now(timezone.utc)
        db.commit()
        log_action(db, company_id=company_id, user=user, action="whatsapp.status_received", entity_type="inbound_message", entity_id=message.id, message=f"Estado WhatsApp recibido: {status_value}")
        return message

    normalized = NormalizedMessage(
        company_id=company_id,
        channel_key=WHATSAPP_CHANNEL_KEY,
        provider=WHATSAPP_PROVIDER,
        external_id=event.get("external_id"),
        sender=event.get("sender"),
        recipients=normalize_recipients(event.get("recipients")),
        subject="WhatsApp",
        text_content=event.get("text_content"),
        external_thread_id=event.get("external_thread_id"),
        metadata=metadata,
    )
    message, conversation = persist_normalized_message(
        db,
        normalized,
        content_type=event.get("message_type") or "whatsapp",
        has_attachments=bool(event.get("attachments")),
        has_pdf=any(attachment.get("is_pdf") for attachment in event.get("attachments", [])),
        has_audio=any(attachment.get("is_audio") for attachment in event.get("attachments", [])),
    )
    message.channel_id = channel.id
    message.processing_step = "received_whatsapp"
    message.status = "received"
    message.last_processed_at = datetime.now(timezone.utc)
    for attachment in event.get("attachments", []):
        db.add(
            MessageAttachment(
                company_id=company_id,
                inbound_message_id=message.id,
                filename=attachment.get("filename") or attachment.get("media_id") or "whatsapp-attachment",
                content_type=attachment.get("content_type"),
                size_bytes=int(attachment.get("size_bytes") or 0),
                storage_path=None,
                extracted_text=None,
                ocr_text=None,
                transcription_text=None,
                is_pdf=bool(attachment.get("is_pdf")),
                is_image=bool(attachment.get("is_image")),
                is_audio=bool(attachment.get("is_audio")),
                extraction_status="pending",
            )
        )
    db.commit()
    log_action(db, company_id=company_id, user=user, action="whatsapp.message_received", entity_type="inbound_message", entity_id=message.id, message=f"WhatsApp recibido: {event.get('external_id')}")
    return message


def enqueue_whatsapp_processing(db: Session, company_id: int, inbound_message_id: int, user_id: int | None = None) -> object:
    return enqueue_job(
        db,
        company_id=company_id,
        job_type="process_inbound_message",
        payload={"inbound_message_id": inbound_message_id, "channel": WHATSAPP_CHANNEL_KEY},
        created_by_user_id=user_id,
    )


def record_manual_response(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    body: str,
    user_id: int | None = None,
    template_name: str | None = None,
) -> InboundMessage:
    conversation = db.get(Conversation, conversation_id)
    if not conversation or conversation.company_id != company_id:
        raise ValueError("Conversation not found for tenant.")
    external_id = f"wa-out-{uuid4().hex}"
    message, _ = upsert_inbound_message(
        db,
        company_id=company_id,
        channel_key=WHATSAPP_CHANNEL_KEY,
        provider=WHATSAPP_PROVIDER,
        external_id=external_id,
        sender=None,
        recipients=[],
        subject="WhatsApp outbound",
        text_content=body,
        external_thread_id=conversation.external_thread_id or external_id,
        metadata={"manual_response": True, "template_name": template_name},
        content_type="whatsapp_text",
        direction="outbound",
        sent_at=datetime.now(timezone.utc),
    )
    message.status = "sent"
    message.processing_step = "outbound_sent"
    conversation.status = "human_owned"
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, company_id=company_id, user=None, action="whatsapp.outbound_recorded", entity_type="inbound_message", entity_id=message.id, message="Respuesta manual de WhatsApp registrada")
    return message


def _extract_message_text(message: dict[str, Any]) -> str | None:
    if message.get("type") == "text":
        return (message.get("text") or {}).get("body")
    if message.get("type") == "document":
        document = message.get("document") or {}
        return document.get("caption") or document.get("filename")
    if message.get("type") == "image":
        image = message.get("image") or {}
        return image.get("caption") or "Imagen adjunta"
    if message.get("type") == "audio":
        return "Audio adjunto"
    return message.get("text", {}).get("body") if isinstance(message.get("text"), dict) else None


def _message_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if message.get("type") == "document":
        document = message.get("document") or {}
        attachments.append(
            {
                "media_id": document.get("id"),
                "filename": document.get("filename"),
                "content_type": document.get("mime_type"),
                "size_bytes": document.get("file_size"),
                "is_pdf": str(document.get("mime_type") or "").lower() == "application/pdf",
            }
        )
    elif message.get("type") == "image":
        image = message.get("image") or {}
        attachments.append(
            {
                "media_id": image.get("id"),
                "filename": image.get("filename") or "image.jpg",
                "content_type": image.get("mime_type"),
                "size_bytes": image.get("file_size"),
                "is_image": True,
            }
        )
    elif message.get("type") == "audio":
        audio = message.get("audio") or {}
        attachments.append(
            {
                "media_id": audio.get("id"),
                "filename": audio.get("filename") or "audio.ogg",
                "content_type": audio.get("mime_type"),
                "size_bytes": audio.get("file_size"),
                "is_audio": True,
            }
        )
    return attachments


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled", "active"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
