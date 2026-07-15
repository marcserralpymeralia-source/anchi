from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.master.database import MasterSessionLocal, engine as master_engine  # noqa: E402
from app.master.models import MasterCompany, MasterTenantDatabase  # noqa: E402
from app.master.migrations import master_migration_report, upgrade_master_schema  # noqa: E402
from app.migrations.inspection import discover_sqlite_files, inspect_database_url, inventory_records, render_markdown_table, simulate_sqlite_reference  # noqa: E402
from app.tenancy.database import get_tenant_engine  # noqa: E402
from app.tenancy.database import tenant_db_session  # noqa: E402
from app.tenancy.migrations import tenant_migration_report, upgrade_tenant_schema  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SIMULATION_ROOT = REPOSITORY_ROOT / "backend" / "storage" / "migration-simulations"


def _json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _resolve_tenant(master_db, *, company_id: int | None, company_slug: str | None) -> MasterTenantDatabase:
    query = select(MasterTenantDatabase).join(MasterCompany).options(selectinload(MasterTenantDatabase.company))
    if company_id is not None:
        query = query.where(MasterTenantDatabase.company_id == company_id)
    elif company_slug:
        query = query.where(MasterCompany.slug == company_slug)
    else:
        raise ValueError("Debes indicar company_id o company_slug")
    tenant = master_db.scalar(query.order_by(MasterCompany.name))
    if not tenant or not tenant.database_url:
        raise ValueError("No se encontró una base tenant activa para la compañía solicitada")
    return tenant


def _master_session():
    return MasterSessionLocal()


def _resolve_master_tenant_refs():
    db = _master_session()
    try:
        tenants = db.scalars(
            select(MasterTenantDatabase)
            .options(selectinload(MasterTenantDatabase.company))
            .where(MasterTenantDatabase.is_active.is_(True), MasterTenantDatabase.database_url.is_not(None))
        ).all()
        payload = []
        for tenant in tenants:
            payload.append(
                {
                    "company_id": tenant.company_id,
                    "company_slug": tenant.company.slug if tenant.company else None,
                    "company_name": tenant.company.name if tenant.company else None,
                    "database_key": tenant.database_key,
                    "database_url": tenant.database_url,
                    "kind_hint": "tenant",
                }
            )
        return payload
    finally:
        db.close()


def cmd_upgrade_master(args) -> int:
    result = upgrade_master_schema(master_engine, application_version=args.application_version, dry_run=args.dry_run, baseline=args.baseline)
    _json({"scope": "master", **result})
    return 0


def cmd_upgrade_tenant(args) -> int:
    db = _master_session()
    try:
        tenant = _resolve_tenant(db, company_id=args.company_id, company_slug=args.company_slug)
        company_slug = tenant.company.slug
        database_key = tenant.database_key
    finally:
        db.close()
    result = upgrade_tenant_schema(
        get_tenant_engine(tenant.database_url),
        company_id=tenant.company_id,
        application_version=args.application_version,
        dry_run=args.dry_run,
        baseline=args.baseline,
    )
    _json({"scope": "tenant", "company_id": tenant.company_id, "company_slug": company_slug, "database_key": database_key, **result})
    return 0


def cmd_upgrade_all_tenants(args) -> int:
    db = _master_session()
    try:
        tenants = db.scalars(
            select(MasterTenantDatabase)
            .options(selectinload(MasterTenantDatabase.company))
            .where(MasterTenantDatabase.is_active.is_(True), MasterTenantDatabase.database_url.is_not(None))
        ).all()
    finally:
        db.close()
    payload = []
    for tenant in tenants:
        company_slug = tenant.company.slug if tenant.company else ""
        result = upgrade_tenant_schema(
            get_tenant_engine(tenant.database_url),
            company_id=tenant.company_id,
            application_version=args.application_version,
            dry_run=args.dry_run,
            baseline=args.baseline,
        )
        payload.append({"company_id": tenant.company_id, "company_slug": company_slug, "database_key": tenant.database_key, **result})
    _json({"scope": "all_tenants", "items": payload})
    return 0


