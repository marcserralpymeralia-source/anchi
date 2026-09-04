from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditLog, User
from app.core.observability import current_context, encode_structured_message, redact_sensitive_data


def _audit_user_id(db: Session, user: User | None) -> int | None:
    if not user:
        return None
    user_id = getattr(user, "id", None)
    if user_id is None:
        return None
    try:
        if db.get(User, user_id) is None:
            return None
    except Exception:
        return None
    return user_id


def log_action(
    db: Session,
    *,
    company_id: int,
    user: User | None,
    action: str,
    message: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    metadata: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            company_id=company_id,
            user_id=_audit_user_id(db, user),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            message=encode_structured_message(message, metadata=metadata),
        )
    )
    db.commit()


def log_flow_event(
    db: Session,
    *,
    company_id: int,
    event: str,
    stage: str,
    message: str,
    flow_id: str | None = None,
    user: User | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    status: str = "info",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist one safe, correlated event in the tenant audit stream.

    Flow events deliberately store diagnostic metadata rather than message bodies.
    ``encode_structured_message`` applies the central secret redaction rules before
    the event is written to the tenant database.
    """

    details = dict(metadata or {})
    active_flow_id = flow_id or current_context().get("flow_id")
    details.update(
        {
            "event": event,
            "stage": stage,
            "status": status,
            "flow_id": active_flow_id,
        }
    )
    log_action(
        db,
        company_id=company_id,
        user=user,
        action=f"flow.{event}"[:120],
        entity_type=entity_type,
        entity_id=entity_id,
        message=message,
        metadata=details,
    )


def audit_log_text(logs: list[AuditLog]) -> str:
    """Serialize tenant flow/audit events as a portable UTF-8 .log document."""

    lines: list[str] = []
    for log in logs:
        parsed = parse_audit_log_message(log.message)
        context = parsed.get("context") or {}
        metadata = parsed.get("metadata") or {}
        timestamp = log.created_at or datetime.now(timezone.utc)
        record = {
            "timestamp": timestamp.isoformat(),
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "message": parsed.get("message") or "",
            "context": context,
            "metadata": metadata,
        }
        lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
    return "\n".join(lines) + ("\n" if lines else "")


def parse_audit_log_message(message: str | None) -> dict:
    if not message:
        return {"message": "", "context": {}, "metadata": {}}
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return {"message": redact_sensitive_data(message), "context": {}, "metadata": {}}
    if not isinstance(data, dict) or "message" not in data:
        return {"message": redact_sensitive_data(message), "context": {}, "metadata": {}}
    context = data.get("context") or {}
    metadata = data.get("metadata") or {}
    if not isinstance(context, dict):
        context = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "message": redact_sensitive_data(str(data.get("message") or "")),
        "context": redact_sensitive_data(context),
        "metadata": redact_sensitive_data(metadata),
    }
