#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import func, select, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"

os.environ.setdefault("APP_ENV", "development")

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.security import verify_password  # noqa: E402
from app.demo_seed import seed_demo_base  # noqa: E402
from app.master.database import MasterSessionLocal, engine as master_engine  # noqa: E402
from app.master.migrations import master_migration_report, upgrade_master_schema  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.master.provisioning import provision_demo_external_tenant  # noqa: E402
from app.tenancy.database import get_tenant_engine, tenant_db_session  # noqa: E402
from app.tenancy.migrations import tenant_migration_report, upgrade_tenant_schema  # noqa: E402
from app.db.models import Customer, EmailSettings, Order, Product  # noqa: E402

from provision_company import provision_company as provision_local_company  # noqa: E402


def _json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _master_session():
    return MasterSessionLocal()


def _resolve_tenant(master_db, *, company_id: int | None = None, company_slug: str | None = None) -> MasterTenantDatabase:
    query = master_db.query(MasterTenantDatabase).join(MasterCompany)
    if company_id is not None:
        query = query.filter(MasterTenantDatabase.company_id == company_id)
    elif company_slug:
        query = query.filter(MasterCompany.slug == company_slug)
    else:
        raise ValueError("Debes indicar --company o --company-slug")
    tenant = query.order_by(MasterCompany.name).first()
    if not tenant or not tenant.database_url:
        raise ValueError("No se encontró una base tenant activa para la compañía solicitada")
    return tenant


def _ensure_external_demo_database_url() -> str:
    settings = get_settings()
    database_url = settings.tenant_database_url or settings.database_url
    if settings.tenant_db_mode != "external":
        raise ValueError("TENANT_DB_MODE debe ser external para provision-demo")
    if not database_url:
        raise ValueError("TENANT_DATABASE_URL o DATABASE_URL es obligatorio para provision-demo")
    if database_url.startswith("sqlite"):
        raise ValueError("La demo externa no puede usar SQLite")
    if settings.master_database_url.startswith("sqlite"):
        raise ValueError("MASTER_DATABASE_URL debe apuntar a una base externa para la demo")
    return database_url


def cmd_migrate_master(args) -> int:
    result = upgrade_master_schema(master_engine, application_version=args.application_version, dry_run=args.dry_run, baseline=args.baseline)
    _json({"scope": "master", **result})
    return 0


def cmd_provision_demo(args) -> int:
    tenant_database_url = _ensure_external_demo_database_url()
    settings = get_settings()
    db = _master_session()
    try:
        result = provision_demo_external_tenant(
            db,
            tenant_database_url=tenant_database_url,
            company_name=settings.default_company_name,
            company_slug="anchi-demo",
            admin_email=settings.default_admin_email,
            admin_password=settings.default_admin_password,
        )
        _json({"scope": "provision_demo", **result})
        return 0
    finally:
        db.close()


def cmd_migrate_tenant(args) -> int:
    db = _master_session()
    try:
        tenant = _resolve_tenant(db, company_id=args.company_id, company_slug=args.company_slug)
        result = upgrade_tenant_schema(
            get_tenant_engine(tenant.database_url),
            company_id=tenant.company_id,
            application_version=args.application_version,
            dry_run=args.dry_run,
            baseline=args.baseline,
        )
        _json(
            {
                "scope": "tenant",
                "company_id": tenant.company_id,
                "company_slug": tenant.company.slug if tenant.company else None,
                "database_key": tenant.database_key,
                **result,
            }
        )
        return 0
    finally:
        db.close()


def cmd_migrate_all_tenants(args) -> int:
    db = _master_session()
    try:
        tenants = (
            db.query(MasterTenantDatabase)
            .join(MasterCompany)
            .filter(MasterTenantDatabase.is_active.is_(True), MasterTenantDatabase.database_url.is_not(None))
            .order_by(MasterTenantDatabase.company_id)
            .all()
        )
    finally:
        db.close()
    payload = []
    for tenant in tenants:
        result = upgrade_tenant_schema(
            get_tenant_engine(tenant.database_url),
            company_id=tenant.company_id,
            application_version=args.application_version,
            dry_run=args.dry_run,
            baseline=args.baseline,
        )
        payload.append(
            {
                "company_id": tenant.company_id,
                "company_slug": tenant.company.slug if tenant.company else None,
                "database_key": tenant.database_key,
                **result,
            }
        )
    _json({"scope": "all_tenants", "items": payload})
    return 0


def cmd_seed_base(args) -> int:
    settings = get_settings()
    if settings.environment == "demo" or os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV")):
        raise ValueError("seed-base solo se permite en local/dev; usa provision-demo para demo externa")
    result = provision_local_company(
        args.name,
        args.slug or "anchi-demo",
        args.admin_email,
        args.admin_password,
        force=args.force,
    )
    _json({"scope": "seed_base", **result})
    return 0


def cmd_seed_demo(args) -> int:
    db = _master_session()
    try:
        tenant = _resolve_tenant(db, company_id=args.company_id, company_slug=args.company_slug)
        session_factory = tenant_db_session(tenant.database_url)
        tenant_db = session_factory()
        try:
            result = seed_demo_base(tenant_db)
        finally:
            tenant_db.close()
        _json(
            {
                "scope": "seed_demo",
                "company_id": tenant.company_id,
                "company_slug": tenant.company.slug if tenant.company else None,
                "database_key": tenant.database_key,
                **result,
            }
        )
        return 0
    finally:
        db.close()


