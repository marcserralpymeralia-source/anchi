from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Conversation, InboundMessage, InputChannel


@dataclass(slots=True)
class NormalizedMessage:
    company_id: int
    channel_key: str
    provider: str
    external_id: str | None
    sender: str | None = None
    recipients: list[str] = field(default_factory=list)
    subject: str | None = None
    text_content: str | None = None
    html_content: str | None = None
    direction: str = "inbound"
    external_thread_id: str | None = None
    received_at: datetime | None = None
    sent_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)



def persist_normalized_message(
    db: Session,
    message: NormalizedMessage,
    *,
    content_type: str | None = None,
    has_attachments: bool = False,
    has_pdf: bool = False,
    has_audio: bool = False,
) -> tuple[InboundMessage, Conversation]:
    return upsert_inbound_message(
        db,
        company_id=message.company_id,
        channel_key=message.channel_key,
        provider=message.provider,
        external_id=message.external_id,
        sender=message.sender,
        recipients=message.recipients,
        subject=message.subject,
        text_content=message.text_content,
        html_content=message.html_content,
        direction=message.direction,
        external_thread_id=message.external_thread_id,
        received_at=message.received_at,
        sent_at=message.sent_at,
        metadata=message.metadata,
        content_type=content_type,
        has_attachments=has_attachments,
        has_pdf=has_pdf,
        has_audio=has_audio,
    )

def normalize_provider(value: str | None) -> str:
    return (value or "imap").strip().lower() or "imap"


def normalize_channel(value: str | None) -> str:
    return (value or "email").strip().lower() or "email"


def normalize_direction(value: str | None) -> str:
    normalized = (value or "inbound").strip().lower()
    return normalized if normalized in {"inbound", "outbound", "internal"} else "inbound"


def normalize_recipients(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("\n", ",").replace(";", ",")
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def conversation_thread_key(message: NormalizedMessage) -> str:
    return (message.external_thread_id or message.external_id or "").strip()


def ensure_input_channel(db: Session, company_id: int, *, key: str, name: str, provider: str = "imap") -> InputChannel:
    channel = db.query(InputChannel).filter(InputChannel.company_id == company_id, InputChannel.key == key).one_or_none()
    if channel:
        return channel
    channel = InputChannel(
        company_id=company_id,
        key=key,
        name=name,
        channel_type="message",
        is_active=True,
        is_default=key == "email",
        supports_text=True,
        supports_attachments=True,
        supports_documents=True,
        supports_audio=False,
        supports_images=False,
    )
    db.add(channel)
    db.flush()
    return channel


def get_or_create_conversation(
    db: Session,
    *,
    company_id: int,
    channel_id: int,
    provider: str,
    external_thread_id: str | None,
    subject: str | None = None,
    customer_id: int | None = None,
    assigned_user_id: int | None = None,
    status: str = "open",
    last_activity_at: datetime | None = None,
) -> Conversation:
    provider = normalize_provider(provider)
    thread_key = (external_thread_id or "").strip() or None
    conversation = None
    if thread_key:
        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.company_id == company_id,
                Conversation.channel_id == channel_id,
                Conversation.provider == provider,
                Conversation.external_thread_id == thread_key,
            )
            .one_or_none()
        )
    if conversation:
        if subject and not conversation.subject:
            conversation.subject = subject
        if customer_id and not conversation.customer_id:
            conversation.customer_id = customer_id
        if assigned_user_id and not conversation.assigned_user_id:
            conversation.assigned_user_id = assigned_user_id
        if last_activity_at:
            conversation.last_activity_at = last_activity_at
        conversation.updated_at = datetime.now(timezone.utc)
        return conversation
    conversation = Conversation(
        company_id=company_id,
        channel_id=channel_id,
        provider=provider,
        external_thread_id=thread_key,
        customer_id=customer_id,
        assigned_user_id=assigned_user_id,
        status=status,
        subject=subject,
        last_activity_at=last_activity_at,
    )
    db.add(conversation)
    db.flush()
    return conversation


