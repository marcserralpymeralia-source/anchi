from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from uuid import uuid4
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.encryption import decrypt_secret, encrypt_secret
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
META_ID_PATTERN = re.compile(r"^\d{5,32}$")

@dataclass(slots=True)
class WhatsAppTenantConfig:
    enabled: bool = False
    provider: str = "meta"
    phone_number_id: str = ""
    business_account_id: str = ""
    business_id: str = ""
    display_phone_number: str = ""
    verified_name: str = ""
    access_token: str = ""
    verify_token: str = ""
    webhook_enabled: bool = False
    bot_enabled: bool = True
    connection_status: str = "not_connected"
    connected_at: str = ""
    last_error: str = ""
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
        business_id=(settings_map.get("business_id") or "").strip(),
        display_phone_number=(settings_map.get("display_phone_number") or "").strip(),
        verified_name=(settings_map.get("verified_name") or "").strip(),
        access_token=_secret_value(settings_map.get("access_token")).strip(),
        verify_token=_secret_value(settings_map.get("verify_token")).strip(),
        webhook_enabled=_as_bool(settings_map.get("webhook_enabled")),
        bot_enabled=_as_bool(settings_map.get("bot_enabled"), default=True),
        connection_status=(settings_map.get("connection_status") or "not_connected").strip().lower(),
        connected_at=(settings_map.get("connected_at") or "").strip(),
        last_error=(settings_map.get("last_error") or "").strip(),
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
        "business_id": config.business_id,
        "display_phone_number": config.display_phone_number,
        "verified_name": config.verified_name,
        "access_token": "••••••••" if config.access_token else "",
        "verify_token": "••••••••" if config.verify_token else "",
        "webhook_enabled": config.webhook_enabled,
        "bot_enabled": config.bot_enabled,
        "connection_status": config.connection_status,
        "connected_at": config.connected_at,
        "last_error": config.last_error,
        "default_language": config.default_language,
        "timezone": config.timezone,
        "response_window_minutes": config.response_window_minutes,
        "max_auto_messages": config.max_auto_messages,
        "max_attachment_bytes": config.max_attachment_bytes,
    }


@dataclass(slots=True)
class WhatsAppEmbeddedSignupResult:
    business_account_id: str
    phone_number_id: str
    business_id: str
    display_phone_number: str
    verified_name: str
    webhook_url: str
    connection_status: str = "connected"


class WhatsAppEmbeddedSignupError(RuntimeError):
    def __init__(self, message: str, *, error_type: str = "meta_request_failed") -> None:
        super().__init__(message)
        self.error_type = error_type


