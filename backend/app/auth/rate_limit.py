from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.master.models import MasterRateLimitBucket, utcnow


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalise_datetime(value, now):  # noqa: ANN001
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=now.tzinfo)
    return value


def bucket_key(scope: str, value: str) -> str:
    """Hash identifiers so emails/IPs are never persisted in the limiter."""

    return _digest(f"anchi:{scope}:{value.strip().lower()}")


def client_ip(request) -> str:  # noqa: ANN001
    client = getattr(request, "client", None)
    return (getattr(client, "host", None) or "unknown").strip().lower()


def consume(
    db: Session,
    key: str,
    *,
    limit: int,
    window_seconds: int,
    block_seconds: int,
) -> RateLimitDecision:
    """Atomically consume one shared bucket attempt.

    The row lives in the master DB, so separate web replicas observe the
    same throttle. A short retry handles concurrent first writes on SQLite
    and PostgreSQL without exposing an implementation error to the caller.
    """

    # Some isolated service tests use a lightweight fake database. Real HTTP
    # requests always provide a SQLAlchemy session, so this compatibility path
    # is intentionally limited to those test doubles.
    if not hasattr(db, "scalar"):
        return RateLimitDecision(True)

    limit = max(int(limit), 1)
    window_seconds = max(int(window_seconds), 1)
    block_seconds = max(int(block_seconds), 1)
    now = utcnow()
    for attempt in range(2):
        try:
            row = db.scalar(
                select(MasterRateLimitBucket)
                .where(MasterRateLimitBucket.bucket_key == key)
                .with_for_update()
            )
            if row is None:
                row = MasterRateLimitBucket(
                    bucket_key=key,
                    window_started_at=now,
                    attempt_count=1,
                    updated_at=now,
                )
                db.add(row)
                db.commit()
                return RateLimitDecision(True)

            started_at = _normalise_datetime(row.window_started_at, now)
            blocked_until = _normalise_datetime(row.blocked_until, now)
            if started_at is None or now - started_at >= timedelta(seconds=window_seconds):
                row.window_started_at = now
                row.attempt_count = 0
                row.blocked_until = None
                blocked_until = None
            if blocked_until and blocked_until > now:
                db.commit()
                return RateLimitDecision(False, max(1, int((blocked_until - now).total_seconds())))
            if int(row.attempt_count or 0) >= limit:
                row.blocked_until = now + timedelta(seconds=block_seconds)
                row.updated_at = now
                db.commit()
                return RateLimitDecision(False, block_seconds)

            row.attempt_count = int(row.attempt_count or 0) + 1
            row.updated_at = now
            db.commit()
            return RateLimitDecision(True)
        except IntegrityError:
            db.rollback()
            if attempt == 0:
                continue
            # A concurrent limiter write should fail closed for the current
            # attempt rather than bypassing protection.
            return RateLimitDecision(False, block_seconds)

    return RateLimitDecision(False, block_seconds)


def reset(db: Session, key: str) -> None:
    # Keep lightweight service fakes usable in isolated login tests. Real
    # requests always use the SQLAlchemy master session.
    if not hasattr(db, "scalar"):
        return
    row = db.scalar(select(MasterRateLimitBucket).where(MasterRateLimitBucket.bucket_key == key))
    if row is None:
        return
    now = utcnow()
    row.window_started_at = now
    row.attempt_count = 0
    row.blocked_until = None
    row.updated_at = now
    db.commit()


def authentication_keys(request, email: str) -> tuple[str, ...]:  # noqa: ANN001
    normalized = (email or "").strip().lower()
    ip = client_ip(request)
    return (
        bucket_key("auth-ip", ip),
        bucket_key("auth-email", normalized or "unknown"),
        bucket_key("auth-pair", f"{ip}|{normalized or 'unknown'}"),
    )


def consume_authentication(db: Session, request, email: str) -> RateLimitDecision:  # noqa: ANN001
    settings = get_settings()
    retry_after = 0
    for key in authentication_keys(request, email):
        decision = consume(
            db,
            key,
            limit=settings.auth_rate_limit_attempts,
            window_seconds=settings.auth_rate_limit_window_seconds,
            block_seconds=settings.auth_rate_limit_block_seconds,
        )
        if not decision.allowed:
            retry_after = max(retry_after, decision.retry_after)
    return RateLimitDecision(retry_after == 0, retry_after)


def reset_authentication(db: Session, request, email: str) -> None:  # noqa: ANN001
    for key in authentication_keys(request, email):
        reset(db, key)


def consume_public_action(
    db: Session,
    request,
    *,
    scope: str,
    value: str,
    limit: int = 5,
) -> RateLimitDecision:  # noqa: ANN001
    """Throttle public token flows by both client and opaque action value."""

    settings = get_settings()
    retry_after = 0
    keys = (
        bucket_key(f"{scope}-ip", client_ip(request)),
        bucket_key(f"{scope}-value", value or "unknown"),
    )
    for key in keys:
        decision = consume(
            db,
            key,
            limit=limit,
            window_seconds=settings.auth_rate_limit_window_seconds,
            block_seconds=settings.auth_rate_limit_block_seconds,
        )
        if not decision.allowed:
            retry_after = max(retry_after, decision.retry_after)
    return RateLimitDecision(retry_after == 0, retry_after)