def find_inbound_message(
    db: Session,
    *,
    company_id: int,
    channel_id: int,
    provider: str,
    external_id: str | None,
) -> InboundMessage | None:
    if not external_id:
        return None
    return (
        db.query(InboundMessage)
        .filter(
            InboundMessage.company_id == company_id,
            InboundMessage.channel_id == channel_id,
            InboundMessage.provider == normalize_provider(provider),
            InboundMessage.source_external_id == external_id,
        )
        .one_or_none()
    )


def upsert_inbound_message(
    db: Session,
    *,
    company_id: int,
    channel_key: str,
    provider: str,
    external_id: str | None,
    sender: str | None,
    recipients: Any = None,
    subject: str | None = None,
    text_content: str | None = None,
    html_content: str | None = None,
    direction: str = "inbound",
    external_thread_id: str | None = None,
    received_at: datetime | None = None,
    sent_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    content_type: str | None = None,
    has_attachments: bool = False,
    has_pdf: bool = False,
    has_audio: bool = False,
) -> tuple[InboundMessage, Conversation]:
    channel = ensure_input_channel(db, company_id, key=normalize_channel(channel_key), name=channel_key.title(), provider=provider)
    conversation = get_or_create_conversation(
        db,
        company_id=company_id,
        channel_id=channel.id,
        provider=provider,
        external_thread_id=external_thread_id or external_id,
        subject=subject,
        last_activity_at=received_at or sent_at or datetime.now(timezone.utc),
    )
    existing = find_inbound_message(db, company_id=company_id, channel_id=channel.id, provider=provider, external_id=external_id)
    if existing:
        existing.conversation_id = conversation.id
        existing.provider = normalize_provider(provider)
        existing.direction = normalize_direction(direction)
        existing.sender = sender or existing.sender
        recipient_value = normalize_recipients(recipients)
        if recipient_value:
            existing.recipient = ", ".join(recipient_value)
        existing.subject = subject or existing.subject
        existing.original_content = text_content or existing.original_content
        existing.raw_payload_json = existing.raw_payload_json or (json.dumps(metadata, ensure_ascii=False) if metadata is not None else None)
        existing.content_type = content_type or existing.content_type
        existing.received_at = received_at or existing.received_at
        existing.status = existing.status or "received"
        existing.processing_step = existing.processing_step or "received"
        existing.has_attachments = has_attachments or existing.has_attachments
        existing.has_pdf = has_pdf or existing.has_pdf
        existing.has_audio = has_audio or existing.has_audio
        existing.updated_at = datetime.now(timezone.utc)
        return existing, conversation
    message = InboundMessage(
        company_id=company_id,
        channel_id=channel.id,
        provider=normalize_provider(provider),
        conversation_id=conversation.id,
        source_external_id=external_id,
        source_thread_id=external_thread_id,
        direction=normalize_direction(direction),
        sender=sender,
        recipient=", ".join(normalize_recipients(recipients)) if recipients else None,
        subject=subject,
        original_content=text_content,
        raw_payload_json=json.dumps(metadata or {}, ensure_ascii=False),
        content_type=content_type,
        received_at=received_at or datetime.now(timezone.utc),
        status="received",
        processing_step="received",
        has_attachments=has_attachments,
        has_pdf=has_pdf,
        has_audio=has_audio,
    )
    db.add(message)
    db.flush()
    return message, conversation


def link_message_to_order(db: Session, message: InboundMessage, order_id: int | None) -> None:
    message.order_id = order_id
    if message.conversation_id:
        conversation = db.get(Conversation, message.conversation_id)
        if conversation and order_id and conversation.last_activity_at is None:
            conversation.last_activity_at = message.received_at or datetime.now(timezone.utc)
            conversation.updated_at = datetime.now(timezone.utc)
