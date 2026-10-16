from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.customers.matching import customer_id_from_phone, build_customer_phone_index
from app.db.models import (
    Alert,
    BackgroundJob,
    Company,
    Conversation,
    Customer,
    InboundMessage,
    Order,
    Product,
    ProductAlias,
    WhatsAppOrderSession,
)
from app.jobs.service import enqueue_job
from app.logs.service import log_flow_event
from app.whatsapp.conversation_orders import (
    has_explicit_order_cancellation,
    has_explicit_order_close,
    is_short_order_confirmation,
)
from app.whatsapp.conversation_semantics import WhatsAppAgentTurnResult, evaluate_whatsapp_agent_turn
from app.whatsapp.service import (
    WHATSAPP_RETRYABLE_AUTO_SEND_STEP,
    send_automatic_response,
    whatsapp_config,
    whatsapp_response_window_status,
)

logger = logging.getLogger(__name__)

_ACTIVE_KEY_PREFIX = "whatsapp-conversation:"
_ACTIVE_STATUSES = {
    "collecting",
    "awaiting_customer",
    "ready_for_review",
    "human_handoff",
    "finalizing",
}
_MEDIA_PENDING = {"pending", "downloaded", "storage_error"}
_MEDIA_UNREADABLE = {
    "extraction_error",
    "unsupported",
    "no_text_found",
    "stored",
    # Audio is downloaded by the current media worker, but no transcription
    # provider is wired into that worker yet. Never infer order details from it.
    "transcription_pending",
}
_ACTIVE_JOB_STATES = {"queued", "running", "retrying"}
_CANCEL_REPLY_PENDING = "customer_cancel_reply_pending"
_CANCEL_HANDOFF_REPLY_PENDING = "customer_cancel_handoff_reply_pending"
_MAX_AGENT_CONTEXT_CHARS = 15000
_MAX_AGENT_CONTEXT_MESSAGES = 50


class WhatsAppAgentReplyDeliveryError(RuntimeError):
    """Keep a failed customer reply from being recorded as a successful job."""

    def __init__(self, error_type: str, *, retryable: bool = False) -> None:
        self.error_type = str(error_type or "reply_delivery_failed")[:120]
        # A provider 429 explicitly rejects delivery. Network/5xx errors may
        # have reached Meta, so replaying those could duplicate a customer reply.
        self.retryable = bool(retryable and self.error_type == "rate_limited")
        super().__init__("No se pudo confirmar la entrega de la respuesta automática de WhatsApp.")


