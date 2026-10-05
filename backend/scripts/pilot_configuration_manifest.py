"""Export the read-only, sanitized pilot manifest for one tenant company."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.pilot_configuration import (  # noqa: E402
    PilotConfigurationManifest,
    build_tenant_configuration_manifest,
    pilot_profile_state,
)
from app.db.models import IntegrationConnection, OrderRoute  # noqa: E402
from app.master.database import MasterSessionLocal  # noqa: E402
from app.master.migrations import master_migration_report  # noqa: E402
from app.master.models import MasterCompany, MasterTenantDatabase  # noqa: E402
from app.tenancy.database import tenant_db_session  # noqa: E402
from app.tenancy.migrations import tenant_migration_report  # noqa: E402


class ManifestGenerationError(RuntimeError):
    """A safe-to-display operator error; never contains database details."""


def _resolve_tenant(master_db, company_id: int) -> MasterTenantDatabase:
    tenant = master_db.scalar(
        select(MasterTenantDatabase)
        .join(MasterCompany)
        .options(selectinload(MasterTenantDatabase.company))
        .where(
            MasterTenantDatabase.company_id == company_id,
            MasterTenantDatabase.is_active.is_(True),
            MasterTenantDatabase.database_url.is_not(None),
            MasterCompany.active.is_(True),
            MasterCompany.status == "active",
        )
    )
    if tenant is None or not tenant.database_url:
        raise ManifestGenerationError("No existe un tenant activo con base configurada para ese company_id.")
    return tenant


def generate_manifest(company_id: int, *, master_session_factory=None) -> dict:
    """Read master and tenant state, then call the same builder as readiness."""
    if company_id <= 0:
        raise ManifestGenerationError("company_id debe ser un entero positivo.")
    if pilot_profile_state() != "catalog":
        raise ManifestGenerationError("La configuración no es un perfil Catalog-only completo y válido.")
    if os.getenv("TENANT_DB_MODE", "").strip().lower() != "external":
        raise ManifestGenerationError("TENANT_DB_MODE debe ser external.")

    runtime = PilotConfigurationManifest.from_runtime(tenant_id=company_id)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(runtime.get("commit_sha", ""))):
        raise ManifestGenerationError("No hay un commit SHA completo configurado para este runtime.")
    if set(runtime["enabled_modules"]) != {"whatsapp_catalog", "generic_rest"} or len(runtime["enabled_modules"]) != 2:
        raise ManifestGenerationError("PILOT_ENABLED_MODULES no coincide con Catalog-only.")
    if set(runtime["disabled_modules"]) != {"email_autodiscovery", "audio", "sage_real", "odoo_real"} or len(runtime["disabled_modules"]) != 4:
        raise ManifestGenerationError("PILOT_DISABLED_MODULES no coincide con Catalog-only.")
    gates = runtime["runtime_gates"]
    if runtime["storage_backend"] != "vercel_blob" or not gates.get("blob_token_configured") or not gates.get("blob_store_host_configured"):
        raise ManifestGenerationError("Vercel Blob debe estar configurado explícitamente.")
    if not gates.get("cron_secret_configured") or not gates.get("cron_endpoint_configured") or not gates.get("cron_allowed_host_configured"):
        raise ManifestGenerationError("El endpoint Cron protegido debe estar configurado.")
    if gates.get("secret_provider_kind") != "environment":
        raise ManifestGenerationError("PILOT_SECRET_PROVIDER debe ser environment.")
    if gates.get("RUN_INTERNAL_JOB_WORKER") or gates.get("RUN_INTERNAL_EMAIL_WORKER"):
        raise ManifestGenerationError("Los workers internos deben estar apagados en el runtime Web.")

    db = (master_session_factory or MasterSessionLocal)()
    try:
        master_schema = master_migration_report(db, persist=False)
        if master_schema.get("status") != "current":
            raise ManifestGenerationError("El esquema master no está current.")
        tenant = _resolve_tenant(db, company_id)
        tenant_db = tenant_db_session(tenant.database_url)()
        try:
            tenant_schema = tenant_migration_report(tenant_db, company_id, persist=False)
            if tenant_schema.get("status") != "current":
                raise ManifestGenerationError("El esquema tenant no está current.")
            connections = tenant_db.scalars(
                select(IntegrationConnection)
                .where(IntegrationConnection.company_id == company_id)
                .order_by(IntegrationConnection.id)
            ).all()
            routes = tenant_db.scalars(
                select(OrderRoute)
                .where(OrderRoute.company_id == company_id)
                .order_by(OrderRoute.id)
            ).all()
            return build_tenant_configuration_manifest(
                company_id=company_id,
                master_schema_report=master_schema,
                tenant_schema_report=tenant_schema,
                connections=connections,
                routes=routes,
            )
        finally:
            tenant_db.close()
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exporta el PilotConfigurationManifest sanitizado de un tenant, sin modificar datos."
    )
    parser.add_argument("--company-id", required=True, type=int, help="ID explícito de MasterCompany del tenant piloto")
    args = parser.parse_args(argv)
    try:
        manifest = generate_manifest(args.company_id)
    except ManifestGenerationError as exc:
        print(f"No se pudo generar el manifest: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - do not leak URLs, credentials, or PII in driver errors
        print(f"No se pudo generar el manifest ({exc.__class__.__name__}).", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
