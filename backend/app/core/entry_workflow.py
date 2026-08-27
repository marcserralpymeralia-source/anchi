from __future__ import annotations

from typing import Any

from app.db.models import Email, InboundMessage, User
from app.jobs.service import enqueue_job
from app.logs.service import log_action

CANONICAL_STATUS_ALIASES = {
    "pending": "not_processed",
    "not_processed": "not_processed",
    "queued": "not_processed",
    "processing": "processing",
    "pending_reprocess": "processing",
    "processed": "processed",
    "order_detected": "processed",
    "processed_order_detected": "processed",
    "processed_no_order": "no_order",
    "no_order": "no_order",
    "doubtful": "review",
    "review": "review",
    "processed_doubtful": "review",
    "error": "error",
    "processing_error": "error",
    "closed": "closed",
    "cerrado": "closed",
    "discarded": "discarded",
    "descartado": "discarded",
    "archived": "archived",
    "received": "not_processed",
    "matched": "processed",
    "no_pedido": "no_order",
    "dudoso": "review",
}

ENTRY_STATUS_LABELS = {
    "not_processed": "Pendiente de procesar",
    "processing": "Procesando",
    "review": "Pendiente de validar",
    "processed": "Procesado",
    "error": "Error",
    "no_order": "No es pedido",
    "closed": "Cerrado",
    "discarded": "Descartado",
    "archived": "Archivado",
}

ENTRY_OPERATIONAL_CATEGORY = {
    "not_processed": "pending",
    "processing": "pending",
    "review": "review",
    "processed": "processed",
    "error": "error",
    "no_order": "no_order",
    "closed": "processed",
    "discarded": "discarded",
    "archived": "archived",
}


def canonicalize_entry_status(status: str | None, *, fallback: str = "not_processed") -> str:
    value = (status or fallback or "not_processed").strip().lower()
    if value.startswith("error"):
        return "error"
    mapped = CANONICAL_STATUS_ALIASES.get(value)
    if mapped is not None:
        return mapped
    if value in {"not_processed", "processing", "review", "processed", "error", "no_order", "closed", "discarded", "archived"}:
        return value
    return fallback


def canonical_email_status(email: Email | None = None, *, raw_status: str | None = None, agent_status: str | None = None, detected_type: str | None = None, archived: bool | None = None) -> str:
    if email is not None:
        raw_status = raw_status or (email.status or "")
        agent_status = agent_status or (email.agent_status or "")
        detected_type = detected_type or email.detected_type
        archived = archived if archived is not None else bool(getattr(email, "archived", False))

    if archived is True:
        return "archived"

    text = (raw_status or "").strip().lower()
    agent = (agent_status or "").strip().lower()
    detected = (detected_type or "").strip().lower()

    if text in {"descartado", "discardado"} or agent == "discarded":
        return "discarded"
    if text in {"cerrado", "closed"} or agent == "closed":
        return "closed"
    if text.startswith("error") or agent.startswith("error") or agent == "processing_error":
        return "error"
    if detected == "no_pedido" or text == "no_pedido" or agent == "processed_no_order":
        return "no_order"
    if text in {"dudoso", "review"} or agent in {"doubtful", "processed_doubtful", "pending_reprocess", "review"}:
        return "review"
    if agent in {"processed_order_detected", "processed", "order_detected"} or text in {"processed", "pedido_confirmado", "pedido_validado", "processed_order_detected"}:
        return "processed"
    if agent in {"not_processed", "queued", "pending"} or text in {"pending", "not_processed", "queued"}:
        return "not_processed"
    if agent == "processing" or text == "processing":
        return "processing"

    return canonicalize_entry_status(text or agent or detected, fallback="not_processed")


def canonical_inbound_status(message: InboundMessage | None = None, *, raw_status: str | None = None, order_id: int | None = None) -> str:
    if message is not None:
        raw_status = raw_status or (message.status or "")
        order_id = order_id if order_id is not None else message.order_id

    status = (raw_status or "").strip().lower()
    if status in {"received", "queued"}:
        return "not_processed"
    if status == "processing":
        return "processing"
    if status in {"matched", "order_detected"} or order_id:
        return "processed"
    if status in {"doubtful", "review"}:
        return "review"
    if status == "no_order":
        return "no_order"
    if status.startswith("error"):
        return "error"
    if status == "processed":
        return "processed"
    if status in {"discarded", "descartado"}:
        return "discarded"
    if status in {"closed", "cerrado"}:
        return "closed"
    return canonicalize_entry_status(status, fallback="not_processed")


def entry_status_label(status: str | None) -> str:
    key = canonicalize_entry_status(status)
    return ENTRY_STATUS_LABELS.get(key, key.replace("_", " ").title())


def entry_operational_category(status: str | None) -> str:
    key = canonicalize_entry_status(status)
    return ENTRY_OPERATIONAL_CATEGORY.get(key, "pending")


def queue_email_processing(db, *, company_id: int, user_id: int, email_id: int, force: bool = False):
    payload = {"email_id": email_id}
    if force:
        payload["force"] = True
    return enqueue_job(db, company_id=company_id, job_type="process_email", payload=payload, created_by_user_id=user_id)


def queue_inbound_processing(db, *, company_id: int, user_id: int, inbound_message_id: int, source_kind: str | None = None, source_provider: str | None = None):
    source = db.get(InboundMessage, inbound_message_id)
    if source is None or source.company_id != company_id:
        return None
    payload = {"inbound_message_id": inbound_message_id, "channel": source_kind or "inbound", "source": (source_provider or source.provider or "manual_import")}
    return enqueue_job(db, company_id=company_id, job_type="process_inbound_message", payload=payload, created_by_user_id=user_id)


def mark_email_no_order(db, *, company_id: int, user_id: int, email_id: int):
    email = db.get(Email, email_id)
    if email is None or email.company_id != company_id:
        return None
    email.status = "no_pedido"
    email.agent_status = "processed_no_order"
    email.detected_type = "no_pedido"
    email.processing_error = None
    db.commit()
    log_action(db, company_id=company_id, user=db.get(User, user_id), action="entry.mark_no_order", entity_type="email", entity_id=email.id, message="Correo marcado como no pedido")
    return email


def close_email(db, *, company_id: int, user_id: int, email_id: int):
    email = db.get(Email, email_id)
    if email is None or email.company_id != company_id:
        return None
    email.status = "cerrado"
    if email.detected_type == "no_pedido":
        email.agent_status = "processed_no_order"
    db.commit()
    log_action(db, company_id=company_id, user=db.get(User, user_id), action="entry.close", entity_type="email", entity_id=email.id, message="Correo cerrado")
    return email


def discard_email(db, *, company_id: int, user_id: int, email_id: int):
    email = db.get(Email, email_id)
    if email is None or email.company_id != company_id:
        return None
    email.status = "descartado"
    email.agent_status = "discarded"
    db.commit()
    log_action(db, company_id=company_id, user=db.get(User, user_id), action="entry.discard", entity_type="email", entity_id=email.id, message="Correo descartado")
    return email


def reprocess_email(db, *, company_id: int, user_id: int, email_id: int):
    email = db.get(Email, email_id)
    if email is None or email.company_id != company_id:
        return None
    job = queue_email_processing(db, company_id=company_id, user_id=user_id, email_id=email_id, force=True)
    log_action(db, company_id=company_id, user=db.get(User, user_id), action="entry.reprocess", entity_type="job", entity_id=job.id, message=f"Correo reencolado: {email_id}")
    return job
