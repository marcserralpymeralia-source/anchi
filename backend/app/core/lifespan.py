from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.master.database import MasterSessionLocal, engine as master_engine, init_master_db
from app.master.bootstrap import ensure_platform_admin
from app.master.migrations import upgrade_master_schema
from app.master.models import EmailSyncState, MasterTenantDatabase, MasterUser
from app.master.provisioning import synchronize_tenant_actors
from app.tenancy.database import ensure_tenant_schema_once
from app.workers.jobs_worker import start_job_worker
from app.workers.email_worker import start_email_sync_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    settings = get_settings()
    master_db = None
    master_db_ready = True
    try:
        # Keep the historical initialization seam for startup failure handling
        # and tests; the registry then applies safe, idempotent upgrades.
        init_master_db()
        upgrade_master_schema(master_engine)
        master_db = MasterSessionLocal()
        ensure_platform_admin(master_db)
        if settings.environment in {"development", "demo"}:
            platform_admin = master_db.scalar(select(MasterUser).where(MasterUser.email == settings.default_admin_email))
            if platform_admin and not platform_admin.platform_role_key:
                platform_admin.platform_role_key = "superadmin"
                platform_admin.email_verified = True
                master_db.commit()
        tenants = master_db.scalars(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.is_active.is_(True),
                MasterTenantDatabase.database_url.is_not(None),
            )
        ).all()
        for tenant in tenants:
            database_url = tenant.database_url
            if not isinstance(database_url, str) or not database_url.strip():
                tenant.health_status = "error"
                master_db.commit()
                logger.warning(
                    "Tenant omitido durante el arranque: URL de base de datos no disponible company_id=%s",
                    tenant.company_id,
                )
                continue
            try:
                # Tenant migrations are a provisioning/startup concern, never
                # part of the latency of an authenticated web request.
                ensure_tenant_schema_once(database_url, company_id=tenant.company_id)
                tenant.health_status = "ok"
                master_db.commit()
                synchronize_tenant_actors(master_db, tenant)
            except Exception:  # noqa: BLE001
                tenant.health_status = "error"
                master_db.commit()
                logger.exception("No se pudieron sincronizar los actores del tenant company_id=%s", tenant.company_id)
            state = master_db.scalar(
                select(EmailSyncState).where(
                    EmailSyncState.company_id == tenant.company_id,
                    EmailSyncState.channel_key == "email",
                )
            )
            if not state:
                master_db.add(
                    EmailSyncState(
                        company_id=tenant.company_id,
                        channel_key="email",
                        enabled=True,
                        frequency_seconds=60,
                        status="idle",
                        next_run_at=datetime.now(timezone.utc),
                    )
                )
                master_db.commit()
        if settings.enable_legacy_sync:
            logger.info("Legacy sync enabled explicitly")
    except SQLAlchemyError:
        master_db_ready = False
        logger.exception("No se pudo inicializar la base master en el arranque; la app arrancará en modo degradado")
    finally:
        if master_db is not None:
            master_db.close()
    running_on_vercel = os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))
    if running_on_vercel or not master_db_ready:
        logger.info("Workers disabled in this process: serverless=%s master_db_ready=%s", running_on_vercel, master_db_ready)
    else:
        if settings.run_internal_email_worker:
            start_email_sync_worker()
        else:
            logger.info("Email worker disabled by RUN_INTERNAL_EMAIL_WORKER")
        if settings.run_internal_job_worker:
            start_job_worker()
        else:
            logger.info("Job worker disabled by RUN_INTERNAL_JOB_WORKER")
    yield
