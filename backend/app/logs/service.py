from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.db.models import AuditLog, User
from app.core.observability import encode_structured_message


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
            user_id=user.id if user else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            message=encode_structured_message(message, metadata=metadata),
        )
    )
    db.commit()


def parse_audit_log_message(message: str | None) -> dict:
    if not message:
        return {"message": "", "context": {}, "metadata": {}}
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return {"message": message, "context": {}, "metadata": {}}
    if not isinstance(data, dict) or "message" not in data:
        return {"message": message, "context": {}, "metadata": {}}
    context = data.get("context") or {}
    metadata = data.get("metadata") or {}
    if not isinstance(context, dict):
        context = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {"message": str(data.get("message") or ""), "context": context, "metadata": metadata}
