from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
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
    Product,
    ProductAlias,
    WhatsAppOrderSession,
)
from app.jobs.service import enqueue_job
from app.logs.service import log_flow_event
from app.whatsapp.conversation_orders import (
    has_explicit_order_close,
    is_short_order_confirmation,
)
from app.whatsapp.conversation_semantics import WhatsAppAgentTurnResult, evaluate_whatsapp_agent_turn
from app.whatsapp.service import (
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
_MEDIA_PENDING = {"pending", "downloaded"}
_MEDIA_UNREADABLE = {
    "extraction_error",
    "storage_error",
    "unsupported",
    "no_text_found",
    "stored",
    # Audio is downloaded by the current media worker, but no transcription
    # provider is wired into that worker yet. Never infer order details from it.
    "transcription_pending",
}
_ACTIVE_JOB_STATES = {"queued", "running", "retrying"}
_MAX_AGENT_CONTEXT_CHARS = 15000
_MAX_AGENT_CONTEXT_MESSAGES = 50


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
    if session.message_cutoff_at is not None:
        query = query.where(InboundMessage.received_at < session.message_cutoff_at)
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
) -> str:
    parts: list[str] = []
    customer_messages = [row for row in messages if row.direction == "inbound"]
    for row in customer_messages[-_MAX_AGENT_CONTEXT_MESSAGES:]:
        text = _message_text(row)
        if text:
            parts.append(f"CLIENTE: {text}")
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
    query = select(InboundMessage).where(
            InboundMessage.company_id == company_id,
            InboundMessage.conversation_id == conversation_id,
            InboundMessage.content_type != "whatsapp_status",
            InboundMessage.received_at >= session.started_at,
    )
    if session.message_cutoff_at is not None:
        query = query.where(InboundMessage.received_at < session.message_cutoff_at)
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
    terms = re.findall(r"[\w-]{3,}", transcript[-5000:], flags=re.UNICODE)
    for line in draft.get("lines", []) if isinstance(draft, dict) else []:
        if isinstance(line, dict):
            terms.extend(re.findall(r"[\w-]{3,}", str(line.get("product_query") or ""), flags=re.UNICODE))
    unique_terms = list(dict.fromkeys(term.lower() for term in terms))[-12:]
    if not unique_terms:
        return []
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
    products = db.scalars(
        select(Product)
        .outerjoin(ProductAlias, ProductAlias.product_id == Product.id)
        .where(
            Product.company_id == company_id,
            Product.deleted_at.is_(None),
            Product.obsolete.is_(False),
            Product.status != "inactive",
            or_(*conditions),
        )
        .distinct()
        .order_by(Product.name.asc(), Product.id.asc())
        .limit(8)
    ).all()
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


def _clarification_reply(missing: list[str]) -> str:
    questions = [str(item).strip().rstrip(".") for item in missing[:4] if str(item).strip()]
    if not questions:
        return "¿Puedes confirmar los datos del pedido para que pueda dejarlo preparado?"
    return "Para preparar bien el pedido necesito confirmar: " + "; ".join(questions) + "."


_NUMBER_WORDS = {
    1: {"un", "uno", "una"},
    2: {"dos"},
    3: {"tres"},
    4: {"cuatro"},
    5: {"cinco"},
    6: {"seis"},
    7: {"siete"},
    8: {"ocho"},
    9: {"nueve"},
    10: {"diez"},
}


def _quantity_appears_in_text(quantity: int | float, text: str) -> bool:
    normalized = str(text or "").casefold()
    formatted = f"{float(quantity):g}"
    if re.search(rf"(?<!\w){re.escape(formatted)}(?:[.,]0+)?(?!\w)", normalized):
        return True
    if float(quantity).is_integer():
        tokens = set(re.findall(r"[a-záéíóúñ]+", normalized))
        return bool(_NUMBER_WORDS.get(int(quantity), set()).intersection(tokens))
    return False


