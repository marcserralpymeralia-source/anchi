from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.master.models import MasterUser, MasterUserSession, utcnow

SESSION_KEY = "server_session_id"


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_server_session(db: Session, request, user: MasterUser, *, company_id: int | None = None, membership_id: int | None = None) -> str:
    settings = get_settings()
    raw_token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(
        MasterUserSession(
            session_token_hash=_digest(raw_token),
            user_id=user.id,
            session_version=int(user.session_version or 1),
            company_id=company_id,
            membership_id=membership_id,
            session_data_json="{}",
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=int(settings.session_max_age or 604800)),
            user_agent=(request.headers.get("user-agent") or "")[:500] or None,
            ip_address=(request.client.host if request.client else None),
        )
    )
    db.commit()
    return raw_token


def rotate_server_session(
    db: Session,
    request,
    user: MasterUser,
    *,
    company_id: int | None = None,
    membership_id: int | None = None,
) -> str:
    """Rotate the opaque session when its tenant context changes."""

    revoke_server_session(db, request)
    raw_token = create_server_session(
        db,
        request,
        user,
        company_id=company_id,
        membership_id=membership_id,
    )
    request.session[SESSION_KEY] = raw_token
    return raw_token


def bind_server_session_context(
    db: Session,
    request,
    *,
    company_id: int | None,
    membership_id: int | None,
) -> None:
    """Bind the selected company to an existing login session."""

    raw_token = (request.scope.get("session") or {}).get(SESSION_KEY)
    row = get_session(db, raw_token)
    if row is None:
        return
    row.company_id = company_id
    row.membership_id = membership_id
    db.commit()


def get_session(db: Session, raw_token: str | None) -> MasterUserSession | None:
    if not raw_token:
        return None
    return db.scalar(select(MasterUserSession).where(MasterUserSession.session_token_hash == _digest(raw_token)))


def validate_server_session(request, db: Session, user: MasterUser) -> bool:
    """Validate a session record when one is present.

    Development/test fixtures may still construct a legacy signed session
    directly. Production and demo deployments require the server-side record.
    """

    raw_token = (request.scope.get("session") or {}).get(SESSION_KEY)
    if not raw_token:
        return get_settings().environment not in {"production", "demo"}
    row = get_session(db, raw_token)
    now = utcnow()
    expires_at = row.expires_at if row is not None else None
    last_seen_at = row.last_seen_at if row is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if last_seen_at is not None and last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    if (
        row is None
        or row.user_id != user.id
        or row.revoked_at is not None
        or expires_at <= now
        or int(row.session_version or 1) != int(user.session_version or 1)
        or not user.is_active
    ):
        return False
    session = request.scope.get("session") or {}
    if row.company_id is not None and session.get("company_id") != row.company_id:
        return False
    if row.membership_id is not None and session.get("membership_id") != row.membership_id:
        return False
    # Avoid a write on every request while keeping activity useful for the
    # platform panel. A stale heartbeat is enough for operational reporting.
    if not last_seen_at or (now - last_seen_at).total_seconds() >= 300:
        row.last_seen_at = now
        db.commit()
    return True


def revoke_server_session(db: Session, request) -> None:
    raw_token = (request.scope.get("session") or {}).get(SESSION_KEY)
    row = get_session(db, raw_token)
    if row is not None and row.revoked_at is None:
        row.revoked_at = utcnow()
        db.commit()
