from __future__ import annotations

import os
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.dependencies import current_tenant_user, require_master_admin
from app.admin.diagnostics import company_diagnostics, company_diagnostics_overview
from app.core.metrics import snapshot_metrics
from app.core.operational import operational_status
from app.core.pilot_configuration import (
    PilotConfigurationManifest,
    build_tenant_configuration_manifest,
    compare_runtime_to_approved,
    pilot_profile_state,
)
from app.db.models import IntegrationConnection, OrderRoute
from app.core.storage import check_storage_readiness, configured_storage_backend
from app.master.database import get_master_db
from app.master.migrations import master_migration_report
from app.master.models import MasterCompany, MasterTenantDatabase
from app.tenancy.database import get_tenant_db
from app.tenancy.database import tenant_db_session
from app.tenancy.migrations import tenant_migration_report
from app.workers.email_worker import is_email_sync_worker_started
from app.workers.jobs_worker import is_job_worker_started

router = APIRouter()
logger = logging.getLogger(__name__)


def _ping_db(db: Session) -> bool:
    db.execute(text("SELECT 1"))
    return True


@router.get("/health")
def health(request: Request):
    return health_live(request)


@router.get("/health/live")
def health_live(request: Request):
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
        "metrics": snapshot_metrics(),
    }


@router.get("/health/ready")
def health_ready(request: Request, master_db: Session = Depends(get_master_db), response: Response = None):
    profile_state = pilot_profile_state()
    catalog_pilot = profile_state == "catalog"
    tenant = getattr(request.state, "tenant", None)
    master_schema = master_migration_report(master_db, persist=False)
    storage = check_storage_readiness()
    configuration_manifest = PilotConfigurationManifest.from_runtime(tenant_id=tenant.company.id if tenant else None)
    configuration_manifest = PilotConfigurationManifest(tenant_id=configuration_manifest["tenant_id"]).build(
        commit_sha=configuration_manifest["commit_sha"],
        schema_version=f"master:{master_schema.get('current_version', 'unknown')}",
        profile=configuration_manifest["profile"],
        enabled_modules=configuration_manifest["enabled_modules"],
        disabled_modules=configuration_manifest["disabled_modules"],
        connectors=configuration_manifest["connectors"],
        storage_backend=configuration_manifest["storage_backend"],
        runtime_gates=configuration_manifest["runtime_gates"],
    )
    configuration_drift = compare_runtime_to_approved(configuration_manifest, os.getenv("PILOT_CONFIGURATION_BASELINE_JSON"))
    master_ok = _ping_db(master_db)
    payload = {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "master": master_ok,
        "master_schema_report": master_schema,
        "tenant": bool(tenant),
        "tenant_company_id": tenant.company.id if tenant else None,
        "tenant_slug": tenant.company.slug if tenant else None,
        "tenant_database_configured": bool(tenant and getattr(tenant.company, "database_url", None)),
        "request_id": getattr(request.state, "request_id", None),
        "correlation_id": getattr(request.state, "correlation_id", None) or getattr(request.state, "request_id", None),
        "metrics": snapshot_metrics(),
        "storage_ready": bool(storage["ok"]),
        "storage": storage,
        "pilot_configuration": {
            "profile": configuration_manifest["profile"],
            "storage_backend": configuration_manifest["storage_backend"],
            "runtime_gates": configuration_manifest["runtime_gates"],
        },
        "configuration_manifest_hash": configuration_manifest["hash"],
        "configuration_drift": configuration_drift,
        "workers_ready": {"email_sync": is_email_sync_worker_started(), "jobs": is_job_worker_started()},
    }
    if tenant and tenant.company.database_url:
        session_factory = tenant_db_session(tenant.company.database_url)
        tenant_db = session_factory()
        try:
            payload["tenant_ping"] = _ping_db(tenant_db)
            payload["tenant_schema_report"] = tenant_migration_report(tenant_db, tenant.company.id, persist=False)
            connections = tenant_db.scalars(select(IntegrationConnection).where(IntegrationConnection.company_id == tenant.company.id).order_by(IntegrationConnection.id)).all()
            routes = tenant_db.scalars(select(OrderRoute).where(OrderRoute.company_id == tenant.company.id).order_by(OrderRoute.id)).all()
            configuration_manifest = build_tenant_configuration_manifest(
                company_id=tenant.company.id,
                master_schema_report=master_schema,
                tenant_schema_report=payload["tenant_schema_report"],
                connections=connections,
                routes=routes,
            )
            configuration_drift = compare_runtime_to_approved(configuration_manifest, os.getenv("PILOT_CONFIGURATION_BASELINE_JSON"))
            payload["configuration_manifest_hash"] = configuration_manifest["hash"]
            payload["configuration_drift"] = configuration_drift
            payload["operational"] = operational_status(
                master_db,
                tenant_db=tenant_db,
                storage=storage,
                database_ok=bool(master_ok and payload["tenant_ping"]),
            )
        finally:
            tenant_db.close()
    else:
        payload["operational"] = operational_status(master_db, storage=storage, database_ok=master_ok)
        if catalog_pilot:
            pilot_databases = master_db.scalars(
                select(MasterTenantDatabase)
                .join(MasterCompany, MasterCompany.id == MasterTenantDatabase.company_id)
                .where(
                    MasterTenantDatabase.is_active.is_(True),
                    MasterTenantDatabase.database_url.is_not(None),
                    MasterCompany.active.is_(True),
                    MasterCompany.status == "active",
                )
                .order_by(MasterTenantDatabase.company_id)
            ).all()
            database_results = []
            for pilot_database in pilot_databases:
                tenant_session = tenant_db_session(pilot_database.database_url)()
                try:
                    database_ok = _ping_db(tenant_session)
                    schema = tenant_migration_report(tenant_session, pilot_database.company_id, persist=False)
                    database_results.append(bool(database_ok and schema.get("status") == "current"))
                except Exception as exc:  # noqa: BLE001
                    database_results.append(False)
                    logger.warning("Pilot tenant readiness failed company_id=%s error_type=%s", pilot_database.company_id, exc.__class__.__name__)
                finally:
                    tenant_session.close()
            payload["pilot_tenant_database_count"] = len(pilot_databases)
            payload["tenant_databases_ready"] = bool(database_results) and all(database_results)
    master_schema_status = payload["master_schema_report"].get("status")
    master_schema_ok = master_schema_status == "current"
    payload["master_schema_ok"] = master_schema_ok
    payload["ok"] = bool(payload["master"] and (not tenant or payload.get("tenant_ping", True)) and master_schema_ok and storage["ok"])
    if os.getenv("PILOT_CONFIGURATION_BASELINE_JSON") and configuration_drift.get("critical"):
        payload["ok"] = False
    if profile_state == "invalid":
        payload["ok"] = False
    if catalog_pilot and (
        configuration_drift.get("status") != "MATCH"
        or bool(configuration_drift.get("critical"))
        or storage.get("backend") != "vercel_blob"
        or configured_storage_backend() != "vercel_blob"
        or configuration_manifest["runtime_gates"].get("RUN_INTERNAL_JOB_WORKER")
        or configuration_manifest["runtime_gates"].get("RUN_INTERNAL_EMAIL_WORKER")
    ):
        payload["ok"] = False
    if tenant and tenant.company.database_url:
        tenant_schema_status = payload["tenant_schema_report"].get("status")
        tenant_schema_ok = tenant_schema_status == "current"
        payload["tenant_schema_ok"] = tenant_schema_ok
        payload["ok"] = payload["ok"] and tenant_schema_ok
        if catalog_pilot:
            payload["tenant_databases_ready"] = bool(payload["tenant_ping"] and tenant_schema_ok)
    elif catalog_pilot:
        payload["ok"] = payload["ok"] and payload.get("tenant_databases_ready", False)
    if profile_state != "legacy" and not payload["ok"] and response is not None:
        response.status_code = 503
    return payload


@router.get("/health/master")
def master_health(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "master": _ping_db(master_db), "metrics": snapshot_metrics()}


@router.get("/health/tenant")
def tenant_health(db: Session = Depends(get_tenant_db), user=Depends(current_tenant_user)):
    schema_report = tenant_migration_report(db, user.company_id)
    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc),
        "company_id": user.company_id,
        "tenant": _ping_db(db),
        "schema_report": schema_report,
        "metrics": snapshot_metrics(),
    }


@router.get("/admin/tenants/{company_id}/health")
def admin_tenant_health(company_id: int, master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return company_diagnostics(master_db, company_id)


@router.get("/admin/tenants")
def admin_tenants(master_db: Session = Depends(get_master_db), _: object = Depends(require_master_admin)):
    return {"items": company_diagnostics_overview(master_db)}


@router.get("/health/metrics")
def health_metrics():
    return {"ok": True, "timestamp": datetime.now(timezone.utc), "metrics": snapshot_metrics()}