def _line_source_message_ids(
    *,
    query: str,
    reference: str,
    product_name: str,
    quantity: int | float | None,
    messages: list[InboundMessage],
) -> list[int]:
    if quantity is None:
        return []
    product_terms = {
        token.casefold()
        for value in (query, reference, product_name)
        for token in re.findall(r"[\w-]{3,}", str(value or ""), flags=re.UNICODE)
    }
    product_ids: set[int] = set()
    quantity_ids: set[int] = set()
    for message in messages:
        if message.direction != "inbound":
            continue
        text = _message_text(message).casefold()
        tokens = set(re.findall(r"[\w-]{3,}", text, flags=re.UNICODE))
        if (reference and reference.casefold() in text) or product_terms.intersection(tokens):
            product_ids.add(message.id)
        if _quantity_appears_in_text(quantity, text):
            quantity_ids.add(message.id)
    if not product_ids or not quantity_ids:
        return []
    return sorted(product_ids | quantity_ids)


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
    catalog_by_reference: dict[str, dict[str, Any]] = {}
    for product in products:
        for value in (product.get("reference"), product.get("alternative_code")):
            if value:
                catalog_by_reference[str(value).strip().casefold()] = product

    lines = draft.get("lines") if isinstance(draft.get("lines"), list) else []
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
        product = catalog_by_reference.get(reference.casefold()) if reference else None
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
    if existing and existing.status not in _ACTIVE_JOB_STATES | {"success"}:
        return f"{base}:retry-after-{existing.id}"
    return candidate