def _unit_key(value: str | None) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().casefold()).rstrip(".")
    aliases = {
        "ud": "unidad",
        "uds": "unidad",
        "und": "unidad",
        "unds": "unidad",
        "unidades": "unidad",
        "kg": "kilogramo",
        "kilo": "kilogramo",
        "kilos": "kilogramo",
        "kilogramos": "kilogramo",
        "g": "gramo",
        "gr": "gramo",
        "gramos": "gramo",
        "l": "litro",
        "lt": "litro",
        "lts": "litro",
        "litros": "litro",
        "cajas": "caja",
        "paquetes": "paquete",
        "bolsas": "bolsa",
        "rollos": "rollo",
    }
    return aliases.get(normalized, normalized)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _json_object(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _outbound_metadata(message: InboundMessage) -> dict[str, Any]:
    value = _json_object(message.raw_payload_json, {})
    return value if isinstance(value, dict) else {}


def _reply_context_external_id(message: InboundMessage) -> str:
    metadata = _json_object(message.raw_payload_json, {})
    payload = metadata.get("payload") if isinstance(metadata.get("payload"), dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    return str(context.get("id") or "").strip()


def _reply_context_message(
    message: InboundMessage,
    messages: list[InboundMessage],
) -> InboundMessage | None:
    external_id = _reply_context_external_id(message)
    if external_id:
        return next(
            (
                candidate
                for candidate in messages
                if candidate.id != message.id
                and candidate.source_external_id == external_id
                and candidate.company_id == message.company_id
                and candidate.conversation_id == message.conversation_id
            ),
            None,
        )
    ordered = sorted(
        (item for item in messages if item.content_type != "whatsapp_status"),
        key=lambda item: (_aware(item.received_at) or _now(), item.id),
    )
    current_index = next(
        (index for index, item in enumerate(ordered) if item.id == message.id),
        None,
    )
    if current_index is None or current_index == 0:
        return None
    previous = ordered[current_index - 1]
    return previous if previous.direction == "outbound" else None


def _confirms_current_order_summary(
    messages: list[InboundMessage],
    *,
    message: InboundMessage,
    session: WhatsAppOrderSession,
    draft: dict[str, Any],
    missing: list[str],
) -> bool:
    if message.direction != "inbound" or not is_short_order_confirmation(message.original_content):
        return False
    if not _draft_is_complete(draft, missing) or session.status != "ready_for_review":
        return False
    ordered = sorted(
        (item for item in messages if item.id != message.id and item.content_type != "whatsapp_status"),
        key=lambda item: (_aware(item.received_at) or _now(), item.id),
    )
    quoted_message = _reply_context_message(message, messages)
    if quoted_message:
        if quoted_message.direction != "outbound":
            return False
        quoted_index = next(
            (index for index, item in enumerate(ordered) if item.id == quoted_message.id),
            None,
        )
        if quoted_index is None or any(
            item.direction == "inbound" for item in ordered[quoted_index + 1 :]
        ):
            return False
        previous = quoted_message
    else:
        if not ordered or ordered[-1].direction != "outbound":
            return False
        previous = ordered[-1]
    metadata = _outbound_metadata(previous)
    return bool(
        metadata.get("auto_response")
        and metadata.get("semantic_state") == "ready_for_confirmation"
        and int(metadata.get("trigger_message_id") or 0) == int(session.last_processed_message_id or 0)
        and int(metadata.get("assistant_session_id") or 0) == int(session.id)
        and int(metadata.get("draft_revision") or -1) == int(session.revision or 0) - 1
        and _order_summary_reply(draft).strip() in str(previous.original_content or "")
    )


def active_whatsapp_session(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    lock: bool = False,
) -> WhatsAppOrderSession | None:
    base_key = f"{_ACTIVE_KEY_PREFIX}{conversation_id}"
    query = select(WhatsAppOrderSession).where(
        WhatsAppOrderSession.company_id == company_id,
        WhatsAppOrderSession.conversation_id == conversation_id,
        or_(
            WhatsAppOrderSession.active_key == base_key,
            WhatsAppOrderSession.active_key.like(f"{base_key}:%"),
        ),
    )
    if lock:
        query = query.with_for_update()
    return db.scalar(query.order_by(WhatsAppOrderSession.id.desc()).limit(1))


def _latest_customer_message(db: Session, *, company_id: int, conversation_id: int) -> InboundMessage | None:
    return db.scalar(
        select(InboundMessage)
        .where(
            InboundMessage.company_id == company_id,
            InboundMessage.conversation_id == conversation_id,
            InboundMessage.direction == "inbound",
            InboundMessage.content_type != "whatsapp_status",
        )
        .options(selectinload(InboundMessage.attachments))
        .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
        .limit(1)
    )


def _retry_prepared_whatsapp_reply(
    db: Session,
    *,
    company_id: int,
    conversation: Conversation,
    session: WhatsAppOrderSession,
    inbound_message: InboundMessage,
    config: Any,
) -> dict[str, Any]:
    """Retry only a stored reply definitively rejected by Meta with HTTP 429."""

    idempotency_key = f"auto:{inbound_message.id}"
    outbound = db.scalar(
        select(InboundMessage).where(
            InboundMessage.company_id == company_id,
            InboundMessage.conversation_id == conversation.id,
            InboundMessage.direction == "outbound",
            InboundMessage.source_message_id == idempotency_key,
        )
    )
    metadata = _outbound_metadata(outbound) if outbound else {}
    if (
        not outbound
        or outbound.status != "failed"
        or outbound.processing_step != WHATSAPP_RETRYABLE_AUTO_SEND_STEP
        or not session.suggested_reply
        or str(outbound.original_content or "") != session.suggested_reply
        or int(metadata.get("trigger_message_id") or 0) != inbound_message.id
        or int(metadata.get("assistant_session_id") or 0) != session.id
        or metadata.get("draft_revision") != session.revision
    ):
        raise WhatsAppAgentReplyDeliveryError("retry_state_invalid")

    window = whatsapp_response_window_status(
        db,
        company_id=company_id,
        conversation_id=conversation.id,
        response_window_minutes=config.response_window_minutes,
    )
    if not window.get("is_open"):
        if session.closed_reason in {
            _CANCEL_REPLY_PENDING,
            _CANCEL_HANDOFF_REPLY_PENDING,
        }:
            transition = _finish_pending_customer_cancellation(
                db,
                session=session,
                conversation=conversation,
                inbound_message=inbound_message,
            )
            return {
                "ok": True,
                "status": (transition or {}).get("status", session.status),
                "session_id": session.id,
                "auto_response": {
                    "sent": False,
                    "reason": str(window.get("reason") or "response_window_closed"),
                },
                "retried": True,
            }
        raise WhatsAppAgentReplyDeliveryError(
            str(window.get("reason") or "response_window_closed")
        )

    try:
        send_result = await_send_automatic_response(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            trigger_message_id=inbound_message.id,
            body=session.suggested_reply,
            semantic_state=str(metadata.get("semantic_state") or "informational"),
            prompt_execution_id=metadata.get("prompt_execution_id"),
            assistant_session_id=session.id,
            draft_revision=session.revision,
        )
    except Exception as exc:  # noqa: BLE001
        error_type = str(getattr(exc, "error_type", None) or type(exc).__name__)
        retryable = error_type == "rate_limited"
        if retryable:
            inbound_message.processing_step = "whatsapp_agent_reply_retryable"
            db.commit()
        elif session.closed_reason in {
            _CANCEL_REPLY_PENDING,
            _CANCEL_HANDOFF_REPLY_PENDING,
        }:
            transition = _finish_pending_customer_cancellation(
                db,
                session=session,
                conversation=conversation,
                inbound_message=inbound_message,
            )
            return {
                "ok": True,
                "status": (transition or {}).get("status", session.status),
                "session_id": session.id,
                "auto_response": {"sent": False, "reason": error_type},
                "retried": True,
            }
        raise WhatsAppAgentReplyDeliveryError(error_type, retryable=retryable) from exc

    if not send_result.get("sent"):
        reason = str(send_result.get("reason") or "reply_not_sent")
        db.refresh(outbound)
        if reason == "already_replied" and str(outbound.status or "").lower() in {
            "accepted", "sent", "delivered", "read"
        }:
            pass
        elif reason in {"bot_disabled", "human_owned", "human_active", "stale_trigger"}:
            if session.closed_reason in {
                _CANCEL_REPLY_PENDING,
                _CANCEL_HANDOFF_REPLY_PENDING,
            }:
                transition = _finish_pending_customer_cancellation(
                    db,
                    session=session,
                    conversation=conversation,
                    inbound_message=inbound_message,
                )
                return {
                    "ok": True,
                    "status": (transition or {}).get("status", session.status),
                    "session_id": session.id,
                    "auto_response": {"sent": False, "reason": reason},
                    "retried": True,
                }
            inbound_message.processing_step = (
                "whatsapp_agent_handoff_waiting"
                if reason in {"human_owned", "human_active"}
                else "whatsapp_agent_superseded"
            )
            db.commit()
            return {"ok": True, "skipped": True, "reason": reason, "session_id": session.id}
        else:
            raise WhatsAppAgentReplyDeliveryError(reason)

    inbound_message.status = "received"
    inbound_message.processing_step = "whatsapp_agent_reply_sent"
    db.commit()
    _log_agent_event(
        db,
        company_id=company_id,
        conversation_id=conversation.id,
        session_id=session.id,
        event="reply_sent",
        status="sent",
        metadata={"message_id": inbound_message.id, "retry": True},
    )
    if session.status == "human_handoff":
        conversation.whatsapp_bot_enabled = False
        conversation.updated_at = _now()
        db.commit()
    transition = None
    if session.closed_reason in {
        _CANCEL_REPLY_PENDING,
        _CANCEL_HANDOFF_REPLY_PENDING,
    }:
        transition = _finish_pending_customer_cancellation(
            db,
            session=session,
            conversation=conversation,
            inbound_message=inbound_message,
        )
    return {
        "ok": True,
        "status": (transition or {}).get("status", session.status),
        "session_id": session.id,
        "intent": session.intent,
        "reply_needed": True,
        "auto_response": send_result,
        "retried": True,
    }


def _session_cutoff_condition(session: WhatsAppOrderSession):
    cutoff = _aware(session.message_cutoff_at)
    if cutoff is None:
        return None
    boundary_id = int(session.last_processed_message_id or 0)
    return or_(
        InboundMessage.received_at < cutoff,
        and_(
            InboundMessage.received_at == cutoff,
            InboundMessage.id <= boundary_id,
        ),
    )


def _latest_session_customer_message(
    db: Session,
    *,
    company_id: int,
    session: WhatsAppOrderSession,
) -> InboundMessage | None:
    query = select(InboundMessage).where(
        InboundMessage.company_id == company_id,
        InboundMessage.conversation_id == session.conversation_id,
        InboundMessage.direction == "inbound",
        InboundMessage.content_type != "whatsapp_status",
        InboundMessage.received_at >= session.started_at,
    )
    cutoff_condition = _session_cutoff_condition(session)
    if cutoff_condition is not None:
        query = query.where(cutoff_condition)
    return db.scalar(
        query.options(selectinload(InboundMessage.attachments))
        .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
        .limit(1)
    )


def _initial_session_started_at(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    latest_message: InboundMessage,
    idle_hours: int,
) -> datetime:
    """Include a burst of customer messages even if earlier jobs were superseded."""

    latest_at = _aware(latest_message.received_at) or _now()
    previous_session = db.scalar(
        select(WhatsAppOrderSession)
        .where(
            WhatsAppOrderSession.company_id == company_id,
            WhatsAppOrderSession.conversation_id == conversation_id,
        )
        .order_by(WhatsAppOrderSession.id.desc())
        .limit(1)
    )
    query = select(InboundMessage).where(
        InboundMessage.company_id == company_id,
        InboundMessage.conversation_id == conversation_id,
        InboundMessage.direction == "inbound",
        InboundMessage.content_type != "whatsapp_status",
        or_(
            InboundMessage.received_at < latest_at,
            (InboundMessage.received_at == latest_at) & (InboundMessage.id <= latest_message.id),
        ),
    )
    if previous_session:
        boundary_at = _aware(previous_session.last_customer_message_at)
        if boundary_at:
            boundary_id = int(previous_session.last_processed_message_id or 0)
            query = query.where(
                or_(
                    InboundMessage.received_at > boundary_at,
                    (InboundMessage.received_at == boundary_at) & (InboundMessage.id > boundary_id),
                )
            )

    candidates = db.scalars(
        query.order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc()).limit(500)
    ).all()
    start_at = latest_at
    cursor_at = latest_at
    max_gap = timedelta(hours=max(1, idle_hours))
    for candidate in candidates:
        if candidate.id == latest_message.id:
            continue
        candidate_at = _aware(candidate.received_at)
        if not candidate_at or cursor_at - candidate_at > max_gap:
            break
        start_at = candidate_at
        cursor_at = candidate_at
    return start_at


def _late_message_belongs_to_active_session(
    db: Session,
    *,
    session: WhatsAppOrderSession,
    message: InboundMessage,
    latest_message: InboundMessage,
    idle_hours: int,
) -> bool:
    message_at = _aware(message.received_at)
    latest_at = _aware(latest_message.received_at)
    if not message_at or not latest_at or latest_at - message_at > timedelta(hours=max(1, idle_hours)):
        return False
    cutoff_condition = _session_cutoff_condition(session)
    if cutoff_condition is not None:
        cutoff_at = _aware(session.message_cutoff_at)
        if message_at > cutoff_at or (
            message_at == cutoff_at
            and int(message.id) > int(session.last_processed_message_id or 0)
        ):
            return False

    previous_session = db.scalar(
        select(WhatsAppOrderSession)
        .where(
            WhatsAppOrderSession.company_id == session.company_id,
            WhatsAppOrderSession.conversation_id == session.conversation_id,
            WhatsAppOrderSession.id < session.id,
        )
        .order_by(WhatsAppOrderSession.id.desc())
        .limit(1)
    )
    if previous_session:
        boundary_at = _aware(previous_session.last_customer_message_at)
        boundary_id = int(previous_session.last_processed_message_id or 0)
        if boundary_at and (
            message_at < boundary_at
            or (message_at == boundary_at and int(message.id) <= boundary_id)
        ):
            return False
    return True


def _mark_late_message_reconciled(message: InboundMessage | None) -> None:
    if message is None:
        return
    message.status = "received"
    message.processing_step = "whatsapp_agent_reconciled"


def _get_or_create_session(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    latest_message: InboundMessage,
    message_at: datetime,
    idle_hours: int,
    force_new: bool = False,
    message_id: int | None = None,
) -> WhatsAppOrderSession:
    base_key = f"{_ACTIVE_KEY_PREFIX}{conversation_id}"
    session = active_whatsapp_session(
        db, company_id=company_id, conversation_id=conversation_id
    )
    if session and not force_new:
        return session

    now = _aware(message_at) or _now()
    started_at = now if force_new else _initial_session_started_at(
        db,
        company_id=company_id,
        conversation_id=conversation_id,
        latest_message=latest_message,
        idle_hours=idle_hours,
    )
    active_key = f"{base_key}:{message_id}" if force_new and message_id is not None else base_key
    session = WhatsAppOrderSession(
        company_id=company_id,
        conversation_id=conversation_id,
        active_key=active_key,
        status="collecting",
        intent="other",
        revision=0,
        draft_json="{}",
        missing_fields_json="[]",
        started_at=started_at,
        last_customer_message_at=now,
        due_at=now + timedelta(hours=idle_hours),
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(session)
    try:
        db.commit()
        db.refresh(session)
        return session
    except IntegrityError:
        db.rollback()
        session = active_whatsapp_session(
            db, company_id=company_id, conversation_id=conversation_id
        )
        if session:
            return session
        raise


def _message_text(message: InboundMessage) -> str:
    parts = [str(message.original_content or "").strip()]
    for attachment in message.attachments or []:
        extracted = (
            attachment.transcription_text
            or attachment.extracted_text
            or attachment.ocr_text
            or ""
        ).strip()
        if extracted:
            parts.append(extracted)
    return "\n".join(part for part in parts if part)


def _bounded_recent_transcript(
    messages: list[InboundMessage],
    *,
    max_chars: int = _MAX_AGENT_CONTEXT_CHARS,
    quoted_message: InboundMessage | None = None,
) -> str:
    parts: list[str] = []
    customer_messages = [row for row in messages if row.direction == "inbound"]
    for row in customer_messages[-_MAX_AGENT_CONTEXT_MESSAGES:]:
        text = _message_text(row)
        if text:
            parts.append(f"CLIENTE: {text}")
    if quoted_message:
        role = "CLIENTE" if quoted_message.direction == "inbound" else "EMPRESA"
        quoted_text = _message_text(quoted_message).strip()[:1500]
        if quoted_text:
            context_line = f"MENSAJE PREVIO RELACIONADO ({role}): {quoted_text}"
            if parts:
                parts.insert(len(parts) - 1, context_line)
            else:
                parts.append(context_line)
    # Retain the newest turns when the model context budget is reached.
    while len(parts) > 1 and len("\n".join(parts)) > max_chars:
        parts.pop(0)
    transcript = "\n".join(parts)
    if len(transcript) > max_chars:
        # Keep the beginning of a single long document: order PDFs commonly
        # list their line items from the first page onward.
        transcript = transcript[:max_chars]
    return transcript


def _context_exceeds_agent_limit(
    messages: list[InboundMessage],
    *,
    has_order_context: bool = False,
) -> bool:
    customer_messages = [message for message in messages if message.direction == "inbound"]
    exceeds_limit = (
        len(customer_messages) > _MAX_AGENT_CONTEXT_MESSAGES
        or sum(len(_message_text(message)) + len("CLIENTE: ") for message in customer_messages)
        > _MAX_AGENT_CONTEXT_CHARS
    )
    if not exceeds_limit:
        return False
    if has_order_context or any(
        attachment.extracted_text
        for message in customer_messages
        for attachment in message.attachments or []
    ):
        return True
    return bool(
        re.search(
            r"\b(?:pedido|pedir|comprar|compra|producto|productos|unidades|cantidad)\b",
            " ".join(str(message.original_content or "") for message in customer_messages).casefold(),
        )
    )


def _explicit_new_order_request(text: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    return bool(
        re.search(r"\b(?:otro|nuevo|siguiente)\s+pedido\b", normalized)
        or re.search(r"\bpedido\s+(?:aparte|separado|distinto|diferente)\b", normalized)
    )


def _session_transcript(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    session: WhatsAppOrderSession,
    latest_message: InboundMessage,
) -> tuple[str, list[InboundMessage]]:
    latest_at = _aware(latest_message.received_at)
    query = select(InboundMessage).where(
            InboundMessage.company_id == company_id,
            InboundMessage.conversation_id == conversation_id,
            InboundMessage.content_type != "whatsapp_status",
            InboundMessage.received_at >= session.started_at,
    )
    if latest_at:
        query = query.where(
            or_(
                InboundMessage.received_at < latest_at,
                and_(
                    InboundMessage.received_at == latest_at,
                    InboundMessage.id <= latest_message.id,
                ),
            )
        )
    else:
        query = query.where(InboundMessage.id <= latest_message.id)
    cutoff_condition = _session_cutoff_condition(session)
    if cutoff_condition is not None:
        query = query.where(cutoff_condition)
    rows = db.scalars(
        query.options(selectinload(InboundMessage.attachments))
        .order_by(InboundMessage.received_at.asc(), InboundMessage.id.asc())
    ).all()
    messages = list(rows)
    if latest_message.id not in {row.id for row in messages}:
        messages.append(latest_message)
        messages.sort(key=lambda row: (_aware(row.received_at) or _now(), row.id))
    # Retain the complete persisted transcript. The model input has its own
    # limit; the structured draft carries order details across older turns.
    transcript = "\n".join(
        f"{'CLIENTE' if row.direction == 'inbound' else 'EMPRESA'}: {text}"
        for row in messages
        if (text := _message_text(row))
    )
    return transcript, messages


def _customer_context(
    db: Session,
    *,
    company_id: int,
    conversation: Conversation,
    messages: list[InboundMessage],
) -> dict[str, Any] | None:
    inbound = next((row for row in reversed(messages) if row.direction == "inbound"), None)
    phone = inbound.sender if inbound else conversation.external_thread_id
    matched_id = customer_id_from_phone(phone, build_customer_phone_index(db, company_id))
    if matched_id is None:
        # A historical/manual conversation link is not identity evidence for a
        # WhatsApp sender. Do not leak that customer's name into the model.
        if conversation.customer_id is not None:
            conversation.customer_id = None
        return None
    customer = db.scalar(
        select(Customer).where(
            Customer.id == matched_id,
            Customer.company_id == company_id,
            Customer.deleted_at.is_(None),
            Customer.status != "inactive",
        )
    )
    if customer:
        conversation.customer_id = customer.id
        for row in messages:
            if row.direction == "inbound" and row.customer_id is None:
                row.customer_id = customer.id
    if not customer:
        return None
    return {
        "id": f"customer:{customer.id}",
        "name": customer.commercial_name or customer.fiscal_name,
        "verified_by_phone": True,
    }


def _verified_customer_for_conversation(
    db: Session,
    *,
    company_id: int,
    conversation: Conversation,
    messages: list[InboundMessage],
) -> Customer | None:
    context = _customer_context(
        db,
        company_id=company_id,
        conversation=conversation,
        messages=messages,
    )
    if not context:
        return None
    try:
        customer_id = int(str(context.get("id", "")).split(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        return None
    return db.scalar(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.company_id == company_id,
            Customer.deleted_at.is_(None),
            Customer.status != "inactive",
        )
    )


def _product_context(
    db: Session,
    *,
    company_id: int,
    transcript: str,
    draft: dict[str, Any],
) -> list[dict[str, Any]]:
    draft_references: set[str] = set()
    draft_names: set[str] = set()
    draft_terms: list[str] = []
    draft_lines = draft.get("lines", []) if isinstance(draft, dict) else []
    for line in draft_lines[:30] if isinstance(draft_lines, list) else []:
        if not isinstance(line, dict):
            continue
        reference = str(line.get("reference") or "").strip()
        if reference:
            draft_references.add(reference)
        for value in (line.get("product_query"), line.get("product_name")):
            name = str(value or "").strip()
            if name:
                draft_names.add(name)
                draft_terms.extend(
                    re.findall(r"[\w-]{3,}", name, flags=re.UNICODE)[:4]
                )

    recent_terms = re.findall(r"[\w-]{3,}", transcript[-5000:], flags=re.UNICODE)
    prioritized_draft_terms = list(
        dict.fromkeys(term.casefold() for term in draft_terms)
    )[-48:]
    unique_terms = list(
        dict.fromkeys(
            prioritized_draft_terms
            + [term.casefold() for term in recent_terms[-12:]]
        )
    )
    conditions = []
    for term in unique_terms:
        like = f"%{term}%"
        conditions.extend(
            [
                Product.name.ilike(like),
                Product.reference.ilike(like),
                Product.alternative_code.ilike(like),
                ProductAlias.alias.ilike(like),
            ]
        )
    tenant_catalog = (
        Product.company_id == company_id,
        Product.deleted_at.is_(None),
        Product.obsolete.is_(False),
        Product.status != "inactive",
    )
    draft_conditions = []
    for reference in draft_references:
        draft_conditions.extend(
            [
                Product.reference.ilike(reference),
                Product.alternative_code.ilike(reference),
                ProductAlias.alias.ilike(reference),
            ]
        )
    for name in draft_names:
        draft_conditions.extend(
            [Product.name.ilike(name), ProductAlias.alias.ilike(name)]
        )

    products_by_id: dict[int, Product] = {}
    if draft_conditions:
        for product in db.scalars(
            select(Product)
            .outerjoin(ProductAlias, ProductAlias.product_id == Product.id)
            .where(*tenant_catalog, or_(*draft_conditions))
            .distinct()
            .order_by(Product.name.asc(), Product.id.asc())
            .limit(30)
        ).all():
            products_by_id[product.id] = product

    if conditions and len(products_by_id) < 30:
        candidates = db.scalars(
            select(Product)
            .outerjoin(ProductAlias, ProductAlias.product_id == Product.id)
            .where(
                *tenant_catalog,
                Product.id.not_in(products_by_id) if products_by_id else True,
                or_(*conditions),
            )
            .distinct()
            .order_by(Product.name.asc(), Product.id.asc())
            .limit(30 - len(products_by_id))
        ).all()
        products_by_id.update({product.id: product for product in candidates})

    products = list(products_by_id.values())
    company = db.get(Company, company_id)
    currency = str(company.currency or "").strip().upper() if company else ""
    return [
        {
            "id": f"product:{product.id}",
            "reference": product.reference,
            "alternative_code": product.alternative_code,
            "name": product.name,
            "description": str(product.description or "")[:240],
            "brand": product.brand,
            "format": product.format,
            "sale_unit": product.sale_unit,
            "sale_price": product.sale_price,
            "sale_price_currency": currency if product.sale_price is not None else None,
        }
        for product in products
    ]


def _faq_entries(value: str, query: str = "") -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for block in re.split(r"\n\s*\n", str(value or "").strip()):
        question = ""
        answer_lines: list[str] = []
        in_answer = False
        for line in block.splitlines():
            match_question = re.match(r"\s*(?:pregunta|question|q)\s*:\s*(.*)", line, re.I)
            match_answer = re.match(r"\s*(?:respuesta|answer|a)\s*:\s*(.*)", line, re.I)
            if match_question:
                question = match_question.group(1).strip()
                in_answer = False
            elif match_answer:
                answer_lines.append(match_answer.group(1).strip())
                in_answer = True
            elif in_answer and line.strip():
                answer_lines.append(line.strip())
        answer = "\n".join(answer_lines).strip()[:1200]
        if question and answer:
            entries.append({"id": f"faq:{len(entries) + 1}", "question": question[:400], "answer": answer})
    if not query or not entries:
        return entries[:8]
    query_terms = set(re.findall(r"[\wáéíóúñ]{3,}", query.casefold()))
    scored = []
    for entry in entries:
        terms = set(re.findall(r"[\wáéíóúñ]{3,}", entry["question"].casefold()))
        overlap = len(query_terms.intersection(terms))
        if overlap:
            scored.append((overlap, entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in scored[:4]]


def _grounded_product_answer(question: str, products: list[dict[str, Any]], evidence_ids: list[str]) -> str | None:
    normalized = question.casefold()
    product_by_id = {str(item.get("id")): item for item in products}
    candidates = [product_by_id[item] for item in evidence_ids if item in product_by_id]
    candidates = [
        product for product in candidates
        if any(
            str(value or "").strip()
            and str(value).strip().casefold() in normalized
            for value in (product.get("name"), product.get("reference"), product.get("alternative_code"))
        )
    ]
    if len(candidates) != 1:
        return None
    product = candidates[0]
    if re.search(r"\b(stock|disponibilidad|disponible|inventario|existencias|queda|quedan)\b", normalized):
        return None
    name = str(product.get("name") or "el producto")
    unit = str(product.get("sale_unit") or "").strip()
    if re.search(r"\b(precio|cu[aá]nto|cuesta|coste|vale)\b", normalized):
        price = product.get("sale_price")
        currency = str(product.get("sale_price_currency") or "").strip()
        if isinstance(price, (int, float)) and not isinstance(price, bool):
            return f"El precio registrado de {name} es {price:g} {currency}".strip() + (f" por {unit}." if unit else ".")
        return None
    if re.search(r"\b(formato|medida|unidad)\b", normalized):
        facts = [str(product.get(key) or "").strip() for key in ("format", "sale_unit")]
        facts = [fact for fact in facts if fact]
        if facts:
            return f"En el catálogo, {name} figura con: {', '.join(dict.fromkeys(facts))}."
    return None


def _grounded_faq_answer(entries: list[dict[str, str]], evidence_ids: list[str]) -> str | None:
    by_id = {entry["id"]: entry["answer"] for entry in entries}
    cited = [by_id[item] for item in evidence_ids if item in by_id]
    return cited[0] if len(cited) == 1 else None


def _clarification_reply(
    missing: list[str],
    *,
    draft: dict[str, Any] | None = None,
) -> str:
    questions = []
    lines = draft.get("lines") if isinstance(draft, dict) else []
    for item in missing[:4]:
        question = str(item).strip().rstrip(".")
        if not question:
            continue
        unknown_product = re.match(
            r"verificar el producto\s+[\u00ab\"]?(.*?)[\u00bb\"]?\s+con el equipo$",
            question,
            flags=re.IGNORECASE,
        )
        if unknown_product:
            product_name = unknown_product.group(1).strip()
            question = (
                f"No localizo \u00ab{product_name}\u00bb en el cat\u00e1logo. "
                "\u00bfPuedes indicarme su referencia o el modelo para identificarlo?"
            )
        quantity_line = re.search(r"cantidad\s+de\s+la\s+l[i\u00ed]nea\s+(\d+)", question, flags=re.I)
        if quantity_line and isinstance(lines, list):
            line_index = int(quantity_line.group(1)) - 1
            if 0 <= line_index < len(lines) and isinstance(lines[line_index], dict):
                line = lines[line_index]
                product = str(
                    line.get("product_name") or line.get("product_query") or line.get("reference") or ""
                ).strip()
                unit = str(line.get("unit") or "").strip()
                if product:
                    question = (
                        f"\u00bfQu\u00e9 cantidad de {product} ({unit}) necesitas?"
                        if unit
                        else f"\u00bfQu\u00e9 cantidad de {product} necesitas?"
                    )
        questions.append(question)
    if not questions:
        return "¿Puedes confirmar los datos del pedido para que pueda dejarlo preparado?"
    return "Para preparar bien el pedido necesito confirmar: " + "; ".join(questions) + "."


_NUMBER_WORDS = {
    1: {"un", "uno", "una"},
    2: {"dos", "un par"},
    3: {"tres"},
    4: {"cuatro"},
    5: {"cinco"},
    6: {"seis", "media docena"},
    7: {"siete"},
    8: {"ocho"},
    9: {"nueve"},
    10: {"diez"},
    11: {"once"},
    12: {"doce", "una docena"},
    13: {"trece"},
    14: {"catorce"},
    15: {"quince"},
    16: {"dieciseis"},
    17: {"diecisiete"},
    18: {"dieciocho"},
    19: {"diecinueve"},
    20: {"veinte"},
    21: {"veintiun", "veintiuno", "veintiuna"},
    22: {"veintidos"},
    23: {"veintitres"},
    24: {"veinticuatro"},
    25: {"veinticinco"},
    26: {"veintiseis"},
    27: {"veintisiete"},
    28: {"veintiocho"},
    29: {"veintinueve"},
}
_TENS_NUMBER_WORDS = {
    30: "treinta",
    40: "cuarenta",
    50: "cincuenta",
    60: "sesenta",
    70: "setenta",
    80: "ochenta",
    90: "noventa",
}
_NUMBER_WORD_UNITS = re.compile(
    r"^(?:cajas?|bolsas?|unidades?|uds?|paquetes?|packs?|kg|kilos?|gramos?|"
    r"litros?|metros?|rollos?|botes?|garrafas?|sacos?|palets?|docenas?)\b"
)


def _number_word_variants(quantity: int) -> set[str]:
    if quantity in _NUMBER_WORDS:
        return _NUMBER_WORDS[quantity]
    tens, units = divmod(quantity, 10)
    tens_word = _TENS_NUMBER_WORDS.get(tens * 10)
    if not tens_word or not units:
        return set()
    if units == 1:
        return {
            f"{tens_word} y un",
            f"{tens_word} y uno",
            f"{tens_word} y una",
        }
    unit_word = next(
        (candidate for candidate in _NUMBER_WORDS.get(units, set()) if " " not in candidate),
        "",
    )
    return {f"{tens_word} y {unit_word}"} if unit_word else set()


def _is_standalone_quantity_reply(quantity: int | float, text: str) -> bool:
    """Accept a short numeric answer only when a preceding bot question anchors it."""

    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return False

    if float(quantity).is_integer():
        value = str(int(quantity))
        values = [rf"{re.escape(value)}(?:[.,]0+)?"]
        values.extend(
            r"\s+".join(re.escape(part) for part in phrase.split())
            for phrase in _number_word_variants(int(quantity))
        )
    else:
        value = f"{float(quantity):g}"
        values = [re.escape(value).replace(r"\.", r"[.,]")]

    unit = (
        r"(?:cajas?|bolsas?|unidades?|uds?|paquetes?|packs?|kg|kilos?|gramos?|"
        r"litros?|metros?|rollos?|botes?|garrafas?|sacos?|palets?|docenas?)"
    )
    prefix = (
        r"(?:(?:si|vale|ok(?:ay)?|perfecto|de acuerdo|seria|serian|son|seran|"
        r"pues|entonces|quiero|necesito|ponme|pon|me pones)\s*,?\s*)*"
    )
    pattern = rf"{prefix}(?:{'|'.join(values)})(?:\s+{unit})?[.!\s]*"
    return bool(re.fullmatch(pattern, normalized))


def _has_local_quantity_context(
    text: str,
    start: int,
    end: int,
    product_markers: list[str],
) -> bool:
    if re.match(r"\s*[:/]\s*\d", text[end:]):
        return False
    raw_tail = text[end:].lstrip(" \t,.:;-")
    if re.match(r"^(?:€|eur(?:os?)?|dolares?|usd|gbp)(?:\b|$)", raw_tail):
        return False
    if re.match(r"^por\s+", raw_tail) and re.search(
        r"\b(?:precio|cuesta|coste|vale|importe)\b",
        text[max(0, start - 80):start],
    ):
        return False
    tail = raw_tail
    tail = re.sub(r"^(?:(?:x|por|de|del)\s+)?(?:(?:el|la|los|las)\s+)?", "", tail)
    if _NUMBER_WORD_UNITS.match(tail):
        return True
    if any(tail.startswith(marker) for marker in product_markers):
        return True

    prefix = text[:start].rstrip(" \t,.:;()[]{}")
    prefix = re.sub(r"(?:\s+|\b)(?:x|de|por)\s*$", "", prefix)
    return any(prefix.endswith(marker) for marker in product_markers)


def _quantity_appears_in_text(
    quantity: int | float,
    text: str,
    *,
    product_markers: list[str] | None = None,
) -> bool:
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    marker_values = []
    for marker in product_markers or []:
        clean_marker = unicodedata.normalize("NFKD", str(marker or "").casefold())
        clean_marker = "".join(char for char in clean_marker if not unicodedata.combining(char)).strip()
        if clean_marker:
            marker_values.append(clean_marker)
    number_pattern = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)?(?![\w-])")
    for match in number_pattern.finditer(normalized):
        raw_number = match.group()
        try:
            if "." in raw_number and len(raw_number.rsplit(".", 1)[1]) == 3:
                detected_quantity = float(raw_number.replace(".", ""))
            else:
                detected_quantity = float(raw_number.replace(",", "."))
        except ValueError:
            continue
        if detected_quantity == float(quantity) and _has_local_quantity_context(
            normalized,
            match.start(),
            match.end(),
            marker_values,
        ):
            return True
    if float(quantity).is_integer():
        for phrase in sorted(_number_word_variants(int(quantity)), key=len, reverse=True):
            pattern = r"(?<![\w-])" + r"\s+".join(re.escape(part) for part in phrase.split()) + r"(?![\w-])"
            for match in re.finditer(pattern, normalized):
                if _has_local_quantity_context(
                    normalized,
                    match.start(),
                    match.end(),
                    marker_values,
                ):
                    return True
    return False


def _line_source_message_ids(
    *,
    query: str,
    reference: str,
    product_name: str,
    quantity: int | float | None,
    messages: list[InboundMessage],
    line_index: int | None = None,
    allow_cross_message_pairing: bool = False,
    order_quantities: list[int | float] | None = None,
    catalog_products: list[dict[str, Any]] | None = None,
) -> list[int]:
    if quantity is None:
        return []
    product_terms = {
        token.casefold()
        for value in (query, reference, product_name)
        for token in re.findall(r"[\w-]{3,}", str(value or ""), flags=re.UNICODE)
    }
    distinctive_terms = product_terms - {
        "caja", "cajas", "bolsa", "bolsas", "producto", "productos",
        "unidad", "unidades", "uds", "ud", "paquete", "paquetes", "pack",
    }
    product_phrases = [
        str(value).strip().casefold()
        for value in (query, product_name, reference)
        if str(value or "").strip()
    ]
    product_ids: set[int] = set()
    quantity_ids: set[int] = set()
    paired_ids: set[int] = set()
    for message in messages:
        if message.direction != "inbound":
            continue
        text = _message_text(message).casefold()
        tokens = set(re.findall(r"[\w-]{3,}", text, flags=re.UNICODE))
        if (reference and reference.casefold() in text) or product_terms.intersection(tokens):
            product_ids.add(message.id)
        if _quantity_appears_in_text(
            quantity,
            text,
            product_markers=product_phrases,
        ):
            quantity_ids.add(message.id)

        def supports_product(segment: str) -> bool:
            segment_tokens = set(re.findall(r"[\w-]{3,}", segment, flags=re.UNICODE))
            return bool(
                any(phrase in segment for phrase in product_phrases)
                or (reference and reference.casefold() in segment)
                or distinctive_terms.intersection(segment_tokens)
            )

        def supports_any_catalog_product(segment: str) -> bool:
            segment_tokens = set(re.findall(r"[\w-]{3,}", segment, flags=re.UNICODE))
            for catalog_product in catalog_products or []:
                phrases = [
                    str(catalog_product.get(key) or "").strip().casefold()
                    for key in ("name", "reference", "alternative_code")
                    if catalog_product.get(key)
                ]
                terms = {
                    token
                    for phrase in phrases
                    for token in re.findall(r"[\w-]{3,}", phrase, flags=re.UNICODE)
                } - {
                    "caja", "cajas", "bolsa", "bolsas", "producto", "productos",
                    "unidad", "unidades", "uds", "ud", "paquete", "paquetes", "pack",
                }
                if any(phrase in segment for phrase in phrases) or terms.intersection(segment_tokens):
                    return True
            return False

        def has_any_order_quantity(segment: str) -> bool:
            return any(
                _quantity_appears_in_text(
                    order_quantity,
                    segment,
                    product_markers=[
                        str(catalog_product.get(key) or "")
                        for catalog_product in catalog_products or []
                        for key in ("name", "reference", "alternative_code")
                    ],
                )
                for order_quantity in (order_quantities or [quantity])
                if isinstance(order_quantity, (int, float)) and not isinstance(order_quantity, bool)
            )

        # Split separate line items before checking whether the product and
        # quantity occur together. Keep "Caja Eco, 4 unidades" intact, but
        # split "4 cajas Eco, 2 cajas Kraft" into independently evidenced lines.
        segments: list[str] = []
        for sentence in re.split(r"[\n;]+|\s+\b(?:y|ademas)\b\s+", text):
            parts = sentence.split(",")
            current = parts[0]
            for part in parts[1:]:
                if (
                    supports_any_catalog_product(current)
                    and has_any_order_quantity(current)
                    and supports_any_catalog_product(part)
                    and has_any_order_quantity(part)
                ):
                    segments.append(current)
                    current = part
                else:
                    current = f"{current}, {part}"
            segments.append(current)
        if any(
            supports_product(segment)
            and _quantity_appears_in_text(
                quantity,
                segment,
                product_markers=product_phrases,
            )
            for segment in segments
        ):
            paired_ids.add(message.id)

    if paired_ids:
        return sorted(paired_ids)

    # A short quantity such as "4" has no product evidence by itself. Attach
    # it only if it answers the latest bot question, that question names this
    # product and asks for its quantity, and it names no other catalog item.
    latest_customer_index = max(
        (index for index, message in enumerate(messages) if message.direction == "inbound"),
        default=-1,
    )
    if latest_customer_index >= 0 and _is_standalone_quantity_reply(
        quantity,
        _message_text(messages[latest_customer_index]),
    ):
        previous_customer_index = max(
            (
                index
                for index, message in enumerate(messages[:latest_customer_index])
                if message.direction == "inbound"
            ),
            default=-1,
        )
        preceding_bot_messages = [
            message
            for message in messages[previous_customer_index + 1:latest_customer_index]
            if message.direction == "outbound"
        ]
        clarification = _message_text(preceding_bot_messages[-1]) if preceding_bot_messages else ""

        def normalize_evidence_text(value: str) -> str:
            normalized_value = unicodedata.normalize("NFKD", str(value or "").casefold())
            return "".join(char for char in normalized_value if not unicodedata.combining(char))

        normalized_clarification = normalize_evidence_text(clarification)
        asks_quantity = bool(
            re.search(r"\b(?:cantidad|cuant[oa]s?|unidades?)\b", normalized_clarification)
        )
        names_this_product = any(
            normalize_evidence_text(phrase) in normalized_clarification
            for phrase in product_phrases
            if phrase
        )
        numbered_quantity_targets = re.findall(
            r"\bcantidad\s+de\s+la\s+linea\s+(\d+)\b",
            normalized_clarification,
        )
        targets_this_line = bool(
            line_index is not None
            and len(numbered_quantity_targets) == 1
            and int(numbered_quantity_targets[0]) == line_index
        )
        names_another_product = False
        for catalog_product in catalog_products or []:
            catalog_name = normalize_evidence_text(str(catalog_product.get("name") or "").strip())
            catalog_reference = normalize_evidence_text(str(catalog_product.get("reference") or "").strip())
            catalog_alias = normalize_evidence_text(str(catalog_product.get("alternative_code") or "").strip())
            normalized_reference = normalize_evidence_text(reference)
            normalized_product_name = normalize_evidence_text(product_name)
            normalized_query = normalize_evidence_text(query)
            same_product = bool(
                (normalized_reference and normalized_reference in {catalog_reference, catalog_alias})
                or (normalized_product_name and normalized_product_name == catalog_name)
                or (not normalized_product_name and normalized_query and normalized_query == catalog_name)
            )
            if same_product:
                continue
            if any(
                value and value in normalized_clarification
                for value in (catalog_name, catalog_reference, catalog_alias)
            ):
                names_another_product = True
                break
        if (
            asks_quantity
            and not names_another_product
            and (names_this_product or targets_this_line)
            and product_ids
        ):
            return sorted(product_ids | {messages[latest_customer_index].id})

    if allow_cross_message_pairing and product_ids and quantity_ids and catalog_products:
        def normalized_value(value: Any) -> str:
            normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
            return "".join(char for char in normalized if not unicodedata.combining(char)).strip()

        target_values = {
            normalized_value(value)
            for value in (query, reference, product_name)
            if normalized_value(value)
        }
        target_catalog_ids: set[str] = set()
        mentioned_catalog_ids: set[str] = set()
        generic_terms = {
            "caja", "cajas", "bolsa", "bolsas", "producto", "productos",
            "unidad", "unidades", "uds", "ud", "paquete", "paquetes", "pack",
        }
        for catalog_product in catalog_products:
            catalog_values = {
                normalized_value(catalog_product.get(key))
                for key in ("name", "reference", "alternative_code")
                if catalog_product.get(key)
            }
            if not catalog_values:
                continue
            catalog_id = str(
                catalog_product.get("id")
                or catalog_product.get("reference")
                or catalog_product.get("name")
            ).strip()
            if catalog_values.intersection(target_values):
                target_catalog_ids.add(catalog_id)
            distinctive_values = {
                value for value in catalog_values
                if value not in generic_terms and len(value) >= 3
            }
            if not distinctive_values:
                continue
            for message in messages:
                if message.direction != "inbound":
                    continue
                message_text = normalized_value(_message_text(message))
                if any(
                    re.search(rf"(?<!\w){re.escape(value)}(?!\w)", message_text)
                    for value in distinctive_values
                ):
                    mentioned_catalog_ids.add(catalog_id)
                    break

        # Cross-turn association is safe only when the transcript does not
        # mention a different catalog product; otherwise an extractor can
        # accidentally attach a quantity for another line to this one.
        other_product_mentioned = bool(mentioned_catalog_ids - target_catalog_ids)
        if target_catalog_ids and not other_product_mentioned:
            return sorted(product_ids | quantity_ids)
    return []


def _canonicalize_draft(
    draft: dict[str, Any],
    *,
    products: list[dict[str, Any]],
    semantic_missing: list[str],
    intent: str,
    messages: list[InboundMessage] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    clean_draft = {
        "customer_name": str(draft.get("customer_name") or "")[:255],
        "lines": [],
        "notes": str(draft.get("notes") or "")[:1000],
    }
    missing = [str(item).strip()[:240] for item in semantic_missing if str(item).strip()]
    catalog_by_reference: dict[str, list[dict[str, Any]]] = {}
    for product in products:
        for value in (product.get("reference"), product.get("alternative_code")):
            if value:
                key = str(value).strip().casefold()
                matches = catalog_by_reference.setdefault(key, [])
                if not any(str(item.get("id")) == str(product.get("id")) for item in matches):
                    matches.append(product)

    lines = draft.get("lines") if isinstance(draft.get("lines"), list) else []
    order_quantities = [
        line.get("quantity")
        for line in lines[:30]
        if isinstance(line, dict)
        and isinstance(line.get("quantity"), (int, float))
        and not isinstance(line.get("quantity"), bool)
        and line.get("quantity") > 0
    ]
    if len(lines) > 30:
        missing.append(
            "El pedido supera el límite de 30 líneas y necesita una revisión completa del equipo."
        )
    for index, raw_line in enumerate(lines[:30], start=1):
        if not isinstance(raw_line, dict):
            continue
        query = str(raw_line.get("product_query") or "").strip()[:255]
        reference = str(raw_line.get("reference") or "").strip()[:120]
        quantity = raw_line.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
            quantity = None
        unit = str(raw_line.get("unit") or "").strip()[:80]
        reference_matches = catalog_by_reference.get(reference.casefold(), []) if reference else []
        ambiguous_reference = len(reference_matches) > 1
        product = reference_matches[0] if len(reference_matches) == 1 else None
        if not product and query:
            exact_matches = [
                item
                for item in products
                if str(item.get("name") or "").strip().casefold() == query.casefold()
            ]
            if len(exact_matches) == 1:
                product = exact_matches[0]
                reference = str(product.get("reference") or "")
        if product:
            reference = str(product.get("reference") or reference)
            query = str(product.get("name") or query)
            catalog_unit = str(product.get("sale_unit") or "").strip()
            unit = unit or catalog_unit
            if unit and catalog_unit and _unit_key(unit) != _unit_key(catalog_unit):
                missing.append(
                    f"Confirmar la unidad de medida de la l\u00ednea {index} (cat\u00e1logo: {catalog_unit})."
                )
            if not catalog_unit:
                missing.append(f"Confirmar la unidad de medida de la línea {index}.")
        elif reference and ambiguous_reference:
            reference = ""
            missing.append(
                f"La referencia {raw_line.get('reference')} coincide con varios productos; confirma cuál necesitas."
            )
        elif reference:
            # A model-supplied code must match this tenant's retrieved catalog.
            reference = ""
            missing.append(f"Confirmar el producto de la línea {index}.")
        if not query:
            missing.append(f"Indicar el producto de la línea {index}.")
        if quantity is None:
            missing.append(f"Indicar la cantidad de la línea {index}.")
        if query and not product:
            missing.append(f"Verificar el producto «{query}» con el equipo.")
        source_message_ids = (
            _line_source_message_ids(
                query=query,
                reference=reference,
                product_name=str(product.get("name") or "") if product else "",
                quantity=quantity,
                messages=messages,
                line_index=index,
                allow_cross_message_pairing=len(lines) <= 1,
                order_quantities=order_quantities,
                catalog_products=products,
            )
            if messages is not None
            else []
        )
        if messages is not None and not source_message_ids:
            missing.append(f"Verificar producto y cantidad de la l\u00ednea {index} con el cliente.")
        clean_draft["lines"].append(
            {
                "product_query": query,
                "reference": reference,
                "product_name": str(product.get("name") or "") if product else "",
                "quantity": quantity,
                "unit": unit,
                "notes": str(raw_line.get("notes") or "")[:300],
                "source_message_ids": source_message_ids,
            }
        )
    if intent in {"order", "order_question"} and not clean_draft["lines"]:
        missing.append("Indicar los productos y cantidades del pedido.")
    missing = list(dict.fromkeys(item for item in missing if item))
    return clean_draft, missing


def _draft_is_complete(draft: dict[str, Any], missing: list[str]) -> bool:
    lines = draft.get("lines") if isinstance(draft, dict) else []
    return bool(
        lines
        and not missing
        and all(
            line.get("reference")
            and line.get("quantity") is not None
            and line.get("unit")
            for line in lines
        )
    )


def _order_summary_reply(draft: dict[str, Any]) -> str:
    rows = []
    for line in draft.get("lines", []):
        product = str(line.get("product_name") or line.get("product_query") or line.get("reference") or "Producto").strip()
        quantity = line.get("quantity")
        unit = str(line.get("unit") or "unidades").strip()
        if quantity is not None:
            rows.append(f"• {quantity:g} {unit} de {product}")
    if not rows:
        return "Para preparar el pedido necesito confirmar los productos y las cantidades. ¿Me los indicas?"
    return "Tengo anotado:\n" + "\n".join(rows) + "\n¿Es correcto? Si falta algo, dímelo y lo actualizo."


def _has_unreadable_or_pending_media(messages: list[InboundMessage]) -> tuple[str | None, str | None]:
    for message in messages:
        for attachment in message.attachments or []:
            status = str(attachment.extraction_status or "pending").lower()
            if status in _MEDIA_PENDING:
                return "pending", str(attachment.filename or "Adjunto")
            if status in _MEDIA_UNREADABLE:
                return "unreadable", str(attachment.filename or "Adjunto")
    return None, None


def _log_agent_event(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    session_id: int,
    event: str,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    log_flow_event(
        db,
        company_id=company_id,
        event=f"whatsapp.agent.{event}",
        stage="whatsapp_agent",
        message=f"Agente conversacional WhatsApp: {event}.",
        entity_type="whatsapp_order_session",
        entity_id=session_id,
        status=status,
        metadata={"conversation_id": conversation_id, **(metadata or {})},
    )


def _finalization_dedupe_key(db: Session, session: WhatsAppOrderSession) -> str:
    base = f"wa-session-finalize:{session.id}:r{session.revision}"
    candidate = session.finalization_job_key or base
    existing = db.scalar(
        select(BackgroundJob).where(
            BackgroundJob.company_id == session.company_id,
            BackgroundJob.job_type == "whatsapp_finalize_session",
            BackgroundJob.dedupe_key == candidate,
        )
    )
    if existing and existing.status not in _ACTIVE_JOB_STATES:
        return f"{base}:retry-after-{existing.id}"
    return candidate


def _set_handoff(
    db: Session,
    *,
    session: WhatsAppOrderSession,
    conversation: Conversation,
    reason: str,
    active: bool = False,
    trigger_message_id: int | None = None,
) -> None:
    session.status = "human_handoff"
    session.handoff_reason = reason[:255]
    session.handoff_at = _now()
    session.human_active = bool(active)
    conversation.status = "human_owned" if active else "open"
    if not active and trigger_message_id:
        existing_alert = db.scalar(
            select(Alert).where(
                Alert.company_id == session.company_id,
                Alert.inbound_message_id == trigger_message_id,
                Alert.alert_type == "whatsapp_agent_handoff",
                Alert.status == "open",
            )
        )
        if not existing_alert:
            db.add(
                Alert(
                    company_id=session.company_id,
                    inbound_message_id=trigger_message_id,
                    alert_type="whatsapp_agent_handoff",
                    severity="high",
                    status="open",
                    title="Conversación de WhatsApp requiere atención",
                    message=reason[:1000],
                    payload_json=json.dumps(
                        {
                            "conversation_id": conversation.id,
                            "session_id": session.id,
                        },
                        ensure_ascii=False,
                    ),
                )
            )


def _finish_pending_customer_cancellation(
    db: Session,
    *,
    session: WhatsAppOrderSession,
    conversation: Conversation,
    inbound_message: InboundMessage,
) -> dict[str, Any] | None:
    marker = session.closed_reason
    if marker == _CANCEL_REPLY_PENDING:
        session.status = "closed_no_order"
        session.active_key = None
        session.closed_reason = "customer_cancelled"
        session.closed_at = _now()
        session.human_active = False
        session.handoff_reason = None
        conversation.status = "open"
        conversation.assigned_user_id = None
        inbound_message.processing_step = "whatsapp_agent_order_cancelled"
        event = "order_cancelled"
        status = "closed_no_order"
    elif marker == _CANCEL_HANDOFF_REPLY_PENDING:
        reason = "El cliente solicita cancelar un pedido ya registrado; requiere revisión humana."
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason=reason,
            trigger_message_id=inbound_message.id,
        )
        session.closed_reason = "customer_cancelled_existing_order"
        conversation.whatsapp_bot_enabled = False
        conversation.updated_at = _now()
        inbound_message.processing_step = "whatsapp_agent_cancel_handoff"
        event = "order_cancellation_handoff"
        status = "human_handoff"
    else:
        return None

    session.updated_at = _now()
    db.commit()
    _log_agent_event(
        db,
        company_id=session.company_id,
        conversation_id=conversation.id,
        session_id=session.id,
        event=event,
        status=status,
        metadata={"trigger_message_id": inbound_message.id},
    )
    return {"status": status, "event": event}


def _send_pending_customer_cancellation_reply(
    db: Session,
    *,
    company_id: int,
    conversation: Conversation,
    session: WhatsAppOrderSession,
    inbound_message: InboundMessage,
    config: Any,
) -> dict[str, Any]:
    marker = session.closed_reason
    if marker not in {_CANCEL_REPLY_PENDING, _CANCEL_HANDOFF_REPLY_PENDING}:
        return {"ok": True, "skipped": True, "reason": "no_cancellation_reply_pending"}

    window = whatsapp_response_window_status(
        db,
        company_id=company_id,
        conversation_id=conversation.id,
        response_window_minutes=config.response_window_minutes,
    )
    if not window.get("is_open"):
        result = {"sent": False, "reason": str(window.get("reason") or "response_window_closed")}
        transition = _finish_pending_customer_cancellation(
            db,
            session=session,
            conversation=conversation,
            inbound_message=inbound_message,
        )
        return {
            "ok": True,
            "status": (transition or {}).get("status", session.status),
            "session_id": session.id,
            "auto_response": result,
        }

    try:
        send_result = await_send_automatic_response(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            trigger_message_id=inbound_message.id,
            body=str(session.suggested_reply or ""),
            semantic_state=(
                "human_handoff"
                if marker == _CANCEL_HANDOFF_REPLY_PENDING
                else "informational"
            ),
            assistant_session_id=session.id,
            draft_revision=session.revision,
        )
    except Exception as exc:  # noqa: BLE001
        error_type = str(getattr(exc, "error_type", None) or type(exc).__name__)
        retryable = error_type == "rate_limited"
        if retryable:
            inbound_message.processing_step = "whatsapp_agent_reply_retryable"
            db.commit()
            raise WhatsAppAgentReplyDeliveryError(error_type, retryable=True) from exc
        transition = _finish_pending_customer_cancellation(
            db,
            session=session,
            conversation=conversation,
            inbound_message=inbound_message,
        )
        return {
            "ok": True,
            "status": (transition or {}).get("status", session.status),
            "session_id": session.id,
            "auto_response": {"sent": False, "reason": error_type},
        }

    delivered = bool(send_result.get("sent"))
    if not delivered and send_result.get("reason") == "already_replied":
        outbound = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == company_id,
                InboundMessage.conversation_id == conversation.id,
                InboundMessage.direction == "outbound",
                InboundMessage.source_message_id == f"auto:{inbound_message.id}",
            )
        )
        delivered = bool(
            outbound
            and str(outbound.status or "").lower()
            in {"accepted", "sent", "delivered", "read"}
        )

    transition = _finish_pending_customer_cancellation(
        db,
        session=session,
        conversation=conversation,
        inbound_message=inbound_message,
    )
    if not delivered:
        logger.warning(
            "WhatsApp cancellation reply not sent session=%s reason=%s",
            session.id,
            str(send_result.get("reason") or "reply_not_sent"),
        )
    return {
        "ok": True,
        "status": (transition or {}).get("status", session.status),
        "session_id": session.id,
        "auto_response": send_result,
    }


def _resume_pending_customer_cancellation(
    db: Session,
    *,
    company_id: int,
    conversation: Conversation,
    session: WhatsAppOrderSession,
    inbound_message: InboundMessage,
    config: Any,
) -> dict[str, Any] | None:
    if session.closed_reason not in {
        _CANCEL_REPLY_PENDING,
        _CANCEL_HANDOFF_REPLY_PENDING,
    }:
        return None

    outbound = db.scalar(
        select(InboundMessage).where(
            InboundMessage.company_id == company_id,
            InboundMessage.conversation_id == conversation.id,
            InboundMessage.direction == "outbound",
            InboundMessage.source_message_id == f"auto:{inbound_message.id}",
        )
    )
    if outbound and outbound.status == "failed" and outbound.processing_step == WHATSAPP_RETRYABLE_AUTO_SEND_STEP:
        inbound_message.processing_step = "whatsapp_agent_reply_retryable"
        db.commit()
        return None
    if outbound is None:
        return _send_pending_customer_cancellation_reply(
            db,
            company_id=company_id,
            conversation=conversation,
            session=session,
            inbound_message=inbound_message,
            config=config,
        )

    delivered = str(outbound.status or "").lower() in {"accepted", "sent", "delivered", "read"}
    if outbound.status in {"sending", "send_unknown"}:
        # The provider outcome is ambiguous; never replay a possibly delivered
        # cancellation acknowledgement. The requested safe state transition
        # still completes and the outbound incident remains visible in the log.
        delivered = False
    transition = _finish_pending_customer_cancellation(
        db,
        session=session,
        conversation=conversation,
        inbound_message=inbound_message,
    )
    return {
        "ok": True,
        "status": (transition or {}).get("status", session.status),
        "session_id": session.id,
        "auto_response": {"sent": delivered, "reason": "recovered_previous_attempt"},
    }


def _handle_explicit_order_cancellation(
    db: Session,
    *,
    company_id: int,
    conversation: Conversation,
    session: WhatsAppOrderSession,
    inbound_message: InboundMessage,
    config: Any,
) -> dict[str, Any] | None:
    if not has_explicit_order_cancellation(inbound_message.original_content):
        return None

    draft = _json_object(session.draft_json, {})
    lines = draft.get("lines", []) if isinstance(draft, dict) else []
    has_active_draft = bool(
        isinstance(lines, list)
        and any(isinstance(line, dict) and line for line in lines)
        and session.order_id is None
        and session.status not in {"closed_review", "finalizing"}
    )
    persisted_order = None
    if not has_active_draft and session.order_id is None:
        persisted_order = db.scalar(
            select(Order)
            .where(
                Order.company_id == company_id,
                Order.conversation_id == conversation.id,
                Order.deleted_at.is_(None),
            )
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(1)
        )
    requires_handoff = bool(
        session.order_id is not None
        or session.status in {"closed_review", "finalizing"}
        or persisted_order is not None
    )
    marker = _CANCEL_HANDOFF_REPLY_PENDING if requires_handoff else _CANCEL_REPLY_PENDING
    if requires_handoff:
        reply = (
            "Ese pedido ya está registrado; no lo voy a cancelar automáticamente. "
            "Lo dejo al equipo para que lo revise y te confirme."
        )
    elif has_active_draft:
        reply = (
            "He cancelado el borrador de este pedido; no se enviará para su tramitación. "
            "Si quieres empezar otro, dime qué necesitas."
        )
    else:
        reply = (
            "No veo un pedido en curso que cancelar. Si te refieres a uno ya enviado, "
            "lo revisará el equipo antes de cambiarlo."
        )

    now = _now()
    session.revision = int(session.revision or 0) + 1
    session.last_processed_message_id = inbound_message.id
    session.last_customer_message_at = _aware(inbound_message.received_at) or now
    session.due_at = session.last_customer_message_at + timedelta(
        hours=config.idle_session_hours
    )
    session.finalization_job_key = None
    session.suggested_reply = reply
    session.last_error = None
    session.closed_reason = marker
    session.updated_at = now
    if requires_handoff:
        # Keep the active key and avoid marking the handoff until the prepared
        # acknowledgement is delivered (or its delivery outcome is known).
        if session.status in {"closed_review", "finalizing"}:
            session.status = "collecting"
    else:
        session.intent = "cancelled"
        session.status = "closed_no_order"
        session.closed_at = None
        session.human_active = False
    session.snapshot_json = json.dumps(
        {
            "reason": "customer_requested_cancellation",
            "cancellation_message_id": inbound_message.id,
            "draft": draft,
            "missing_or_uncertain": _json_object(session.missing_fields_json, []),
            "existing_order_id": (
                int(session.order_id) if session.order_id is not None
                else int(persisted_order.id) if persisted_order is not None
                else None
            ),
        },
        ensure_ascii=False,
    )
    inbound_message.status = "received"
    inbound_message.processing_step = "whatsapp_agent_cancel_reply_pending"
    db.commit()
    _log_agent_event(
        db,
        company_id=company_id,
        conversation_id=conversation.id,
        session_id=session.id,
        event="order_cancellation_requested",
        status="handoff_pending" if requires_handoff else "cancel_pending",
        metadata={"trigger_message_id": inbound_message.id},
    )
    return _send_pending_customer_cancellation_reply(
        db,
        company_id=company_id,
        conversation=conversation,
        session=session,
        inbound_message=inbound_message,
        config=config,
    )


def set_whatsapp_human_control(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    active: bool,
    user_id: int | None = None,
) -> WhatsAppOrderSession | None:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.company_id == company_id,
        )
    )
    session = active_whatsapp_session(
        db, company_id=company_id, conversation_id=conversation_id
    )
    if not conversation:
        return None
    if not active:
        set_whatsapp_bot_enabled(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
            enabled=True,
        )
        return active_whatsapp_session(
            db, company_id=company_id, conversation_id=conversation_id
        )

    conversation.whatsapp_bot_enabled = False
    conversation.status = "human_owned"
    if session:
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason="Atención asumida por un agente.",
            active=True,
        )
        session.updated_at = _now()
    if user_id:
        conversation.assigned_user_id = user_id
    conversation.updated_at = _now()
    db.commit()
    if session:
        db.refresh(session)
    return session


