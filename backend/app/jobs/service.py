from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db.models import BackgroundJob

ACTIVE_JOB_STATUSES = {"queued", "running", "retrying"}
DEFAULT_MAX_RETRIES = {
    "email_sync": 5,
    "process_recent_emails": 5,
    "backfill_imap": 5,
    "process_pending_emails": 4,
    "process_email": 3,
    "process_order": 3,
    "export_order": 3,
    "export_order_ftp": 3,
    "bulk_order_action": 2,
    "import_confirm": 1,
    "import_file": 1,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dumps(payload: dict | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _loads(payload_json: str | None) -> dict:
    if not payload_json:
        return {}
    try:
        data = json.loads(payload_json)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_dedupe_key(job_type: str, payload: dict | None = None) -> str:
    raw_payload = _dumps(payload or {})
    digest = hashlib.sha1(f"{job_type}:{raw_payload or '{}'}".encode("utf-8")).hexdigest()
    return digest


def enqueue_job(
    db: Session,
    *,
    company_id: int,
    job_type: str,
    payload: dict | None = None,
    created_by_user_id: int | None = None,
    dedupe_key: str | None = None,
    max_retries: int | None = None,
) -> BackgroundJob:
    normalized_payload = payload or {}
    key = dedupe_key or build_dedupe_key(job_type, normalized_payload)
    existing = db.scalars(
        select(BackgroundJob).where(
            BackgroundJob.company_id == company_id,
            BackgroundJob.job_type == job_type,
            BackgroundJob.dedupe_key == key,
            BackgroundJob.status.in_(tuple(ACTIVE_JOB_STATUSES)),
        )
    ).first()
    if existing:
        return existing
    job = BackgroundJob(
        company_id=company_id,
        job_type=job_type,
        dedupe_key=key,
        status="queued",
        payload_json=_dumps(normalized_payload),
        created_by_user_id=created_by_user_id,
        max_retries=max(1, int(max_retries or DEFAULT_MAX_RETRIES.get(job_type, 3))),
        queued_at=_now(),
        attempt_count=0,
        updated_at=_now(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def list_jobs(
    db: Session,
    company_id: int,
    status: str | None = None,
    limit: int = 50,
    job_type: str | None = None,
    search: str | None = None,
) -> list[BackgroundJob]:
    stmt = select(BackgroundJob).where(BackgroundJob.company_id == company_id)
    if status and status != "all":
        stmt = stmt.where(BackgroundJob.status == status)
    if job_type and job_type != "all":
        stmt = stmt.where(BackgroundJob.job_type == job_type)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            or_(
                BackgroundJob.job_type.ilike(like),
                BackgroundJob.status.ilike(like),
                BackgroundJob.error_message.ilike(like),
                BackgroundJob.last_error_type.ilike(like),
                BackgroundJob.dedupe_key.ilike(like),
                BackgroundJob.payload_json.ilike(like),
                BackgroundJob.result_json.ilike(like),
            )
        )
    return db.scalars(stmt.order_by(BackgroundJob.queued_at.desc()).limit(max(min(limit, 200), 1))).all()


def get_job(db: Session, company_id: int, job_id: int) -> BackgroundJob | None:
    job = db.get(BackgroundJob, job_id)
    if not job or job.company_id != company_id:
        return None
    return job


def cancel_job(db: Session, company_id: int, job_id: int) -> BackgroundJob | None:
    job = get_job(db, company_id, job_id)
    if not job:
        return None
    if job.status in {"success", "failed", "cancelled"}:
        return job
    job.status = "cancelled"
    job.finished_at = _now()
    job.updated_at = _now()
    job.lock_until = None
    job.lock_owner = None
    job.next_retry_at = None
    db.commit()
    db.refresh(job)
    return job


def claim_next_job(db: Session, *, owner: str, job_types: set[str] | None = None) -> BackgroundJob | None:
    now = _now()
    conditions = [
        BackgroundJob.status.in_(("queued", "retrying")),
        or_(BackgroundJob.lock_until.is_(None), BackgroundJob.lock_until <= now),
        or_(BackgroundJob.next_retry_at.is_(None), BackgroundJob.next_retry_at <= now),
    ]
    if job_types:
        conditions.append(BackgroundJob.job_type.in_(sorted(job_types)))
    candidate_id = db.scalar(
        select(BackgroundJob.id)
        .where(*conditions)
        .order_by(BackgroundJob.queued_at.asc(), BackgroundJob.created_at.asc())
        .limit(1)
    )
    if not candidate_id:
        return None
    result = db.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.id == candidate_id,
            BackgroundJob.status.in_(("queued", "retrying")),
            or_(BackgroundJob.lock_until.is_(None), BackgroundJob.lock_until <= now),
            or_(BackgroundJob.next_retry_at.is_(None), BackgroundJob.next_retry_at <= now),
        )
        .values(
            status="running",
            started_at=now,
            lock_owner=owner,
            lock_until=now + timedelta(minutes=5),
            attempt_count=BackgroundJob.attempt_count + 1,
            last_heartbeat_at=now,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(BackgroundJob, candidate_id)


def finish_job(db: Session, job: BackgroundJob, result: dict | None = None) -> BackgroundJob:
    now = _now()
    job.status = "success"
    job.result_json = _dumps(result or {})
    job.error_message = None
    job.last_error_at = None
    job.last_error_type = None
    job.next_retry_at = None
    job.finished_at = now
    job.updated_at = now
    job.progress = 100
    job.lock_owner = None
    job.lock_until = None
    job.last_heartbeat_at = now
    db.commit()
    db.refresh(job)
    return job


def fail_job(db: Session, job: BackgroundJob, error_message: str, *, retry: bool = False, error_type: str | None = None) -> BackgroundJob:
    now = _now()
    backoff_seconds = min(300, 15 * (2 ** max(job.retry_count, 0)))
    if retry and job.retry_count < max(1, job.max_retries):
        job.status = "retrying"
        job.retry_count += 1
        job.lock_owner = None
        job.lock_until = None
        job.next_retry_at = now + timedelta(seconds=backoff_seconds)
    else:
        job.status = "failed"
        job.finished_at = now
        job.lock_owner = None
        job.lock_until = None
        job.next_retry_at = None
    job.error_message = error_message[:2000]
    job.last_error_at = now
    job.last_error_type = error_type or ("retryable" if retry else "fatal")
    job.last_heartbeat_at = now
    job.updated_at = now
    db.commit()
    db.refresh(job)
    return job


def retry_job(db: Session, company_id: int, job_id: int) -> BackgroundJob | None:
    job = get_job(db, company_id, job_id)
    if not job or job.status == "running":
        return None
    now = _now()
    job.status = "queued"
    job.error_message = None
    job.progress = 0
    job.started_at = None
    job.finished_at = None
    job.queued_at = now
    job.retry_count += 1
    job.next_retry_at = now
    job.last_error_at = None
    job.last_error_type = None
    job.lock_owner = None
    job.lock_until = None
    job.last_heartbeat_at = now
    job.updated_at = now
    db.commit()
    db.refresh(job)
    return job


def update_job_progress(db: Session, job: BackgroundJob, progress: int) -> BackgroundJob:
    job.progress = max(0, min(int(progress), 100))
    job.last_heartbeat_at = _now()
    job.updated_at = _now()
    db.commit()
    db.refresh(job)
    return job


def job_payload(job: BackgroundJob) -> dict:
    return _loads(job.payload_json)