def embedded_signup_public_config(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return {
        "configured": settings.meta_whatsapp_embedded_signup_ready,
        "app_id": settings.meta_app_id,
        "config_id": settings.meta_embedded_signup_config_id,
        "graph_api_version": settings.meta_graph_api_version,
        "embedded_signup_version": settings.meta_embedded_signup_version,
        "missing": settings.meta_whatsapp_missing_configuration,
    }


def whatsapp_webhook_url(company_slug: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.app_url.rstrip('/')}/webhooks/whatsapp/{company_slug}"


def _validate_meta_id(value: str, label: str, *, required: bool = True) -> str:
    normalized = str(value or "").strip()
    if not normalized and not required:
        return ""
    if not META_ID_PATTERN.fullmatch(normalized):
        raise WhatsAppEmbeddedSignupError(f"Meta no devolvió un {label} válido.", error_type="invalid_signup_payload")
    return normalized


def _meta_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
    return fallback


async def _meta_request(
    client: httpx.AsyncClient,
    settings: Settings,
    method: str,
    path: str,
    *,
    access_token: str | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"https://graph.facebook.com/{settings.meta_graph_api_version}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = await client.request(method, url, headers=headers, params=params, json=json_body)
    except httpx.TimeoutException as exc:
        raise WhatsAppEmbeddedSignupError("Meta no respondió a tiempo. Vuelve a intentarlo.", error_type="timeout") from exc
    except httpx.HTTPError as exc:
        raise WhatsAppEmbeddedSignupError("No se pudo conectar con Meta.", error_type="connection_failed") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise WhatsAppEmbeddedSignupError("Meta devolvió una respuesta no válida.", error_type="invalid_response") from exc
    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
        raise WhatsAppEmbeddedSignupError(_meta_error_message(payload, "Meta rechazó la operación."), error_type="meta_api_error")
    return payload if isinstance(payload, dict) else {}


def _upsert_whatsapp_setting(
    db: Session,
    *,
    company_id: int,
    channel_id: int,
    key: str,
    value: str,
    is_secret: bool = False,
) -> None:
    setting = db.scalar(
        select(ChannelSetting).where(
            ChannelSetting.company_id == company_id,
            ChannelSetting.channel_id == channel_id,
            ChannelSetting.key == key,
        )
    )
    if not setting:
        setting = ChannelSetting(company_id=company_id, channel_id=channel_id, key=key)
        db.add(setting)
    setting.value = encrypt_secret(value) if is_secret else value
    setting.value_type = "secret" if is_secret else "string"
    setting.is_secret = is_secret
    setting.updated_at = datetime.now(timezone.utc)


def _store_embedded_signup_state(
    db: Session,
    *,
    company_id: int,
    business_account_id: str,
    phone_number_id: str,
    business_id: str,
    access_token: str,
    verify_token: str,
    display_phone_number: str,
    verified_name: str,
    waba_name: str,
    connection_status: str,
    webhook_enabled: bool,
    last_error: str = "",
) -> InputChannel:
    channel = get_or_create_whatsapp_channel(db, company_id)
    channel.is_active = True
    channel.updated_at = datetime.now(timezone.utc)
    values = {
        "enabled": "true",
        "provider": WHATSAPP_PROVIDER,
        "business_account_id": business_account_id,
        "phone_number_id": phone_number_id,
        "business_id": business_id,
        "display_phone_number": display_phone_number,
        "verified_name": verified_name,
        "waba_name": waba_name,
        "connection_status": connection_status,
        "connected_at": datetime.now(timezone.utc).isoformat() if connection_status == "connected" else "",
        "webhook_enabled": "true" if webhook_enabled else "false",
        "bot_enabled": "true",
        "last_error": last_error[:500],
    }
    for key, value in values.items():
        _upsert_whatsapp_setting(db, company_id=company_id, channel_id=channel.id, key=key, value=value)
    _upsert_whatsapp_setting(db, company_id=company_id, channel_id=channel.id, key="access_token", value=access_token, is_secret=True)
    _upsert_whatsapp_setting(db, company_id=company_id, channel_id=channel.id, key="verify_token", value=verify_token, is_secret=True)
    db.commit()
    return channel


async def complete_embedded_signup(
    db: Session,
    *,
    company_id: int,
    company_slug: str,
    code: str,
    business_account_id: str,
    phone_number_id: str,
    business_id: str = "",
    client: httpx.AsyncClient | None = None,
) -> WhatsAppEmbeddedSignupResult:
    settings = get_settings()
    if not settings.meta_whatsapp_embedded_signup_ready:
        raise WhatsAppEmbeddedSignupError("Embedded Signup no está configurado en el servidor.", error_type="server_not_configured")
    code = str(code or "").strip()
    if not code or len(code) > 2048:
        raise WhatsAppEmbeddedSignupError("Meta no devolvió un código de autorización válido.", error_type="invalid_signup_payload")
    business_account_id = _validate_meta_id(business_account_id, "WABA ID")
    phone_number_id = _validate_meta_id(phone_number_id, "Phone Number ID")
    business_id = _validate_meta_id(business_id, "Business ID", required=False)
    callback_url = whatsapp_webhook_url(company_slug, settings)
    verify_token = secrets.token_urlsafe(32)
    owns_client = client is None
    graph_client = client or httpx.AsyncClient(timeout=settings.meta_request_timeout_seconds)
    try:
        token_params = {
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "code": code,
        }
        if settings.meta_oauth_redirect_uri:
            token_params["redirect_uri"] = settings.meta_oauth_redirect_uri
        token_payload = await _meta_request(
            graph_client,
            settings,
            "GET",
            "oauth/access_token",
            params=token_params,
        )
        access_token = str(token_payload.get("access_token") or "").strip()
        if not access_token:
            raise WhatsAppEmbeddedSignupError("Meta no devolvió un token de acceso.", error_type="token_exchange_failed")

        waba = await _meta_request(
            graph_client,
            settings,
            "GET",
            business_account_id,
            access_token=access_token,
            params={"fields": "id,name,business_verification_status"},
        )
        if str(waba.get("id") or "") != business_account_id:
            raise WhatsAppEmbeddedSignupError("El WABA devuelto por Meta no coincide con el autorizado.", error_type="asset_mismatch")
        phone_payload = await _meta_request(
            graph_client,
            settings,
            "GET",
            f"{business_account_id}/phone_numbers",
            access_token=access_token,
            params={"fields": "id,display_phone_number,verified_name,quality_rating,code_verification_status"},
        )
        phones = phone_payload.get("data") if isinstance(phone_payload.get("data"), list) else []
        phone = next((item for item in phones if str(item.get("id") or "") == phone_number_id), None)
        if not phone:
            raise WhatsAppEmbeddedSignupError("El teléfono seleccionado no pertenece al WABA autorizado.", error_type="asset_mismatch")

        state_payload = {
            "company_id": company_id,
            "business_account_id": business_account_id,
            "phone_number_id": phone_number_id,
            "business_id": business_id,
            "access_token": access_token,
            "verify_token": verify_token,
            "display_phone_number": str(phone.get("display_phone_number") or ""),
            "verified_name": str(phone.get("verified_name") or ""),
            "waba_name": str(waba.get("name") or ""),
        }
        _store_embedded_signup_state(db, **state_payload, connection_status="provisioning", webhook_enabled=False)
        try:
            await _meta_request(
                graph_client,
                settings,
                "POST",
                f"{phone_number_id}/register",
                access_token=access_token,
                json_body={"messaging_product": "whatsapp", "pin": settings.meta_whatsapp_registration_pin},
            )
            await _meta_request(
                graph_client,
                settings,
                "POST",
                f"{business_account_id}/subscribed_apps",
                access_token=access_token,
                json_body={"override_callback_uri": callback_url, "verify_token": verify_token},
            )
        except WhatsAppEmbeddedSignupError as exc:
            _store_embedded_signup_state(
                db,
                **state_payload,
                connection_status="error",
                webhook_enabled=False,
                last_error=str(exc),
            )
            raise

        _store_embedded_signup_state(db, **state_payload, connection_status="connected", webhook_enabled=True)
        return WhatsAppEmbeddedSignupResult(
            business_account_id=business_account_id,
            phone_number_id=phone_number_id,
            business_id=business_id,
            display_phone_number=str(phone.get("display_phone_number") or ""),
            verified_name=str(phone.get("verified_name") or ""),
            webhook_url=callback_url,
        )
    finally:
        if owns_client:
            await graph_client.aclose()


def resolve_company_from_slug(master_db: Session, company_slug: str) -> tuple[MasterCompany | None, MasterTenantDatabase | None]:
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == company_slug))
    if not company:
        return None, None
    tenant_db = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id, MasterTenantDatabase.is_active.is_(True)))
    return company, tenant_db


