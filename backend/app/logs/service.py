from sqlalchemy.orm import Session

from app.db.models import AuditLog, User


def log_action(
    db: Session,
    *,
    company_id: int,
    user: User | None,
    action: str,
    message: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
) -> None:
    db.add(
        AuditLog(
            company_id=company_id,
            user_id=user.id if user else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            message=message,
        )
    )
    db.commit()