def cmd_report_master(args) -> int:
    db = _master_session()
    try:
        result = master_migration_report(db, persist=False)
    finally:
        db.close()
    _json({"scope": "master", **result})
    return 0


def cmd_report_tenant(args) -> int:
    db = _master_session()
    try:
        tenant = _resolve_tenant(db, company_id=args.company_id, company_slug=args.company_slug)
        company_slug = tenant.company.slug
        session_factory = tenant_db_session(tenant.database_url)
        tenant_db = session_factory()
        try:
            result = tenant_migration_report(tenant_db, tenant.company_id, persist=False)
        finally:
            tenant_db.close()
        _json({"scope": "tenant", "company_id": tenant.company_id, "company_slug": company_slug, "database_key": tenant.database_key, **result})
        return 0
    finally:
        db.close()


def cmd_status_all_tenants(args) -> int:
    db = _master_session()
    try:
        inventory = inventory_records(db, REPOSITORY_ROOT)
    finally:
        db.close()
    if args.summary:
        rows = []
        for item in inventory:
            if item["type"] not in {"master", "tenant"}:
                continue
            rows.append(
                [
                    item["logical_name"],
                    item["engine"],
                    item["state"],
                    item["readiness"],
                    item["source"],
                ]
            )
        print(render_markdown_table(["Base", "Motor", "Estado", "Readiness", "Fuente"], rows))
        return 0
    _json({"scope": "all_tenants", "items": inventory})
    return 0


def cmd_inventory(args) -> int:
    db = _master_session()
    try:
        inventory = inventory_records(db, REPOSITORY_ROOT)
    finally:
        db.close()
    if args.summary:
        rows = []
        for item in inventory:
            action = "baseline/upgrade" if item["baseline_safe"] else "manual review"
            rows.append(
                [
                    item["logical_name"],
                    item["type"],
                    item["engine"],
                    item["state"],
                    action,
                ]
            )
        print(render_markdown_table(["Base lógica", "Tipo", "Motor", "Estado detectado", "Acción propuesta"], rows))
        return 0
    _json({"scope": "inventory", "items": inventory})
    return 0


def cmd_simulate_master(args) -> int:
    from app.core.config import get_settings  # noqa: E402

    settings = get_settings()
    source = Path(settings.master_database_url.replace("sqlite:///", "", 1)).expanduser()
    result = simulate_sqlite_reference(source, SIMULATION_ROOT, label="master", kind_hint="master", application_version=args.application_version)
    _json({"scope": "simulate_master", **result})
    return 0


def cmd_simulate_tenant(args) -> int:
    db = _master_session()
    try:
        tenant = _resolve_tenant(db, company_id=args.company_id, company_slug=args.company_slug)
        company_slug = tenant.company.slug
        source = Path(tenant.database_url.replace("sqlite:///", "", 1)).expanduser()
        result = simulate_sqlite_reference(
            source,
            SIMULATION_ROOT,
            label=tenant.database_key or company_slug or f"tenant-{tenant.company_id}",
            kind_hint="tenant",
            company_id=tenant.company_id,
            application_version=args.application_version,
        )
    finally:
        db.close()
    _json({"scope": "simulate_tenant", "company_id": tenant.company_id, "company_slug": company_slug, "database_key": tenant.database_key, **result})
    return 0


