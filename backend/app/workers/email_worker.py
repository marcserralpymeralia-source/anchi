from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
import app.master.database as master_database
from app.master.models import EmailSyncState, MasterTenantDatabase
from app.workers.email_listener import reconcile_tenant_email

logger = logging.getLogger(__name__)
_worker_started = False


def _state_for_company(master_db: Session, company_id: int) -> EmailSyncState:
    state = master_db.scalar(
        select(EmailSyncState).where(
            EmailSyncState.company_id == company_id,
            EmailSyncState.channel_key == "email",
        )
    )
    if state:
        return state
    state = EmailSyncState(
        company_id=company_id,
        channel_key="email",
        enabled=True,
        frequency_seconds=60,
        status="idle",
        next_run_at=datetime.now(timezone.utc),
    )
    master_db.add(state)
    master_db.commit()
    return state


def _acquire_lock(master_db: Session, state: EmailSyncState, owner: str) -> bool:
    now = datetime.now(timezone.utc)
    result = master_db.execute(
        update(EmailSyncState)
        .where(
            EmailSyncState.id == state.id,
            or_(EmailSyncState.lock_until.is_(None), EmailSyncState.lock_until <= now),
        )
        .values(
            lock_owner=owner,
            lock_until=now + timedelta(minutes=2),
            status="running",
            last_sync_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        master_db.rollback()
        return False
    master_db.commit()
    master_db.refresh(state)
    return True


def _release_lock(
    master_db: Session,
    state: EmailSyncState,
    *,
    owner: str,
    success: bool,
    error: str | None = None,
) -> bool:
    now = datetime.now(timezone.utc)
    values = {
        "lock_owner": None,
        "lock_until": None,
        "next_run_at": now + timedelta(seconds=max(state.frequency_seconds or 60, 30)),
        "updated_at": now,
    }
    if success:
        values.update(
            status="idle",
            last_success_at=now,
            last_error_at=None,
            last_error_message=None,
        )
    else:
        values.update(
            status="error",
            last_error_at=now,
            last_error_message=error,
        )
    result = master_db.execute(
        update(EmailSyncState)
        .where(
            EmailSyncState.id == state.id,
            EmailSyncState.lock_owner == owner,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        master_db.rollback()
        return False
    master_db.commit()
    master_db.refresh(state)
    return True


def _run_due_tenant(master_db: Session, tenant: MasterTenantDatabase, state: EmailSyncState) -> None:
    try:
        reconcile_tenant_email(master_db, tenant, owner="email-worker")
        _release_lock(master_db, state, owner="email-worker", success=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error sincronizando tenant %s: %s", tenant.company_id, exc)
        _release_lock(master_db, state, owner="email-worker", success=False, error=str(exc))


def _worker_loop() -> None:
    settings = get_settings()
    poll_seconds = max(int(getattr(settings, "email_worker_poll_seconds", 15)), 5)
    while True:
        master_db = master_database.MasterSessionLocal()
        try:
            now = datetime.now(timezone.utc)
            due_states = master_db.scalars(
                select(EmailSyncState)
                .join(MasterTenantDatabase, MasterTenantDatabase.company_id == EmailSyncState.company_id)
                .where(
                    MasterTenantDatabase.is_active.is_(True),
                    MasterTenantDatabase.database_url.is_not(None),
                    EmailSyncState.enabled.is_(True),
                    EmailSyncState.channel_key == "email",
                    EmailSyncState.next_run_at.is_not(None),
                    EmailSyncState.next_run_at <= now,
                )
            ).all()
            for state in due_states:
                tenant = master_db.scalar(
                    select(MasterTenantDatabase).where(
                        MasterTenantDatabase.company_id == state.company_id,
                        MasterTenantDatabase.is_active.is_(True),
                    )
                )
                if not tenant or not tenant.database_url:
                    continue
                if not _acquire_lock(master_db, state, owner="email-worker"):
                    continue
                _run_due_tenant(master_db, tenant, state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Email worker error: %s", exc)
        finally:
            master_db.close()
        time.sleep(poll_seconds)


def start_email_sync_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    threading.Thread(target=_worker_loop, name="anchi-email-sync", daemon=True).start()


def is_email_sync_worker_started() -> bool:
    return _worker_started
