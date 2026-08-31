from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import Conversation, InboundMessage, Order


_EXPLICIT_CLOSE_PATTERNS = (
    r"\bnada mas\b",
    r"\bnada más\b",
    r"\beso es todo\b",
    r"\bya esta\b",
    r"\bya está\b",
    r"\bfinaliza(?:r)?(?: el)? pedido\b",
    r"\bconfirma(?:r)?(?: el)? pedido\b",
    r"\bhazme el pedido\b",
    r"\benvia(?:r)?(?: el)? pedido\b",
    r"\benvía(?:r)?(?: el)? pedido\b",
)


@dataclass(slots=True)
class ConversationOrderContext:
    conversation_id: int
    messages: list[InboundMessage]
    transcript: str
    state: str
    closing_message_id: int | None = None


def _normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def has_explicit_order_close(text: str | None) -> bool:
    normalized = _normalized_text(text).lower()
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in _EXPLICIT_CLOSE_PATTERNS)


def _last_order_created_at(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
) -> datetime | None:
    return db.scalar(
        select(Order.created_at)
        .where(
            Order.company_id == company_id,
            Order.conversation_id == conversation_id,
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(1)
    )


def current_order_messages(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    through_message: InboundMessage | None = None,
    limit: int = 40,
) -> list[InboundMessage]:
    boundary = _last_order_created_at(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
    )

    query = (
        select(InboundMessage)
        .where(
            InboundMessage.company_id == company_id,
            InboundMessage.conversation_id == conversation_id,
            InboundMessage.content_type != "whatsapp_status",
        )
    )

    if boundary is not None:
        query = query.where(InboundMessage.received_at > boundary)

    if through_message is not None:
        if through_message.received_at is not None:
            query = query.where(
                or_(
                    InboundMessage.received_at < through_message.received_at,
                    and_(
                        InboundMessage.received_at == through_message.received_at,
                        InboundMessage.id <= through_message.id,
                    ),
                )
            )
        else:
            query = query.where(InboundMessage.id <= through_message.id)

    rows = db.scalars(
        query.order_by(
            InboundMessage.received_at.desc(),
            InboundMessage.id.desc(),
        ).limit(max(1, min(limit, 100)))
    ).all()

    return list(reversed(rows))


def _message_text(message: InboundMessage) -> str:
    content_parts: list[str] = []
    original_content = _normalized_text(message.original_content)
    if original_content:
        content_parts.append(original_content)

    for attachment in message.attachments or []:
        if attachment.extracted_text:
            content_parts.append(_normalized_text(attachment.extracted_text))
        elif attachment.ocr_text:
            content_parts.append(_normalized_text(attachment.ocr_text))
        elif attachment.transcription_text:
            content_parts.append(_normalized_text(attachment.transcription_text))

    return "\n\n".join(dict.fromkeys(part for part in content_parts if part))


def build_transcript(messages: list[InboundMessage]) -> str:
    lines: list[str] = []

    for message in messages:
        content = _message_text(message)
        if not content:
            continue

        role = "CLIENTE" if message.direction == "inbound" else "EMPRESA"
        lines.append(f"{role}: {content}")

    return "\n".join(lines)


def evaluate_conversation_order(
    db: Session,
    *,
    message: InboundMessage,
) -> ConversationOrderContext:
    if not message.conversation_id:
        return ConversationOrderContext(
            conversation_id=0,
            messages=[message],
            transcript=build_transcript([message]),
            state="not_conversational",
        )

    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == message.conversation_id,
            Conversation.company_id == message.company_id,
        )
    )
    if not conversation:
        return ConversationOrderContext(
            conversation_id=message.conversation_id,
            messages=[message],
            transcript=build_transcript([message]),
            state="not_conversational",
        )

    messages = current_order_messages(
        db,
        company_id=message.company_id,
        conversation_id=conversation.id,
        through_message=message,
    )
    transcript = build_transcript(messages)

    closing_message = next(
        (
            item
            for item in reversed(messages)
            if item.direction == "inbound"
            and has_explicit_order_close(item.original_content)
        ),
        None,
    )

    return ConversationOrderContext(
        conversation_id=conversation.id,
        messages=messages,
        transcript=transcript,
        state="ready" if closing_message else "collecting",
        closing_message_id=closing_message.id if closing_message else None,
    )
