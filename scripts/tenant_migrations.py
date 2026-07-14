from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select

from app.master.database import MasterSessionLocal
from app.master.models import MasterCompany, MasterTenantDatabase
from app.tenancy.database import ensure_tenant_schema, tenant_db_session
from app.tenancy.migrations import ensure_tenant_migration_record, tenant_migration_report


def _resolve_company(master_db, selector: str) -> MasterCompany | None:
    if selector.isdigit():
        return master_db.get(MasterCompany, int(selector))
    return master_db.scalar(select(MasterCompany).where(MasterCompany.slug == selector))


def check_company(selector: str | None = None) -> list[dict]:
    master_db = MasterSessionLocal()
    try:
        if selector:
            company = _resolve_company(master_db, selector)
            if not company:
                return []
            tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
            if not tenant or not tenant.database_url:
                return [{"company_id": company.id, "company_slug": company.slug, "status": "missing_tenant"}]
            session_factory = tenant_db_session(tenant.database_url)
            db = session_factory()
            try:
                return [{"company_id": company.id, "company_slug": company.slug, **tenant_migration_report(db, company.id)}]
            finally:
                db.close()
        rows = []
        companies = master_db.scalars(select(MasterCompany).order_by(MasterCompany.name)).all()
        for company in companies:
            tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
            if not tenant or not tenant.database_url:
                rows.append({"company_id": company.id, "company_slug": company.slug, "status": "missing_tenant"})
                continue
            session_factory = tenant_db_session(tenant.database_url)
            db = session_factory()
            try:
                rows.append({"company_id": company.id, "company_slug": company.slug, **tenant_migration_report(db, company.id)})
            finally:
                db.close()
        return rows
    finally:
        master_db.close()


def migrate_company(selector: str) -> dict:
    master_db = MasterSessionLocal()
    try:
        company = _resolve_company(master_db, selector)
        if not company:
            raise SystemExit(f"Company not found: {selector}")
        tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
        if not tenant or not tenant.database_url:
            raise SystemExit(f"Tenant DB not configured for company {company.slug}")
        ensure_tenant_schema(tenant.database_url)
        session_factory = tenant_db_session(tenant.database_url)
        db = session_factory()
        try:
            ensure_tenant_migration_record(db, company.id, notes="Tenant migration command")
            report = tenant_migration_report(db, company.id)
        finally:
            db.close()
        return {"company_id": company.id, "company_slug": company.slug, **report}
    finally:
        master_db.close()


def migrate_all() -> list[dict]:
    master_db = MasterSessionLocal()
    try:
        companies = master_db.scalars(select(MasterCompany).order_by(MasterCompany.name)).all()
        results = []
        for company in companies:
            tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
            if not tenant or not tenant.database_url:
                results.append({"company_id": company.id, "company_slug": company.slug, "status": "missing_tenant"})
                continue
            ensure_tenant_schema(tenant.database_url)
            session_factory = tenant_db_session(tenant.database_url)
            db = session_factory()
            try:
                ensure_tenant_migration_record(db, company.id, notes="Bulk migration command")
                results.append({"company_id": company.id, "company_slug": company.slug, **tenant_migration_report(db, company.id)})
            finally:
                db.close()
        return results
    finally:
        master_db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Tenant migration helpers for Anchi")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Inspect tenant migration state")
    check.add_argument("--company", default="")

    migrate = sub.add_parser("migrate", help="Apply/refresh a single tenant migration state")
    migrate.add_argument("--company", required=True)

    sub.add_parser("migrate-all", help="Refresh all tenant migration states")

    args = parser.parse_args()
    if args.command == "check":
        payload = check_company(args.company or None)
        print(json.dumps(payload, default=str, ensure_ascii=False, indent=2))
        return 0
    if args.command == "migrate":
        payload = migrate_company(args.company)
        print(json.dumps(payload, default=str, ensure_ascii=False, indent=2))
        return 0
    if args.command == "migrate-all":
        payload = migrate_all()
        print(json.dumps(payload, default=str, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
