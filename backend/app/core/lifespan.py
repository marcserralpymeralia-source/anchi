from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.core.config import get_settings
from app.core.pilot_configuration import pilot_profile_state
from app.core.schema_readiness import validate_runtime_schema
from app.master.database import MasterSessionLocal  # compatibility seam for test fixtures
from app.workers.jobs_worker import start_job_worker
from app.workers.email_worker import start_email_sync_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    settings = get_settings()
    schema_state = validate_runtime_schema()
    logger.info("Runtime schema verified master=%s tenant_count=%s", schema_state["master"], len(schema_state["tenants"]))
    running_on_vercel = os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))
    if running_on_vercel:
        logger.info("Workers disabled in this process: serverless=%s", running_on_vercel)
    else:
        disable_internal_workers = pilot_profile_state() != "legacy"
        if settings.run_internal_email_worker and not disable_internal_workers:
            start_email_sync_worker()
        else:
            logger.info("Email worker disabled by pilot profile or RUN_INTERNAL_EMAIL_WORKER")
        if settings.run_internal_job_worker and not disable_internal_workers:
            start_job_worker()
        else:
            logger.info("Job worker disabled by pilot profile or RUN_INTERNAL_JOB_WORKER")
    yield