def cmd_health(args) -> int:
    settings = get_settings()
    db = _master_session()
    try:
        company = db.scalar(select(MasterCompany).where(MasterCompany.slug == (args.company_slug or "anchi-demo")))
        if not company:
            raise ValueError("No existe la compañía demo en master")
        tenant = db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
        user = db.scalar(select(MasterUser).where(MasterUser.email == settings.default_admin_email))
        membership = db.scalar(
            select(CompanyMembership).where(
                CompanyMembership.company_id == company.id,
                CompanyMembership.user_id == (user.id if user else None),
            )
        )
        login_ok = bool(user and membership and verify_password(settings.default_admin_password, user.password_hash))
        master_schema = master_migration_report(db, persist=False)
        master_ok = db.execute(text("SELECT 1")).scalar() == 1
        payload = {
            "status": "ok",
            "environment": settings.environment,
            "master_db": "OK" if master_ok else "ERROR",
            "company": f"{company.name} OK",
            "user": f"{settings.default_admin_email} {'OK' if user else 'ERROR'}",
            "membership": "OK" if membership else "MISSING",
            "login": "OK" if login_ok else "ERROR",
            "tenant_db": "MISSING",
            "tenant_migrations": "MISSING",
            "customers": 0,
            "products": 0,
            "orders": 0,
            "imap_auto_sync": "disabled" if not settings.environment == "production" else "unknown",
            "vercel_workers": "disabled" if (os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))) else "local",
            "master_schema_status": master_schema.get("status"),
        }
        if tenant and tenant.database_url:
            session_factory = tenant_db_session(tenant.database_url)
            tenant_db = session_factory()
            try:
                tenant_db.execute(text("SELECT 1"))
                tenant_schema = tenant_migration_report(tenant_db, company.id, persist=False)
                email_settings = tenant_db.query(EmailSettings).filter(EmailSettings.company_id == company.id).first()
                payload.update(
                    {
                        "tenant_db": "OK",
                        "tenant_migrations": tenant_schema.get("status", "ok"),
                        "customers": tenant_db.scalar(select(func.count()).select_from(Customer).where(Customer.company_id == company.id)) or 0,
                        "products": tenant_db.scalar(select(func.count()).select_from(Product).where(Product.company_id == company.id)) or 0,
                        "orders": tenant_db.scalar(select(func.count()).select_from(Order).where(Order.company_id == company.id)) or 0,
                        "imap_auto_sync": "disabled" if not (email_settings and email_settings.auto_sync_enabled) else "enabled",
                    }
                )
            finally:
                tenant_db.close()
        _json(payload)
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Comandos de demo, migracion y bootstrap de Anchi.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target):
        target.add_argument("--application-version", default=None, help="Version de la aplicacion para registrar en el ledger")
        target.add_argument("--dry-run", action="store_true", help="Calcula el plan sin escribir cambios")
        target.add_argument("--baseline", action="store_true", help="Registra la version actual sin ejecutar cambios")

    add_common(sub.add_parser("migrate-master", help="Aplica migraciones al master DB"))

    provision_demo = sub.add_parser("provision-demo", help="Registra la demo Anchi contra una base externa")

    migrate_tenant = sub.add_parser("migrate-tenant", help="Aplica migraciones a un tenant concreto")
    migrate_tenant.add_argument("--company", "--company-slug", dest="company_slug", default=None)
    migrate_tenant.add_argument("--company-id", type=int, default=None)
    add_common(migrate_tenant)

    add_common(sub.add_parser("migrate-all-tenants", help="Aplica migraciones a todos los tenants activos"))

    seed_base = sub.add_parser("seed-base", help="Crea la compañia demo limpia y su tenant local")
    seed_base.add_argument("--name", default="Anchi Demo", help="Nombre de la compañia demo")
    seed_base.add_argument("--slug", default="anchi-demo", help="Slug tecnico de la compañia demo")
    seed_base.add_argument("--admin-email", default="admin@anchi.local", help="Email del administrador demo")
    seed_base.add_argument("--admin-password", default="AnchiDemo2026!", help="Password del administrador demo")
    seed_base.add_argument("--force", action="store_true", help="Permite sobrescribir la base tenant si ya existe")

    seed_demo = sub.add_parser("seed-demo", help="Carga el dataset demo en el tenant indicado")
    seed_demo.add_argument("--company", "--company-slug", dest="company_slug", default="anchi-demo")
    seed_demo.add_argument("--company-id", type=int, default=None)

    health = sub.add_parser("health", help="Comprueba master, tenant y dataset demo")
    health.add_argument("--company", "--company-slug", dest="company_slug", default="anchi-demo")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "migrate-master":
        return cmd_migrate_master(args)
    if args.command == "provision-demo":
        return cmd_provision_demo(args)
    if args.command == "migrate-tenant":
        return cmd_migrate_tenant(args)
    if args.command == "migrate-all-tenants":
        return cmd_migrate_all_tenants(args)
    if args.command == "seed-base":
        return cmd_seed_base(args)
    if args.command == "seed-demo":
        return cmd_seed_demo(args)
    if args.command == "health":
        return cmd_health(args)
    parser.error("Comando no soportado")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