def set_whatsapp_bot_enabled(
    db: Session,
    *,
    company_id: int,
    conversation_id: int,
    enabled: bool,
) -> Conversation | None:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.company_id == company_id,
        )
    )
    if not conversation:
        return None
    conversation.whatsapp_bot_enabled = bool(enabled)
    if enabled:
        if conversation.status == "human_owned":
            conversation.status = "open"
            conversation.assigned_user_id = None
        session = active_whatsapp_session(
            db,
            company_id=company_id,
            conversation_id=conversation_id,
        )
        if session and (session.human_active or session.status == "human_handoff"):
            session.human_active = False
            session.handoff_reason = None
            session.handoff_at = None
            session.status = "ready_for_review" if _draft_is_complete(
                _json_object(session.draft_json, {}),
                _json_object(session.missing_fields_json, []),
            ) else "collecting"
            last_inbound = _latest_customer_message(
                db, company_id=company_id, conversation_id=conversation_id
            )
            if last_inbound:
                config = whatsapp_config(db, company_id)
                session.last_customer_message_at = _aware(last_inbound.received_at) or _now()
                session.due_at = session.last_customer_message_at + timedelta(
                    hours=config.idle_session_hours
                )
                session.revision = int(session.revision or 0) + 1
                session.finalization_job_key = None
            session.updated_at = _now()
    conversation.updated_at = _now()
    db.commit()
    db.refresh(conversation)
    return conversation