def resolve_company_from_whatsapp_identifiers(
    master_db: Session,
    *,
    business_account_id: str | None,
    phone_number_id: str | None,
) -> tuple[MasterCompany | None, MasterTenantDatabase | None]:
    from app.tenancy.database import tenant_db_session

    wanted_waba = str(business_account_id or "").strip()
    wanted_phone = str(phone_number_id or "").strip()
    if not wanted_waba and not wanted_phone:
        return None, None
    tenants = master_db.scalars(select(MasterTenantDatabase).where(MasterTenantDatabase.is_active.is_(True))).all()
    for tenant in tenants:
        tenant_db = tenant_db_session(tenant.database_url)()
        try:
            channel = tenant_db.scalar(
                select(InputChannel).where(
                    InputChannel.company_id == tenant.company_id,
                    InputChannel.key == WHATSAPP_CHANNEL_KEY,
                )
            )
            if not channel:
                continue
            rows = tenant_db.scalars(
                select(ChannelSetting).where(
                    ChannelSetting.company_id == tenant.company_id,
                    ChannelSetting.channel_id == channel.id,
                    ChannelSetting.key.in_(("business_account_id", "phone_number_id")),
                )
            ).all()
            values = {row.key: str(row.value or "").strip() for row in rows}
            phone_matches = bool(wanted_phone and values.get("phone_number_id") == wanted_phone)
            waba_matches = bool(wanted_waba and values.get("business_account_id") == wanted_waba)
            if phone_matches or waba_matches:
                return master_db.get(MasterCompany, tenant.company_id), tenant
        finally:
            tenant_db.close()
    return None, None


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
        entry_waba_id = entry.get("id") if isinstance(entry, dict) else None
        changes = entry.get("changes", []) if isinstance(entry, dict) else []
        for change in changes:
            value = change.get("value", {}) if isinstance(change, dict) else {}
            metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
            phone_number_id = metadata.get("phone_number_id")
            business_account_id = entry_waba_id or metadata.get("business_account_id")
            display_phone_number = metadata.get("display_phone_number")
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
                        "recipients": [display_phone_number] if display_phone_number else [],
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
                        "recipients": [status.get("recipient_id")] if status.get("recipient_id") else ([display_phone_number] if display_phone_number else []),
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