def _set_handoff(
    db: Session,
    *,
    session: WhatsAppOrderSession,
    conversation: Conversation,
    reason: str,
    active: bool = False,
) -> None:
    session.status = "human_handoff"
    session.handoff_reason = reason[:255]
    session.handoff_at = _now()
    session.human_active = bool(active)
    conversation.status = "human_owned" if active else "open"


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
        .where(
            WhatsAppOrderSession.company_id == company_id,
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
        if session.finalization_job_key == key:
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
                        payload_json=json.dumps({"session_id": session.id}, ensure_ascii=False),
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
        session.handoff_reason = "Falta vincular alguna lÃ­nea del pedido a mensajes originales del cliente."
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
                payload_json=json.dumps({"session_id": session.id}, ensure_ascii=False),
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
        session.handoff_reason = "El nÃºmero de WhatsApp no coincide con un cliente verificado; confirmar identidad antes de crear el pedido."
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
                    message="El nÃºmero de WhatsApp no coincide de forma Ãºnica con un cliente. Confirmar identidad antes de crear el pedido.",
                    payload_json=json.dumps({"session_id": session.id}, ensure_ascii=False),
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
                    "Pedido recibido en WhatsApp sin confirmaciÃ³n expresa del cliente."
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
            refreshed.status = "human_handoff"
            refreshed.human_active = False
            refreshed.handoff_reason = "No se pudo completar la evaluación automática; requiere revisión humana."
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
    if previous_session and (previous_session.human_active or conversation.status == "human_owned"):
        session = previous_session
    else:
        prior_draft = _json_object(previous_session.draft_json, {}) if previous_session else {}
        force_new_session = bool(
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
                reason="order_boundary",
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
        db.commit()
        return {"ok": True, "status": "human_handoff"}
    if session.status == "human_handoff":
        session.last_customer_message_at = message_at
        session.due_at = message_at + timedelta(hours=config.idle_session_hours)
        session.revision = int(session.revision or 0) + 1
        session.finalization_job_key = None
        session.last_processed_message_id = latest_message.id
        inbound_message.status = "received"
        inbound_message.processing_step = "whatsapp_agent_handoff_waiting"
        db.commit()
        return {"ok": True, "status": "human_handoff", "session_id": session.id}
    if (
        session.last_processed_message_id == latest_message.id
        and inbound_message.processing_step != "whatsapp_media_pending"
    ):
        return {"ok": True, "skipped": True, "reason": "turn_already_processed", "session_id": session.id}

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
    if media_state == "unreadable":
        session.last_processed_message_id = latest_message.id
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason=f"No se pudo interpretar el adjunto {media_filename}.",
        )
        inbound_message.status = "received"
        inbound_message.processing_step = "whatsapp_media_handoff"
        db.commit()
        return {"ok": True, "status": "human_handoff", "session_id": session.id}

    prior_draft = _json_object(session.draft_json, {})
    prior_missing = _json_object(session.missing_fields_json, [])
    if _confirms_current_order_summary(
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
            reason="Se alcanzÃ³ el lÃ­mite de turnos de IA configurado.",
        )
        session.last_processed_message_id = latest_message.id
        session.last_customer_message_at = message_at
        session.due_at = message_at + timedelta(hours=config.idle_session_hours)
        session.revision = int(session.revision or 0) + 1
        session.finalization_job_key = None
        inbound_message.status = "received"
        inbound_message.processing_step = "whatsapp_agent_turn_limit"
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
                    body="Voy a pasar tu consulta a una persona del equipo para que pueda ayudarte.",
                    semantic_state="human_handoff",
                    assistant_session_id=session.id,
                    draft_revision=session.revision,
                )
            except Exception as exc:  # noqa: BLE001
                send_result = {"sent": False, "reason": type(exc).__name__}
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
    recent_transcript = _bounded_recent_transcript(messages)
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
                inbound_message=latest_message,
                input_reference=f"whatsapp-session:{session.id}:revision:{session.revision}",
            )
    except Exception as exc:  # noqa: BLE001
        _set_handoff(
            db,
            session=session,
            conversation=conversation,
            reason="No se pudo evaluar el mensaje con IA.",
        )
        session.last_processed_message_id = latest_message.id
        session.last_error = type(exc).__name__
        inbound_message.status = "doubtful"
        inbound_message.processing_step = "whatsapp_agent_error"
        inbound_message.processing_error = "No se pudo evaluar el mensaje con IA."
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
        return {"ok": True, "status": "human_handoff", "session_id": session.id}

    allowed_evidence = {item["id"] for item in products}
    allowed_evidence.update(item["id"] for item in faq_entries)
    invalid_evidence = set(evaluation.evidence_ids) - allowed_evidence
    clean_draft, missing = _canonicalize_draft(
        evaluation.draft,
        products=products,
        semantic_missing=evaluation.missing_or_uncertain,
        intent=evaluation.intent,
        messages=[item for item in messages if item.direction == "inbound"],
    )
    line_limit_exceeded = any(
        "supera el límite de 30 líneas" in item.casefold() for item in missing
    )
    preserve_existing_order = evaluation.intent in {"question", "order_question", "other"} and bool(prior_draft.get("lines"))
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
    if evaluation.confidence < 0.45 and not evaluation.contextual_order_followup:
        action = "handoff"
    if evaluation.intent in {"question", "other"} and evaluation.reply_needed and not grounded_answer:
        action = "handoff"
    if evaluation.intent == "order_question" and evaluation.reply_needed and not grounded_answer and not missing:
        action = "handoff"
    if (
        action != "handoff"
        and close_requested
        and evaluation.intent in {"order", "order_question"}
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
    if action == "handoff":
        safe_reply = (
            "El pedido o el PDF es demasiado extenso para revisar todas sus líneas automáticamente. "
            "Lo paso al equipo para que lo compruebe completo."
            if context_limit_exceeded
            else "El pedido tiene más de 30 líneas; lo paso al equipo para revisar el documento completo."
            if line_limit_exceeded
            else "Voy a pasar tu consulta a una persona del equipo para que pueda ayudarte."
        )
    elif preserve_existing_order and evaluation.reply_needed and grounded_answer:
        continuation = _order_summary_reply(clean_draft) if complete else _clarification_reply(missing)
        safe_reply = f"{grounded_answer}\n\n{continuation}"
    elif evaluation.intent == "order_question" and grounded_answer:
        continuation = _order_summary_reply(clean_draft) if complete else _clarification_reply(missing)
        safe_reply = f"{grounded_answer}\n\n{continuation}"
    elif evaluation.intent in {"order", "order_question"} and not complete:
        safe_reply = _clarification_reply(missing)
    elif evaluation.intent in {"order", "order_question"} and complete:
        safe_reply = _order_summary_reply(clean_draft)
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
        and evaluation.intent in {"order", "order_question"}
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
                    send_result = {"sent": False, "reason": "send_failed"}
            else:
                send_result = {"sent": False, "reason": str(window.get("reason") or "response_window_closed")}
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

    return {
        "ok": True,
        "status": session.status,
        "session_id": session.id,
        "intent": session.intent,
        "missing_or_uncertain": missing,
        "reply_needed": bool(reply),
        "auto_response": send_result,
    }


def await_send_automatic_response(**kwargs) -> dict[str, Any]:
    """Synchronously bridge the async Meta sender from the worker process."""

    import asyncio

    return asyncio.run(send_automatic_response(**kwargs))


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