def enqueue_due_whatsapp_session_finalizations(
    db: Session,
    *,
    company_id: int,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    current = _aware(now) or _now()
    sessions = db.scalars(
        select(WhatsAppOrderSession)
        .join(Conversation, Conversation.id == WhatsAppOrderSession.conversation_id)
        .where(
            WhatsAppOrderSession.company_id == company_id,
            Conversation.company_id == company_id,
            Conversation.whatsapp_bot_enabled.is_(True),
            WhatsAppOrderSession.active_key.is_not(None),
            WhatsAppOrderSession.status.in_(
                ["collecting", "awaiting_customer", "ready_for_review"]
            ),
            WhatsAppOrderSession.human_active.is_(False),
            WhatsAppOrderSession.due_at <= current,
        )
        .order_by(WhatsAppOrderSession.due_at.asc(), WhatsAppOrderSession.id.asc())
        .limit(max(1, min(limit, 200)))
    ).all()
    queued = 0
    for session in sessions:
        key = _finalization_dedupe_key(db, session)
        existing = db.scalar(
            select(BackgroundJob).where(
                BackgroundJob.company_id == company_id,
                BackgroundJob.job_type == "whatsapp_finalize_session",
                BackgroundJob.dedupe_key == key,
            )
        )
        if existing and existing.status in _ACTIVE_JOB_STATES:
            session.finalization_job_key = key
            continue
        job = enqueue_job(
            db,
            company_id=company_id,
            job_type="whatsapp_finalize_session",
            payload={
                "session_id": session.id,
                "expected_revision": session.revision,
                "reason": "idle_timeout",
            },
            dedupe_key=key,
            max_retries=5,
        )
        session.finalization_job_key = key
        session.updated_at = current
        db.commit()
        if job.status in {"queued", "retrying", "running", "success"}:
            queued += 1
    return queued


def _build_final_transcript(messages: list[InboundMessage]) -> str:
    parts: list[str] = []
    for message in messages:
        text = _message_text(message)
        if text:
            role = "CLIENTE" if message.direction == "inbound" else "EMPRESA"
            parts.append(f"{role}: {text}")
    return "\n".join(parts)


def finalize_whatsapp_order_session(
    db: Session,
    *,
    company_id: int,
    session_id: int,
    expected_revision: int | None,
    reason: str,
    user_id: int | None = None,
) -> dict[str, Any]:
    session = db.scalar(
        select(WhatsAppOrderSession)
        .where(
            WhatsAppOrderSession.id == session_id,
            WhatsAppOrderSession.company_id == company_id,
        )
        .with_for_update()
    )
    if not session:
        return {"ok": True, "skipped": True, "reason": "session_not_found"}
    if session.status in {"closed_review", "closed_no_order"}:
        return {"ok": True, "skipped": True, "reason": "already_closed", "order_id": session.order_id}
    if session.status == "human_handoff" and reason != "manual":
        return {"ok": True, "skipped": True, "reason": "human_handoff"}
    if session.human_active and reason != "manual":
        return {"ok": True, "skipped": True, "reason": "human_active"}
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == session.conversation_id,
            Conversation.company_id == company_id,
        )
    )
    if not conversation:
        return {"ok": False, "message": "No se encontró la conversación WhatsApp."}

    if reason != "manual" and not conversation.whatsapp_bot_enabled:
        if session.status == "finalizing":
            session.status = "ready_for_review" if _draft_is_complete(
                _json_object(session.draft_json, {}),
                _json_object(session.missing_fields_json, []),
            ) else "awaiting_customer"
        session.finalization_job_key = None
        session.updated_at = _now()
        db.commit()
        return {"ok": True, "skipped": True, "reason": "bot_disabled"}

    latest_message = _latest_session_customer_message(
        db, company_id=company_id, session=session
    )
    current = _now()
    expected_revision = int(expected_revision if expected_revision is not None else session.revision)
    if latest_message:
        latest_at = _aware(latest_message.received_at) or current
        persisted_at = _aware(session.last_customer_message_at) or session.started_at
        latest_turn_unprocessed = session.last_processed_message_id != latest_message.id
        if (
            latest_at > persisted_at
            or expected_revision != int(session.revision or 0)
            or latest_turn_unprocessed
        ):
            config = whatsapp_config(db, company_id)
            session.last_customer_message_at = latest_at
            session.due_at = latest_at + timedelta(hours=config.idle_session_hours)
            session.revision = max(int(session.revision or 0), expected_revision) + 1
            session.status = "collecting" if session.status != "human_handoff" else "human_handoff"
            session.finalization_job_key = None
            session.updated_at = current
            db.commit()
            _log_agent_event(
                db,
                company_id=company_id,
                conversation_id=session.conversation_id,
                session_id=session.id,
                event="timer_reset",
                status="skipped",
                metadata={"revision": session.revision},
            )
            return {"ok": True, "skipped": True, "reason": "customer_message_after_job", "due_at": session.due_at.isoformat()}
        due_at = _aware(session.due_at)
        if reason == "idle_timeout" and due_at and due_at > current:
            return {"ok": True, "skipped": True, "reason": "not_due", "due_at": due_at.isoformat()}
    if session.status == "finalizing" and session.finalization_job_key is None:
        return {"ok": True, "skipped": True, "reason": "finalization_already_claimed"}
    if latest_message is None:
        session.status = "closed_no_order"
        session.active_key = None
        session.closed_reason = reason
        session.closed_at = current
        session.updated_at = current
        session.human_active = False
        conversation.status = "open"
        conversation.assigned_user_id = None
        db.commit()
        return {"ok": True, "status": session.status}

    transcript, messages = _session_transcript(
        db,
        company_id=company_id,
        conversation_id=session.conversation_id,
        session=session,
        latest_message=latest_message,
    )
    media_state, media_filename = _has_unreadable_or_pending_media(messages)
    if media_state:
        if media_state == "pending" and reason == "idle_timeout" and session.pending_media_retry_count < 5:
            session.pending_media_retry_count = int(session.pending_media_retry_count or 0) + 1
            session.due_at = current + timedelta(minutes=5)
            session.revision = int(session.revision or 0) + 1
            session.finalization_job_key = None
            session.status = "awaiting_customer"
            session.updated_at = current
            db.commit()
            _log_agent_event(
                db,
                company_id=company_id,
                conversation_id=session.conversation_id,
                session_id=session.id,
                event="media_finalization_deferred",
                status="retrying",
                metadata={"retry_count": session.pending_media_retry_count},
            )
            return {
                "ok": True,
                "skipped": True,
                "reason": "media_processing_pending",
                "retry_count": session.pending_media_retry_count,
            }
        session.status = "closed_review"
        session.active_key = None
        session.closed_reason = "media_processing_failed" if media_state == "pending" else "unreadable_media"
        session.closed_at = current
        session.handoff_reason = (
            f"El adjunto {media_filename} está pendiente de procesar."
            if media_state == "pending"
            else f"No se pudo interpretar el adjunto {media_filename}."
        )
        session.snapshot_json = json.dumps(
            {
                "source_message_ids": [item.id for item in messages],
                "draft": _json_object(session.draft_json, {}),
                "missing_or_uncertain": _json_object(session.missing_fields_json, []),
                "reason": "unreadable_media",
            },
            ensure_ascii=False,
        )
        if media_state == "pending" and latest_message:
            existing_alert = db.scalar(
                select(Alert).where(
                    Alert.company_id == company_id,
                    Alert.inbound_message_id == latest_message.id,
                    Alert.alert_type == "whatsapp_media_processing",
                    Alert.status == "open",
                )
            )
            if not existing_alert:
                db.add(
                    Alert(
                        company_id=company_id,
                        inbound_message_id=latest_message.id,
                        alert_type="whatsapp_media_processing",
                        severity="medium",
                        status="open",
                        title="Adjunto de WhatsApp pendiente de revisión",
                        message="No se pudo finalizar el procesamiento del adjunto. Revisar manualmente antes de tramitar el pedido.",
                        payload_json=json.dumps(
                            {
                                "session_id": session.id,
                                "conversation_id": conversation.id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
        session.human_active = False
        conversation.status = "open"
        conversation.assigned_user_id = None
        session.updated_at = current
        db.commit()
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=session.conversation_id,
            session_id=session.id,
            event="finalize_completed",
            status="review",
            metadata={"reason": "unreadable_media"},
        )
        return {"ok": True, "status": "closed_review", "reason": "unreadable_media"}

    session.pending_media_retry_count = 0

    draft = _json_object(session.draft_json, {})
    missing = _json_object(session.missing_fields_json, [])
    session.snapshot_json = json.dumps(
        {
            "source_message_ids": [item.id for item in messages],
            "draft": draft,
            "missing_or_uncertain": missing,
            "revision": session.revision,
            "last_customer_message_at": (_aware(session.last_customer_message_at) or current).isoformat(),
            "reason": reason,
        },
        ensure_ascii=False,
    )
    session.status = "finalizing"
    session.closed_reason = reason
    session.finalization_job_key = session.finalization_job_key or f"session:{session.id}:revision:{session.revision}"
    session.updated_at = current
    db.commit()

    order_intent = session.intent in {"order", "order_question"} or bool(draft.get("lines"))
    if not order_intent:
        session.status = "closed_no_order"
        session.active_key = None
        session.closed_at = _now()
        session.updated_at = _now()
        session.human_active = False
        conversation.status = "open"
        conversation.assigned_user_id = None
        db.commit()
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=session.conversation_id,
            session_id=session.id,
            event="finalize_completed",
            status="no_order",
            metadata={"reason": reason},
        )
        return {"ok": True, "status": "closed_no_order"}

    if missing or not _draft_is_complete(draft, missing):
        session.status = "closed_review"
        session.active_key = None
        session.closed_reason = "incomplete_order"
        session.closed_at = _now()
        session.human_active = False
        if latest_message:
            existing_alert = db.scalar(
                select(Alert).where(
                    Alert.company_id == company_id,
                    Alert.inbound_message_id == latest_message.id,
                    Alert.alert_type == "whatsapp_order_incomplete",
                    Alert.status == "open",
                )
            )
            if not existing_alert:
                db.add(
                    Alert(
                        company_id=company_id,
                        inbound_message_id=latest_message.id,
                        alert_type="whatsapp_order_incomplete",
                        severity="medium",
                        status="open",
                        title="Pedido de WhatsApp incompleto",
                        message=(
                            "Han pasado 12 horas sin respuesta; el pedido necesita revisión humana antes de tramitarse."
                            if reason == "idle_timeout"
                            else "La conversación se cerró con un pedido incompleto que necesita revisión humana antes de tramitarse."
                        ),
                        payload_json=json.dumps(
                            {
                                "session_id": session.id,
                                "conversation_id": session.conversation_id,
                                "draft": draft,
                                "missing_or_uncertain": missing,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
        session.handoff_reason = "El pedido qued\u00f3 incompleto y necesita revisi\u00f3n humana."
        session.updated_at = _now()
        conversation.status = "open"
        conversation.assigned_user_id = None
        db.commit()
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=session.conversation_id,
            session_id=session.id,
            event="finalize_completed",
            status="incomplete_review",
            metadata={"reason": reason, "missing_count": len(missing)},
        )
        return {"ok": True, "status": "closed_review", "reason": "incomplete_order"}

    session_inbound_ids = {item.id for item in messages if item.direction == "inbound"}
    if any(
        not isinstance(line, dict)
        or not set(line.get("source_message_ids") or []).intersection(session_inbound_ids)
        for line in draft.get("lines", [])
    ):
        session.status = "closed_review"
        session.active_key = None
        session.closed_reason = "order_evidence_unverified"
        session.closed_at = _now()
        session.human_active = False
        session.handoff_reason = "Falta vincular alguna línea del pedido a mensajes originales del cliente."
        session.updated_at = _now()
        db.add(
            Alert(
                company_id=company_id,
                inbound_message_id=latest_message.id,
                alert_type="whatsapp_order_evidence",
                severity="medium",
                status="open",
                title="Validar líneas del pedido de WhatsApp",
                message="El borrador no tiene evidencia de origen para todas las líneas. Revisar la conversación antes de crear el pedido.",
                payload_json=json.dumps(
                    {
                        "session_id": session.id,
                        "conversation_id": conversation.id,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        conversation.status = "open"
        conversation.assigned_user_id = None
        db.commit()
        return {"ok": True, "status": "closed_review", "reason": "order_evidence_unverified"}

    verified_customer = _verified_customer_for_conversation(
        db,
        company_id=company_id,
        conversation=conversation,
        messages=messages,
    )
    if verified_customer is None:
        session.status = "closed_review"
        session.active_key = None
        session.closed_reason = "customer_identity_unverified"
        session.closed_at = _now()
        session.human_active = False
        session.handoff_reason = "El número de WhatsApp no coincide con un cliente verificado; confirmar identidad antes de crear el pedido."
        session.updated_at = _now()
        existing_alert = db.scalar(
            select(Alert).where(
                Alert.company_id == company_id,
                Alert.inbound_message_id == latest_message.id,
                Alert.alert_type == "whatsapp_customer_identity",
                Alert.status == "open",
            )
        )
        if not existing_alert:
            db.add(
                Alert(
                    company_id=company_id,
                    inbound_message_id=latest_message.id,
                    alert_type="whatsapp_customer_identity",
                    severity="medium",
                    status="open",
                    title="Verificar cliente de pedido WhatsApp",
                    message="El número de WhatsApp no coincide de forma única con un cliente. Confirmar identidad antes de crear el pedido.",
                    payload_json=json.dumps(
                        {
                            "session_id": session.id,
                            "conversation_id": conversation.id,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        conversation.status = "open"
        conversation.assigned_user_id = None
        db.commit()
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=session.conversation_id,
            session_id=session.id,
            event="finalize_completed",
            status="identity_review",
            metadata={"reason": "customer_identity_unverified"},
        )
        return {"ok": True, "status": "closed_review", "reason": "customer_identity_unverified"}

    try:
        from app.agent.services import AgentProcessingService

        result = AgentProcessingService().pipeline.process_inbound_message(
            db,
            latest_message,
            user=None,
            force_order=bool(draft.get("lines")),
            source_text_override=(
                "Pedido de WhatsApp validado. "
                + "; ".join(
                    f"{line.get('quantity')} {line.get('unit')} {line.get('product_name') or line.get('product_query')} ({line.get('reference')})"
                    for line in draft.get("lines", [])
                    if isinstance(line, dict)
                )
            ),
            force_human_review=True,
            whatsapp_session_id=session.id,
            validated_order_snapshot={
                "draft": draft,
                "customer_confirmed": reason == "customer_confirmation",
                "review_reasons": [] if reason == "customer_confirmation" else [
                    "Pedido recibido en WhatsApp sin confirmación expresa del cliente."
                ],
            },
        )
        if isinstance(result, dict) and result.get("ok") is False:
            error = RuntimeError(str(result.get("message") or "No se pudo evaluar el pedido WhatsApp."))
            error.retryable = bool(result.get("retryable"))
            error.error_type = str(result.get("error_type") or "whatsapp_order_evaluation_failed")
            raise error
        order_id = result.get("order_id") if isinstance(result, dict) else None
        if order_id:
            session.order_id = int(order_id)
            for source_message in messages:
                if source_message.order_id is None:
                    source_message.order_id = int(order_id)
        else:
            session.handoff_reason = str(
                result.get("message") if isinstance(result, dict) else "El pedido requiere revisión manual."
            )[:255]
        session.status = "closed_review"
        session.active_key = None
        session.closed_at = _now()
        session.human_active = False
        session.last_error = None
        session.updated_at = _now()
        conversation.status = "open"
        conversation.assigned_user_id = None
        db.commit()
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=session.conversation_id,
            session_id=session.id,
            event="finalize_completed",
            status="review",
            metadata={"order_id": session.order_id, "reason": reason},
        )
        return {"ok": True, "status": session.status, "order_id": session.order_id, "pipeline_status": result.get("status") if isinstance(result, dict) else None}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        refreshed = db.get(WhatsAppOrderSession, session_id)
        if refreshed and refreshed.company_id == company_id:
            refreshed.status = "finalizing"
            refreshed.human_active = False
            refreshed.handoff_reason = None
            refreshed.last_error = type(exc).__name__
            refreshed.updated_at = _now()
            db.commit()
        logger.error(
            "WhatsApp session finalization failed session=%s error_type=%s",
            session_id,
            type(exc).__name__,
        )
        raise


def process_whatsapp_agent_turn(
    db: Session,
    *,
    company_id: int,
    inbound_message: InboundMessage,
) -> dict[str, Any]:
    if inbound_message.company_id != company_id or not inbound_message.conversation_id:
        return {"ok": True, "skipped": True, "reason": "not_a_tenant_conversation"}
    if inbound_message.direction != "inbound":
        return {"ok": True, "skipped": True, "reason": "not_customer_message"}
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == inbound_message.conversation_id,
            Conversation.company_id == company_id,
        )
    )
    if not conversation:
        return {"ok": True, "skipped": True, "reason": "conversation_not_found"}

    if inbound_message.processing_step in {
        "whatsapp_agent_order_cancelled",
        "whatsapp_agent_cancel_handoff",
    }:
        return {"ok": True, "skipped": True, "reason": "turn_already_processed"}

    config = whatsapp_config(db, company_id)
    if not conversation.whatsapp_bot_enabled:
        inbound_message.status = "received"
        human_owned = conversation.status == "human_owned"
        inbound_message.processing_step = (
            "whatsapp_agent_handoff_waiting" if human_owned else "whatsapp_agent_disabled"
        )
        db.commit()
        return {"ok": True, "status": "human_handoff" if human_owned else "bot_disabled"}

    latest_message = _latest_customer_message(
        db, company_id=company_id, conversation_id=conversation.id
    )
    if not latest_message:
        return {"ok": True, "skipped": True, "reason": "superseded_by_new_customer_message"}
    late_message_to_reconcile: InboundMessage | None = None
    if latest_message.id != inbound_message.id:
        active_session = active_whatsapp_session(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
        )
        media_ready = bool(inbound_message.attachments) and all(
            str(attachment.extraction_status or "pending").lower() not in _MEDIA_PENDING
            for attachment in inbound_message.attachments
        )
        if (
            active_session
            and latest_message.processing_step == "whatsapp_media_pending"
            and media_ready
        ):
            # Re-evaluate the latest customer turn when an earlier PDF/document
            # finishes downloading; that turn had been deferred on this media.
            if (
                active_session.last_processed_message_id == latest_message.id
                and inbound_message.processing_step in {"received", "received_whatsapp"}
                and _late_message_belongs_to_active_session(
                    db,
                    session=active_session,
                    message=inbound_message,
                    latest_message=latest_message,
                    idle_hours=config.idle_session_hours,
                )
            ):
                late_message_to_reconcile = inbound_message
            inbound_message = latest_message
        elif (
            active_session
            and active_session.status in {"collecting", "awaiting_customer", "ready_for_review"}
            and not active_session.human_active
            and conversation.status != "human_owned"
            and active_session.last_processed_message_id == latest_message.id
            and inbound_message.processing_step in {"received", "received_whatsapp"}
            and all(
                str(attachment.extraction_status or "pending").lower() not in _MEDIA_PENDING
                for attachment in inbound_message.attachments
            )
            and _late_message_belongs_to_active_session(
                db,
                session=active_session,
                message=inbound_message,
                latest_message=latest_message,
                idle_hours=config.idle_session_hours,
            )
        ):
            # Meta may deliver an earlier customer message after a newer one
            # has already been processed. Re-evaluate the current chronological
            # latest turn with the complete transcript so no order line is lost.
            late_message_to_reconcile = inbound_message
            inbound_message = latest_message
        else:
            return {"ok": True, "skipped": True, "reason": "superseded_by_new_customer_message"}

    message_at = _aware(latest_message.received_at) or _now()
    previous_session = active_whatsapp_session(
        db,
        company_id=company_id,
        conversation_id=conversation.id,
        lock=True,
    )
    if (
        previous_session
        and previous_session.closed_reason in {
            _CANCEL_REPLY_PENDING,
            _CANCEL_HANDOFF_REPLY_PENDING,
        }
        and previous_session.last_processed_message_id != latest_message.id
    ):
        pending_trigger = db.scalar(
            select(InboundMessage).where(
                InboundMessage.id == previous_session.last_processed_message_id,
                InboundMessage.company_id == company_id,
                InboundMessage.conversation_id == conversation.id,
            )
        )
        if pending_trigger is None:
            pending_trigger = latest_message
        transition = _finish_pending_customer_cancellation(
            db,
            session=previous_session,
            conversation=conversation,
            inbound_message=pending_trigger,
        )
        if previous_session.status == "human_handoff":
            latest_message.status = "received"
            latest_message.processing_step = "whatsapp_agent_handoff_waiting"
            previous_session.last_processed_message_id = latest_message.id
            previous_session.last_customer_message_at = message_at
            previous_session.due_at = message_at + timedelta(
                hours=config.idle_session_hours
            )
            previous_session.revision = int(previous_session.revision or 0) + 1
            previous_session.updated_at = _now()
            db.commit()
            return {
                "ok": True,
                "status": "human_handoff",
                "session_id": previous_session.id,
                "reason": "customer_cancelled_existing_order",
                "pending_reply": bool(transition),
            }
        if previous_session.closed_reason == "customer_cancelled":
            previous_session = None

    if previous_session and (previous_session.human_active or conversation.status == "human_owned"):
        session = previous_session
    else:
        prior_draft = _json_object(previous_session.draft_json, {}) if previous_session else {}
        previous_due_at = _aware(previous_session.due_at) if previous_session else None
        session_idle_expired = bool(
            previous_session
            and previous_session.active_key
            and previous_session.status not in {"human_handoff", "closed_review", "closed_no_order"}
            and not previous_session.human_active
            and previous_due_at
            and message_at >= previous_due_at
        )
        force_new_session = session_idle_expired or bool(
            previous_session
            and prior_draft.get("lines")
            and _explicit_new_order_request(latest_message.original_content)
        )
        if force_new_session and previous_session:
            previous_session.message_cutoff_at = message_at
            previous_session.status = (
                "ready_for_review"
                if _draft_is_complete(
                    prior_draft,
                    _json_object(previous_session.missing_fields_json, []),
                )
                else "awaiting_customer"
            )
            previous_session.finalization_job_key = None
            previous_session.updated_at = _now()
            db.commit()
            enqueue_whatsapp_session_finalization(
                db,
                session=previous_session,
                reason="idle_timeout" if session_idle_expired else "order_boundary",
            )
        session = _get_or_create_session(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            latest_message=latest_message,
            message_at=message_at,
            idle_hours=config.idle_session_hours,
            force_new=force_new_session,
            message_id=latest_message.id if force_new_session else None,
        )
        session = active_whatsapp_session(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            lock=True,
        ) or session
    if (
        late_message_to_reconcile is not None
        and (
            session.last_processed_message_id != latest_message.id
            or session.status not in {"collecting", "awaiting_customer", "ready_for_review"}
            or session.human_active
            or conversation.status == "human_owned"
        )
    ):
        return {
            "ok": True,
            "skipped": True,
            "reason": "session_changed_during_late_message_reconciliation",
            "session_id": session.id,
        }
    if session.human_active or conversation.status == "human_owned":
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason=session.handoff_reason or "Atención asumida por un agente.",
            active=True,
        )
        session.last_customer_message_at = message_at
        session.due_at = message_at + timedelta(hours=config.idle_session_hours)
        session.revision = int(session.revision or 0) + 1
        session.finalization_job_key = None
        session.last_processed_message_id = latest_message.id
        session.updated_at = _now()
        _mark_late_message_reconciled(late_message_to_reconcile)
        db.commit()
        return {"ok": True, "status": "human_handoff"}
    if (
        session.closed_reason in {
            _CANCEL_REPLY_PENDING,
            _CANCEL_HANDOFF_REPLY_PENDING,
        }
        and session.last_processed_message_id == latest_message.id
    ):
        pending_result = _resume_pending_customer_cancellation(
            db,
            company_id=company_id,
            conversation=conversation,
            session=session,
            inbound_message=latest_message,
            config=config,
        )
        if pending_result is not None:
            return pending_result
    if (
        inbound_message.processing_step == "whatsapp_agent_reply_retryable"
        and session.last_processed_message_id == latest_message.id
    ):
        return _retry_prepared_whatsapp_reply(
            db,
            company_id=company_id,
            conversation=conversation,
            session=session,
            inbound_message=latest_message,
            config=config,
        )
    if session.status == "human_handoff":
        session.last_customer_message_at = message_at
        session.due_at = message_at + timedelta(hours=config.idle_session_hours)
        session.revision = int(session.revision or 0) + 1
        session.finalization_job_key = None
        session.last_processed_message_id = latest_message.id
        inbound_message.status = "received"
        inbound_message.processing_step = "whatsapp_agent_handoff_waiting"
        _mark_late_message_reconciled(late_message_to_reconcile)
        db.commit()
        return {"ok": True, "status": "human_handoff", "session_id": session.id}
    if (
        session.last_processed_message_id == latest_message.id
        and inbound_message.processing_step != "whatsapp_media_pending"
        and late_message_to_reconcile is None
    ):
        return {"ok": True, "skipped": True, "reason": "turn_already_processed", "session_id": session.id}

    cancellation_result = _handle_explicit_order_cancellation(
        db,
        company_id=company_id,
        conversation=conversation,
        session=session,
        inbound_message=latest_message,
        config=config,
    )
    if cancellation_result is not None:
        return cancellation_result

    if late_message_to_reconcile is not None:
        late_at = _aware(late_message_to_reconcile.received_at)
        started_at = _aware(session.started_at)
        if late_at and (not started_at or late_at < started_at):
            session.started_at = late_at

    transcript, messages = _session_transcript(
        db,
        company_id=company_id,
        conversation_id=conversation.id,
        session=session,
        latest_message=latest_message,
    )
    media_state, media_filename = _has_unreadable_or_pending_media(messages)
    session.last_customer_message_at = message_at
    session.due_at = message_at + timedelta(hours=config.idle_session_hours)
    session.revision = int(session.revision or 0) + 1
    session.finalization_job_key = None
    session.updated_at = _now()
    if media_state == "pending":
        session.status = "awaiting_customer"
        session.handoff_reason = None
        session.last_processed_message_id = latest_message.id
        inbound_message.status = "received"
        inbound_message.processing_step = "whatsapp_media_pending"
        _mark_late_message_reconciled(late_message_to_reconcile)
        db.commit()
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            session_id=session.id,
            event="media_pending",
            status="waiting",
        )
        return {"ok": True, "status": "media_pending", "session_id": session.id}
    readable_inbound_text_exists = any(
        item.direction == "inbound" and bool(_message_text(item).strip())
        for item in messages
    )
    if media_state == "unreadable" and not readable_inbound_text_exists:
        session.last_processed_message_id = latest_message.id
        fallback_reply = (
            f"No he podido interpretar el archivo o audio adjunto {media_filename}. "
            "Lo paso al equipo para que lo revise; no se tramitará automáticamente."
        )
        session.suggested_reply = fallback_reply
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason=f"No se pudo interpretar el adjunto {media_filename}.",
            trigger_message_id=latest_message.id,
        )
        inbound_message.status = "received"
        inbound_message.processing_step = "whatsapp_media_handoff"
        _mark_late_message_reconciled(late_message_to_reconcile)
        db.commit()
        send_result: dict[str, Any] = {"sent": False, "reason": "response_window_closed"}
        window = whatsapp_response_window_status(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            response_window_minutes=config.response_window_minutes,
        )
        if window.get("is_open"):
            try:
                send_result = await_send_automatic_response(
                    db,
                    company_id=company_id,
                    conversation_id=conversation.id,
                    trigger_message_id=latest_message.id,
                    body=fallback_reply,
                    semantic_state="human_handoff",
                    assistant_session_id=session.id,
                    draft_revision=session.revision,
                )
            except Exception as exc:  # noqa: BLE001
                error_type = str(getattr(exc, "error_type", None) or type(exc).__name__)
                if error_type == "rate_limited" and conversation.whatsapp_bot_enabled:
                    inbound_message.processing_step = "whatsapp_agent_reply_retryable"
                    db.commit()
                    raise WhatsAppAgentReplyDeliveryError(
                        error_type, retryable=True
                    ) from exc
                send_result = {"sent": False, "reason": type(exc).__name__}
        conversation.whatsapp_bot_enabled = False
        conversation.updated_at = _now()
        db.commit()
        return {
            "ok": True,
            "status": "human_handoff",
            "session_id": session.id,
            "reason": "unreadable_media",
            "auto_response": send_result,
        }

    prior_draft = _json_object(session.draft_json, {})
    prior_missing = _json_object(session.missing_fields_json, [])
    if late_message_to_reconcile is None and _confirms_current_order_summary(
        messages,
        message=latest_message,
        session=session,
        draft=prior_draft,
        missing=prior_missing,
    ):
        session.intent = "order"
        session.status = "ready_for_review"
        session.last_processed_message_id = latest_message.id
        session.suggested_reply = None
        session.handoff_reason = None
        session.last_error = None
        session.updated_at = _now()
        inbound_message.status = "received"
        inbound_message.processing_step = "whatsapp_order_confirmed_by_customer"
        db.commit()
        return enqueue_whatsapp_session_finalization(
            db,
            session=session,
            reason="customer_confirmation",
        )
    if int(session.agent_turn_count or 0) >= config.max_agent_turns_per_session:
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason="Se alcanzó el límite de turnos de IA configurado.",
            trigger_message_id=latest_message.id,
        )
        session.last_processed_message_id = latest_message.id
        session.last_customer_message_at = message_at
        session.due_at = message_at + timedelta(hours=config.idle_session_hours)
        session.revision = int(session.revision or 0) + 1
        session.finalization_job_key = None
        session.suggested_reply = (
            "Voy a pasar tu consulta a una persona del equipo para que pueda ayudarte."
        )
        inbound_message.status = "received"
        inbound_message.processing_step = "whatsapp_agent_turn_limit"
        _mark_late_message_reconciled(late_message_to_reconcile)
        db.commit()
        send_result: dict[str, Any] = {"sent": False, "reason": "response_window_closed"}
        window = whatsapp_response_window_status(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            response_window_minutes=config.response_window_minutes,
        )
        if window.get("is_open"):
            try:
                send_result = await_send_automatic_response(
                    db,
                    company_id=company_id,
                    conversation_id=conversation.id,
                    trigger_message_id=latest_message.id,
                    body=session.suggested_reply,
                    semantic_state="human_handoff",
                    assistant_session_id=session.id,
                    draft_revision=session.revision,
                )
            except Exception as exc:  # noqa: BLE001
                error_type = str(getattr(exc, "error_type", None) or type(exc).__name__)
                if error_type == "rate_limited" and conversation.whatsapp_bot_enabled:
                    inbound_message.processing_step = "whatsapp_agent_reply_retryable"
                    db.commit()
                    raise WhatsAppAgentReplyDeliveryError(
                        error_type, retryable=True
                    ) from exc
                send_result = {"sent": False, "reason": type(exc).__name__}
        conversation.whatsapp_bot_enabled = False
        conversation.updated_at = _now()
        db.commit()
        return {
            "ok": True,
            "status": "human_handoff",
            "session_id": session.id,
            "reason": "agent_turn_limit",
            "auto_response": send_result,
        }
    customer = _customer_context(
        db,
        company_id=company_id,
        conversation=conversation,
        messages=messages,
    )
    quoted_message = _reply_context_message(latest_message, messages)
    reply_context_text = None
    if quoted_message:
        quoted_role = "CLIENTE" if quoted_message.direction == "inbound" else "EMPRESA"
        quoted_text = _message_text(quoted_message).strip()[:1500]
        if quoted_text:
            reply_context_text = f"{quoted_role}: {quoted_text}"
    recent_transcript = _bounded_recent_transcript(
        messages,
        quoted_message=quoted_message,
    )
    context_limit_exceeded = _context_exceeds_agent_limit(
        messages,
        has_order_context=session.intent in {"order", "order_question"} or bool(prior_draft.get("lines")),
    )
    products = [] if context_limit_exceeded else _product_context(
        db,
        company_id=company_id,
        transcript=recent_transcript,
        draft=prior_draft,
    )
    faq_entries = _faq_entries(config.business_faq, latest_message.original_content)
    try:
        if context_limit_exceeded:
            evaluation = WhatsAppAgentTurnResult(
                intent="order_question" if prior_draft.get("lines") else "order",
                action="handoff",
                draft=prior_draft,
                missing_or_uncertain=prior_missing or ["Revisar todas las líneas del pedido manualmente."],
                reply_needed=True,
                suggested_reply="",
                confidence=1.0,
                evidence_ids=[],
                close_requested=False,
            )
        else:
            evaluation = evaluate_whatsapp_agent_turn(
                db,
                company_id=company_id,
                transcript=recent_transcript,
                draft=prior_draft,
                customer=customer,
                products=products,
                business_faq=json.dumps(faq_entries, ensure_ascii=False),
                missing_or_uncertain=prior_missing,
                latest_message_text=_message_text(latest_message),
                reply_context_text=reply_context_text,
                inbound_message=latest_message,
                input_reference=f"whatsapp-session:{session.id}:revision:{session.revision}",
                reconcile_order_transcript=late_message_to_reconcile is not None,
            )
    except Exception as exc:  # noqa: BLE001
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason="No se pudo evaluar el mensaje con IA.",
            trigger_message_id=latest_message.id,
        )
        session.last_processed_message_id = latest_message.id
        session.last_error = type(exc).__name__
        inbound_message.status = "doubtful"
        inbound_message.processing_step = "whatsapp_agent_error"
        inbound_message.processing_error = "No se pudo evaluar el mensaje con IA."
        fallback_reply = (
            "No he podido procesar tu mensaje automáticamente. "
            "Lo dejo pendiente de revisión por el equipo; puedes enviar aquí "
            "cualquier detalle adicional del pedido."
        )
        session.suggested_reply = fallback_reply
        _mark_late_message_reconciled(late_message_to_reconcile)
        db.commit()
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            session_id=session.id,
            event="intent_evaluated",
            status="error",
            metadata={"error_type": type(exc).__name__},
        )
        send_result: dict[str, Any] = {"sent": False, "reason": "response_window_closed"}
        try:
            window = whatsapp_response_window_status(
                db,
                company_id=company_id,
                conversation_id=conversation.id,
                response_window_minutes=config.response_window_minutes,
            )
            if window.get("is_open"):
                send_result = await_send_automatic_response(
                    db,
                    company_id=company_id,
                    conversation_id=conversation.id,
                    trigger_message_id=latest_message.id,
                    body=fallback_reply,
                    semantic_state="human_handoff",
                    assistant_session_id=session.id,
                    draft_revision=session.revision,
                )
            else:
                send_result = {
                    "sent": False,
                    "reason": str(window.get("reason") or "response_window_closed"),
                }
        except Exception as send_exc:  # noqa: BLE001
            logger.warning(
                "WhatsApp agent error acknowledgement failed session=%s type=%s",
                session.id,
                type(send_exc).__name__,
            )
            error_type = str(
                getattr(send_exc, "error_type", None) or type(send_exc).__name__
            )
            if error_type == "rate_limited" and conversation.whatsapp_bot_enabled:
                inbound_message.processing_step = "whatsapp_agent_reply_retryable"
                db.commit()
                raise WhatsAppAgentReplyDeliveryError(
                    error_type, retryable=True
                ) from send_exc
            send_result = {"sent": False, "reason": "send_failed"}
        conversation.whatsapp_bot_enabled = False
        conversation.updated_at = _now()
        db.commit()
        return {
            "ok": True,
            "status": "human_handoff",
            "session_id": session.id,
            "auto_response": send_result,
        }

    allowed_evidence = {item["id"] for item in products}
    allowed_evidence.update(item["id"] for item in faq_entries)
    invalid_evidence = set(evaluation.evidence_ids) - allowed_evidence
    clean_draft, missing = _canonicalize_draft(
        evaluation.draft,
        products=products,
        semantic_missing=evaluation.missing_or_uncertain,
        intent=evaluation.intent,
        messages=messages,
    )
    line_limit_exceeded = any(
        "supera el límite de 30 líneas" in item.casefold() for item in missing
    )
    preserve_existing_order = evaluation.intent in {"question", "order_question", "other", "greeting"} and bool(prior_draft.get("lines"))
    if preserve_existing_order:
        clean_draft = prior_draft
        missing = prior_missing
    close_requested = has_explicit_order_close(latest_message.original_content)
    complete = _draft_is_complete(clean_draft, missing)
    action = evaluation.action
    grounded_answer = _grounded_faq_answer(faq_entries, evaluation.evidence_ids)
    if re.search(
        r"\b(stock|disponibilidad|disponible|inventario|existencias|queda|quedan)\b",
        latest_message.original_content.casefold(),
    ):
        grounded_answer = None
    if grounded_answer is None and evaluation.intent in {"question", "order_question"}:
        grounded_answer = _grounded_product_answer(
            latest_message.original_content,
            products,
            evaluation.evidence_ids,
        )
    if invalid_evidence:
        action = "handoff"
    if line_limit_exceeded:
        action = "handoff"
    if media_state == "unreadable":
        action = "handoff"
    if (
        evaluation.confidence < 0.45
        and not evaluation.contextual_order_followup
        and evaluation.intent != "greeting"
    ):
        action = "handoff"
    if evaluation.intent == "question" and evaluation.reply_needed and not grounded_answer:
        action = "handoff"
    if evaluation.intent == "order_question" and evaluation.reply_needed and not grounded_answer and not missing:
        action = "handoff"
    if (
        action != "handoff"
        and close_requested
        and clean_draft.get("lines")
        and not complete
    ):
        action = "ask"
        missing = list(dict.fromkeys(missing + ["Aún hay datos del pedido que necesitan confirmación."]))

    session.intent = (
        session.intent
        if preserve_existing_order
        else "order_question" if evaluation.intent in {"question", "other"} and clean_draft.get("lines")
        else evaluation.intent
    )
    session.draft_json = json.dumps(clean_draft, ensure_ascii=False)
    session.missing_fields_json = json.dumps(missing, ensure_ascii=False)
    if action == "handoff" and media_state == "unreadable":
        safe_reply = (
            f"He conservado el texto de tu mensaje, pero no he podido interpretar el adjunto {media_filename}. "
            "Lo paso al equipo para que lo revise antes de tramitar el pedido."
        )
    elif action == "handoff":
        safe_reply = (
            "El pedido o el PDF es demasiado extenso para revisar todas sus líneas automáticamente. "
            "Lo paso al equipo para que lo compruebe completo."
            if context_limit_exceeded
            else "El pedido tiene más de 30 líneas; lo paso al equipo para revisar el documento completo."
            if line_limit_exceeded
            else "Voy a pasar tu consulta a una persona del equipo para que pueda ayudarte."
        )
    elif preserve_existing_order and evaluation.reply_needed and grounded_answer:
        continuation = _order_summary_reply(clean_draft) if complete else _clarification_reply(missing, draft=clean_draft)
        safe_reply = f"{grounded_answer}\n\n{continuation}"
    elif evaluation.intent == "order_question" and grounded_answer:
        continuation = _order_summary_reply(clean_draft) if complete else _clarification_reply(missing, draft=clean_draft)
        safe_reply = f"{grounded_answer}\n\n{continuation}"
    elif evaluation.intent in {"order", "order_question"} and not complete:
        safe_reply = _clarification_reply(missing, draft=clean_draft)
    elif close_requested and clean_draft.get("lines") and not complete:
        safe_reply = _clarification_reply(missing, draft=clean_draft)
    elif evaluation.intent in {"order", "order_question"} and complete:
        safe_reply = _order_summary_reply(clean_draft)
    elif evaluation.intent == "greeting":
        safe_reply = (
            "¡Hola! ¿En qué puedo ayudarte? Si quieres hacer un pedido, "
            "puedes enviarme los productos y las cantidades poco a poco."
        )
    elif evaluation.intent == "other":
        normalized_customer_text = unicodedata.normalize(
            "NFKD", latest_message.original_content or ""
        ).casefold()
        normalized_customer_text = "".join(
            character
            for character in normalized_customer_text
            if not unicodedata.combining(character)
        )
        if re.fullmatch(r"\s*(?:gracias|muchas gracias|ok|okay|vale|perfecto|de acuerdo)[.!\s]*", normalized_customer_text):
            safe_reply = (
                "¡De nada! Si necesitas ayuda con el pedido o tienes otra consulta, "
                "escríbeme por aquí."
            )
        else:
            safe_reply = (
                "No he terminado de entender qué necesitas. ¿Quieres hacer un pedido o tienes una consulta? "
                "Puedes enviarme los productos y las cantidades en varios mensajes, o adjuntar un PDF."
            )
    else:
        safe_reply = grounded_answer or ""
    session.suggested_reply = safe_reply[:4096] or None
    session.last_processed_message_id = latest_message.id
    session.last_prompt_execution_id = evaluation.prompt_execution_id
    session.agent_turn_count = int(session.agent_turn_count or 0) + 1
    session.last_error = None
    session.updated_at = _now()
    inbound_message.status = "received"
    inbound_message.processing_step = "whatsapp_agent_evaluated"
    _mark_late_message_reconciled(late_message_to_reconcile)

    if action == "handoff":
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason=(
                "La IA propuso información sin evidencia del catálogo o de la FAQ."
                if invalid_evidence
                else "El pedido o PDF supera el límite de análisis automático y requiere revisión íntegra."
                if context_limit_exceeded
                else "El pedido supera 30 líneas y requiere revisión completa."
                if line_limit_exceeded
                else "La conversación necesita atención humana."
            ),
            trigger_message_id=latest_message.id,
        )
    elif evaluation.intent in {"order", "order_question"}:
        session.status = "ready_for_review" if complete else "awaiting_customer"
    else:
        session.status = (
            "ready_for_review" if clean_draft.get("lines") and complete
            else "awaiting_customer" if clean_draft.get("lines")
            else "collecting"
        )
        session.handoff_reason = None
    db.commit()

    _log_agent_event(
        db,
        company_id=company_id,
        conversation_id=conversation.id,
        session_id=session.id,
        event="intent_evaluated",
        status=action,
        metadata={
            "intent": evaluation.intent,
            "confidence": round(evaluation.confidence, 3),
            "revision": session.revision,
            "missing_count": len(missing),
            "prompt_execution_id": evaluation.prompt_execution_id,
        },
    )

    if (
        action != "handoff"
        and close_requested
        and clean_draft.get("lines")
        and complete
    ):
        return enqueue_whatsapp_session_finalization(
            db,
            session=session,
            reason="customer_request",
        )

    reply = session.suggested_reply or ""
    if (
        evaluation.reply_needed
        or action in {"ask", "handoff"}
        or (
            session.status == "ready_for_review"
            and evaluation.intent in {"order", "order_question"}
        )
    ):
        send_result: dict[str, Any] = {"sent": False, "skipped": True, "reason": "no_reply_needed"}
        if reply:
            window = whatsapp_response_window_status(
                db,
                company_id=company_id,
                conversation_id=conversation.id,
                response_window_minutes=config.response_window_minutes,
            )
            if window.get("is_open"):
                try:
                    send_result = await_send_automatic_response(
                        db,
                        company_id=company_id,
                        conversation_id=conversation.id,
                        trigger_message_id=latest_message.id,
                        body=reply,
                        semantic_state=(
                            "ready_for_confirmation"
                            if action != "handoff"
                            and session.status == "ready_for_review"
                            and _order_summary_reply(clean_draft) in reply
                            else "human_handoff" if action == "handoff" else "informational"
                        ),
                        assistant_session_id=session.id,
                        draft_revision=session.revision,
                        prompt_execution_id=evaluation.prompt_execution_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("WhatsApp agent reply failed session=%s type=%s", session.id, type(exc).__name__)
                    error_type = str(getattr(exc, "error_type", None) or type(exc).__name__)
                    retryable_delivery = bool(
                        error_type == "rate_limited"
                        and conversation.whatsapp_bot_enabled
                        and not session.human_active
                    )
                    if retryable_delivery:
                        inbound_message.processing_step = "whatsapp_agent_reply_retryable"
                        db.commit()
                    raise WhatsAppAgentReplyDeliveryError(
                        error_type,
                        retryable=retryable_delivery,
                    ) from exc
            else:
                send_result = {"sent": False, "reason": str(window.get("reason") or "response_window_closed")}
        if (
            not send_result.get("sent")
            and str(send_result.get("reason") or "")
            in {"auto_message_limit", "response_window_closed"}
        ):
            limit_reason = str(send_result.get("reason") or "response_window_closed")
            _set_handoff(
                db,
                session=session,
                conversation=conversation,
                reason=(
                    "La respuesta automática alcanzó su límite de seguridad; "
                    "la conversación requiere atención humana."
                    if limit_reason == "auto_message_limit"
                    else "No se puede responder fuera de la ventana de atención de WhatsApp. "
                    "Revisar la conversación y usar una plantilla aprobada si procede."
                ),
                trigger_message_id=latest_message.id,
            )
            action = "handoff"
            inbound_message.processing_step = (
                "whatsapp_agent_response_limit"
                if limit_reason == "auto_message_limit"
                else "whatsapp_agent_response_window_closed"
            )
            conversation.updated_at = _now()
            db.commit()
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            session_id=session.id,
            event="reply_sent" if send_result.get("sent") else "reply_skipped",
            status="sent" if send_result.get("sent") else str(send_result.get("reason") or "skipped"),
            metadata={"message_id": latest_message.id},
        )
    else:
        send_result = {"sent": False, "skipped": True, "reason": "no_reply_needed"}

    if action == "handoff":
        conversation.whatsapp_bot_enabled = False
        conversation.updated_at = _now()
        db.commit()

    return {
        "ok": True,
        "status": session.status,
        "session_id": session.id,
        "intent": session.intent,
        "missing_or_uncertain": missing,
        "reply_needed": bool(reply),
        "auto_response": send_result,
    }


def await_send_automatic_response(db: Session, **kwargs) -> dict[str, Any]:
    """Synchronously bridge the async Meta sender from the worker process."""

    import asyncio

    return asyncio.run(send_automatic_response(db, **kwargs))


def handoff_failed_whatsapp_message(
    db: Session,
    *,
    company_id: int,
    inbound_message_id: int,
    error_type: str,
) -> dict[str, Any]:
    """Make an exhausted WhatsApp processing job visible and stop bot replies."""

    message = db.scalar(
        select(InboundMessage)
        .where(
            InboundMessage.id == inbound_message_id,
            InboundMessage.company_id == company_id,
            InboundMessage.direction == "inbound",
        )
        .with_for_update()
    )
    if not message or not message.conversation_id:
        return {"ok": True, "skipped": True, "reason": "message_not_found"}
    conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.id == message.conversation_id,
            Conversation.company_id == company_id,
        )
        .with_for_update()
    )
    if not conversation:
        return {"ok": True, "skipped": True, "reason": "conversation_not_found"}
    session = active_whatsapp_session(
        db,
        company_id=company_id,
        conversation_id=conversation.id,
        lock=True,
    )
    existing_order = db.scalar(
        select(WhatsAppOrderSession.id)
        .where(
            WhatsAppOrderSession.company_id == company_id,
            WhatsAppOrderSession.conversation_id == conversation.id,
            WhatsAppOrderSession.last_processed_message_id == message.id,
            WhatsAppOrderSession.order_id.is_not(None),
        )
        .limit(1)
    )
    if message.order_id or existing_order or (session and session.order_id):
        return {"ok": True, "skipped": True, "reason": "order_already_created"}
    reason = "El procesamiento automático falló y requiere revisión humana."
    if session:
        _set_handoff(db, session=session, conversation=conversation, reason=reason)
        session.last_processed_message_id = message.id
        session.last_error = str(error_type or "job_failed")[:120]
        session.updated_at = _now()
    message.status = "doubtful"
    message.processing_step = "whatsapp_agent_failed"
    message.processing_error = "No se pudo completar el procesamiento automático tras varios intentos."
    alert = db.scalar(
        select(Alert).where(
            Alert.company_id == company_id,
            Alert.inbound_message_id == message.id,
            Alert.alert_type == "whatsapp_agent_processing_failed",
            Alert.status == "open",
        )
    )
    if not alert:
        db.add(
            Alert(
                company_id=company_id,
                inbound_message_id=message.id,
                alert_type="whatsapp_agent_processing_failed",
                severity="high",
                status="open",
                title="Un mensaje de WhatsApp necesita atención",
                message="Falló el procesamiento automático. Revisa el mensaje y continúa el pedido manualmente.",
                payload_json=json.dumps(
                    {"conversation_id": conversation.id, "session_id": session.id if session else None},
                    ensure_ascii=False,
                ),
            )
        )
    db.commit()

    send_result: dict[str, Any] = {"sent": False, "reason": "response_window_closed"}
    try:
        config = whatsapp_config(db, company_id)
        window = whatsapp_response_window_status(
            db,
            company_id=company_id,
            conversation_id=conversation.id,
            response_window_minutes=config.response_window_minutes,
        )
        if window.get("is_open") and conversation.whatsapp_bot_enabled:
            send_result = await_send_automatic_response(
                db,
                company_id=company_id,
                conversation_id=conversation.id,
                trigger_message_id=message.id,
                body=(
                    "No he podido procesar tu mensaje automáticamente. Lo dejo pendiente "
                    "de revisión por el equipo; puedes enviar aquí cualquier detalle adicional del pedido."
                ),
                semantic_state="human_handoff",
                assistant_session_id=session.id if session else None,
                draft_revision=session.revision if session else None,
            )
        elif window.get("is_open"):
            send_result = {"sent": False, "reason": "bot_disabled"}
        else:
            send_result = {"sent": False, "reason": str(window.get("reason") or "response_window_closed")}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "WhatsApp processing failure acknowledgement failed message=%s type=%s",
            inbound_message_id,
            type(exc).__name__,
        )
        send_result = {"sent": False, "reason": "send_failed"}

    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == message.conversation_id,
            Conversation.company_id == company_id,
        )
    )
    session = active_whatsapp_session(
        db,
        company_id=company_id,
        conversation_id=message.conversation_id,
        lock=True,
    )
    if conversation:
        conversation.status = "human_owned"
        conversation.whatsapp_bot_enabled = False
        conversation.updated_at = _now()
    if session:
        session.status = "human_handoff"
        session.human_active = True
        session.handoff_reason = reason
        session.handoff_at = session.handoff_at or _now()
        session.updated_at = _now()
    db.commit()
    if session:
        _log_agent_event(
            db,
            company_id=company_id,
            conversation_id=message.conversation_id,
            session_id=session.id,
            event="processing_failed_handoff",
            status="error",
            metadata={"message_id": message.id, "error_type": str(error_type or "job_failed")[:120]},
        )
    return {
        "ok": True,
        "status": "human_handoff",
        "message_id": message.id,
        "session_id": session.id if session else None,
        "auto_response": send_result,
    }


def enqueue_whatsapp_session_finalization(
    db: Session,
    *,
    session: WhatsAppOrderSession,
    reason: str,
    user_id: int | None = None,
) -> dict[str, Any]:
    key = _finalization_dedupe_key(db, session)
    job = enqueue_job(
        db,
        company_id=session.company_id,
        job_type="whatsapp_finalize_session",
        payload={
            "session_id": session.id,
            "expected_revision": session.revision,
            "reason": reason,
        },
        created_by_user_id=user_id,
        dedupe_key=key,
        max_retries=5,
    )
    session.finalization_job_key = key
    session.updated_at = _now()
    db.commit()
    _log_agent_event(
        db,
        company_id=session.company_id,
        conversation_id=session.conversation_id,
        session_id=session.id,
        event="finalize_started",
        status="queued",
        metadata={"job_id": job.id, "reason": reason, "revision": session.revision},
    )
    return {"ok": True, "status": "finalizing", "session_id": session.id, "job_id": job.id}
