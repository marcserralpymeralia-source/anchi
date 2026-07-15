from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.database import Base
from app.db import models as operational_models  # noqa: F401
from app.master.models import MasterCompany, MasterTenantDatabase
from app.master.service import slugify
from app.tenancy.migrations import ensure_tenant_migration_record, ensure_tenant_schema


ROOT = Path(__file__).resolve().parents[3]
TENANT_DB_DIR = ROOT / "backend" / "tenants"


def tenant_database_path(company: MasterCompany) -> Path:
    TENANT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return TENANT_DB_DIR / f"{company.id:04d}-{slugify(company.slug or company.name)}.db"


def tenant_database_url(company: MasterCompany) -> str:
    return f"sqlite:///{tenant_database_path(company).as_posix()}"


def _session_factory(database_url: str) -> sessionmaker[Session]:
    engine = create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _copy_company_rows(source_db: Session, target_db: Session, company_id: int) -> int:
    inserted = 0
    target_db.execute(text("PRAGMA foreign_keys=OFF"))
    try:
        for table in Base.metadata.sorted_tables:
            if table.name == "companies":
                rows = source_db.execute(select(table).where(table.c.id == company_id)).mappings().all()
            elif "company_id" in table.c:
                rows = source_db.execute(select(table).where(table.c.company_id == company_id)).mappings().all()
            else:
                continue
            if not rows:
                continue
            target_db.execute(table.insert(), [dict(row) for row in rows])
            inserted += len(rows)
        target_db.commit()
    finally:
        target_db.execute(text("PRAGMA foreign_keys=ON"))
    return inserted


def provision_company_database(master_db: Session, legacy_db: Session, company: MasterCompany) -> tuple[MasterTenantDatabase, bool]:
    database_url = tenant_database_url(company)
    tenant_db = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
    was_provisioned = False
    if not tenant_db:
        tenant_db = MasterTenantDatabase(
            company_id=company.id,
            database_key=slugify(company.slug or company.name),
            database_url=database_url,
            database_type="sqlite",
            is_active=True,
            health_status="pending",
        )
        master_db.add(tenant_db)
    else:
        tenant_db.database_key = slugify(company.slug or company.name)
        tenant_db.database_url = database_url
        tenant_db.database_type = "sqlite"
        tenant_db.is_active = True

    target_path = tenant_database_path(company)
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session_factory = _session_factory(database_url)
    target_db = session_factory()
    try:
        companies_table = Base.metadata.tables["companies"]
        count = target_db.scalar(select(func.count()).select_from(companies_table))
        if not count:
            _copy_company_rows(legacy_db, target_db, company.id)
            tenant_db.provisioned_at = tenant_db.provisioned_at or company.updated_at
            tenant_db.health_status = "ok"
            tenant_db.notes = f"Provisioned at {target_path.as_posix()}"
            was_provisioned = True
        else:
            tenant_db.health_status = "ok"
        ensure_tenant_schema(database_url, company_id=company.id)
        ensure_tenant_migration_record(
            target_db,
            company.id,
            notes="Provisioned tenant schema" if was_provisioned else "Tenant schema verified during provisioning",
        )
    finally:
        target_db.close()
    master_db.commit()
    return tenant_db, was_provisioned
