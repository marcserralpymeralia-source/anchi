"""Tenant-scoped audit records for connector lifecycle and delivery activity."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.observability import current_context, encode_structured_message, redact_sensitive_data
from app.db.models import AuditLog


_AUDIT_ACTION_PREFIXES = ("connection.", "delivery.", "http.")


def append_integration_audit(
    db: Session,
    *,
    company_id: int,
    event_type: str,
    outcome: str,
    message: str,
    connection_id: int | None = None,
    connector_key: str | None = None,
    delivery_id: int | None = None,
    order_id: int | None = None,
    correlation_id: str | None = None,
    destination_hostname: str | None = None,
    http_status: int | None = None,
    duration_ms: int | None = None,
    error_category: str | None = None,
    actor_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one safe event without committing the caller's transaction."""

    context = current_context()
    details: dict[str, Any] = dict(metadata or {})
    details.update(
        {
            "event_type": event_type,
            "outcome": outcome,
            "connection_id": connection_id,
            "connector_key": connector_key,
            "delivery_id": delivery_id,
            "order_id": order_id,
            "correlation_id": correlation_id or context.get("correlation_id"),
            "destination_hostname": destination_hostname,
            "http_status": http_status,
            "duration_ms": duration_ms,
            "error_category": error_category,
        }
    )
    details = {key: value for key, value in details.items() if value is not None}
    log = AuditLog(
        company_id=company_id,
        user_id=actor_id if actor_id is not None else context.get("user_id"),
        action=str(event_type)[:120],
        entity_type="order_delivery" if delivery_id is not None else ("integration_connection" if connection_id is not None else "integration"),
        entity_id=delivery_id if delivery_id is not None else connection_id,
        message=encode_structured_message(str(message), metadata=redact_sensitive_data(details)),
    )
    db.add(log)
    return log


def append_secure_http_audit(db: Session, event: Any, *, delivery_id: int | None = None, order_id: int | None = None) -> AuditLog:
    """Persist a ``SecureHTTPTransport`` event through the tenant audit model."""

    return append_integration_audit(
        db,
        company_id=int(event.tenant_id),
        connection_id=int(event.connection_id),
        connector_key=str(event.connector_key),
        delivery_id=delivery_id,
        order_id=order_id,
        event_type=str(event.event_type),
        outcome=str(event.outcome),
        message="Actividad HTTP externa registrada.",
        correlation_id=str(event.correlation_id),
        destination_hostname=event.destination_hostname,
        http_status=event.http_status,
        duration_ms=event.duration_ms,
        error_category=event.error_category,
        metadata={"operation": event.operation, "request_id": event.request_id},
    )


def list_integration_audit(db: Session, *, company_id: int, limit: int = 100) -> list[AuditLog]:
    prefixes = [AuditLog.action.like(f"{prefix}%") for prefix in _AUDIT_ACTION_PREFIXES]
    return db.scalars(
        select(AuditLog)
        .where(AuditLog.company_id == company_id, or_(*prefixes))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(max(1, min(int(limit), 200)))
    ).all()


def integration_audit_to_dict(log: AuditLog) -> dict[str, Any]:
    from app.core.observability import decode_structured_message

    parsed = decode_structured_message(log.message)
    return {
        "id": log.id,
        "created_at": log.created_at.isoformat() if log.created_at else None,
        "action": log.action,
        "entity_type": log.entity_type,
        "entity_id": log.entity_id,
        "message": parsed["message"],
        "context": parsed["context"],
        "metadata": parsed["metadata"],
    }


__all__ = ["append_integration_audit", "append_secure_http_audit", "integration_audit_to_dict", "list_integration_audit"]