def cmd_simulate_all_tenants(args) -> int:
    db = _master_session()
    try:
        tenants = db.scalars(
            select(MasterTenantDatabase)
            .options(selectinload(MasterTenantDatabase.company))
            .where(MasterTenantDatabase.is_active.is_(True), MasterTenantDatabase.database_url.is_not(None))
            .order_by(MasterTenantDatabase.company_id)
        ).all()
        payload = []
        settings = []
        from app.core.config import get_settings  # noqa: E402

        settings = get_settings()
        master_source = Path(settings.master_database_url.replace("sqlite:///", "", 1)).expanduser()
        payload.append(simulate_sqlite_reference(master_source, SIMULATION_ROOT, label="master", kind_hint="master", application_version=args.application_version))
        for tenant in tenants:
            source = Path(tenant.database_url.replace("sqlite:///", "", 1)).expanduser()
            payload.append(
                simulate_sqlite_reference(
                    source,
                    SIMULATION_ROOT,
                    label=tenant.database_key or (tenant.company.slug if tenant.company else f"tenant-{tenant.company_id}"),
                    kind_hint="tenant",
                    company_id=tenant.company_id,
                    application_version=args.application_version,
                )
            )
    finally:
        db.close()
    _json({"scope": "simulate_all_tenants", "items": payload})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gestion formal de migraciones de esquema")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target):
        target.add_argument("--application-version", default=None, help="Version de la aplicacion para registrar en el ledger")
        target.add_argument("--dry-run", action="store_true", help="Calcula el plan sin escribir cambios")
        target.add_argument("--baseline", action="store_true", help="Registra la version actual sin ejecutar cambios")

    add_common(sub.add_parser("upgrade-master", help="Aplica migraciones al master DB"))
    add_common(sub.add_parser("report-master", help="Muestra el estado del master DB"))

    status_all = sub.add_parser("status-all-tenants", help="Muestra el estado de master y tenants")
    status_all.add_argument("--summary", action="store_true", help="Imprime una tabla resumida")

    tenant_parser = sub.add_parser("upgrade-tenant", help="Aplica migraciones a un tenant concreto")
    tenant_parser.add_argument("--company-id", type=int, default=None)
    tenant_parser.add_argument("--company-slug", default=None)
    add_common(tenant_parser)

    tenant_report = sub.add_parser("report-tenant", help="Muestra el estado de un tenant concreto")
    tenant_report.add_argument("--company-id", type=int, default=None)
    tenant_report.add_argument("--company-slug", default=None)
    add_common(tenant_report)

    all_tenants = sub.add_parser("upgrade-all-tenants", help="Aplica migraciones a todos los tenants activos")
    add_common(all_tenants)

    inventory = sub.add_parser("inventory", help="Inventario completo de bases detectadas")
    inventory.add_argument("--summary", action="store_true", help="Imprime una tabla resumida")

    simulate_master = sub.add_parser("simulate-master", help="Simula baseline y upgrade del master sobre una copia")
    add_common(simulate_master)

    simulate_tenant = sub.add_parser("simulate-tenant", help="Simula baseline y upgrade de un tenant sobre una copia")
    simulate_tenant.add_argument("--company-id", type=int, default=None)
    simulate_tenant.add_argument("--company-slug", default=None)
    add_common(simulate_tenant)

    simulate_all = sub.add_parser("simulate-all-tenants", help="Simula baseline y upgrade de master y tenants sobre copias")
    add_common(simulate_all)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "upgrade-master":
        return cmd_upgrade_master(args)
    if args.command == "report-master":
        return cmd_report_master(args)
    if args.command == "status-all-tenants":
        return cmd_status_all_tenants(args)
    if args.command == "upgrade-tenant":
        return cmd_upgrade_tenant(args)
    if args.command == "report-tenant":
        return cmd_report_tenant(args)
    if args.command == "upgrade-all-tenants":
        return cmd_upgrade_all_tenants(args)
    if args.command == "inventory":
        return cmd_inventory(args)
    if args.command == "simulate-master":
        return cmd_simulate_master(args)
    if args.command == "simulate-tenant":
        return cmd_simulate_tenant(args)
    if args.command == "simulate-all-tenants":
        return cmd_simulate_all_tenants(args)
    parser.error("Comando no soportado")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
