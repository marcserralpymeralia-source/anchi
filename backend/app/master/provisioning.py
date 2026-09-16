from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.core.permissions import DEFAULT_ROLE_PERMISSIONS
from app.core.security import hash_password
from app.db.database import Base
from app.db.models import Company as TenantCompany
from app.db import models as operational_models  # noqa: F401
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser, utcnow
from app.master.service import slugify
from app.tenancy.migrations import ensure_tenant_migration_record, ensure_tenant_schema


ROOT = Path(__file__).resolve().parents[3]
TENANT_DB_DIR = ROOT / "backend" / "tenants"


def tenant_database_path(company: MasterCompany) -> Path:
    TENANT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return TENANT_DB_DIR / f"{company.id:04d}-{slugify(company.slug or company.name)}.db"


def tenant_database_url(company: MasterCompany) -> str:
    return f"sqlite:///{tenant_database_path(company).as_posix()}"


def _database_type(database_url: str) -> str:
    if "://" not in database_url:
        return "unknown"
    return database_url.split("://", 1)[0]


def _ensure_master_company(master_db: Session, name: str, slug: str) -> MasterCompany:
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == slug))
    if company:
        company.name = name
        company.legal_name = company.legal_name or name
        company.active = True
        return company
    company = MasterCompany(name=name, slug=slug, legal_name=name, active=True)
    master_db.add(company)
    master_db.flush()
    return company


def _ensure_master_user(master_db: Session, email: str, full_name: str, password: str) -> MasterUser:
    user = master_db.scalar(select(MasterUser).where(MasterUser.email == email))
    if not user:
        user = MasterUser(email=email, full_name=full_name, password_hash=hash_password(password), is_active=True)
        master_db.add(user)
        master_db.flush()
        return user
    user.full_name = full_name
    user.is_active = True
    master_db.flush()
    return user


def _ensure_membership(master_db: Session, user: MasterUser, company: MasterCompany) -> CompanyMembership:
    membership = master_db.scalar(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.company_id == company.id,
        )
    )
    if not membership:
        membership = CompanyMembership(user_id=user.id, company_id=company.id, role_key="Administrador", is_active=True, is_owner=True)
        master_db.add(membership)
        master_db.flush()
    else:
        membership.role_key = "Administrador"
        membership.is_active = True
        membership.is_owner = True
    return membership


def _ensure_tenant_database_row(master_db: Session, company: MasterCompany, database_url: str) -> MasterTenantDatabase:
    tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
    if not tenant:
        tenant = MasterTenantDatabase(
            company_id=company.id,
            database_key=slugify(company.slug or company.name),
            database_url=database_url,
            database_type=_database_type(database_url),
            is_active=True,
            health_status="pending",
        )
        master_db.add(tenant)
        master_db.flush()
        return tenant
    tenant.database_key = slugify(company.slug or company.name)
    tenant.database_url = database_url
    tenant.database_type = _database_type(database_url)
    tenant.is_active = True
    tenant.health_status = "pending"
    return tenant


def _session_factory(database_url: str) -> tuple[object, sessionmaker[Session]]:
    engine = create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {})
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def synchronize_tenant_actors(master_db: Session, tenant: MasterTenantDatabase) -> int:
    """Project active master memberships into the tenant actor table.

    Existing installations may predate the identity bridge, so this operation
    links legacy actors by email when possible and never removes local rows.
    It runs during startup/provisioning, not on every request.
    """

    database_url = tenant.database_url
    if not isinstance(database_url, str) or not database_url.strip():
        return 0
    memberships = master_db.scalars(
        select(CompanyMembership)
        .options(selectinload(CompanyMembership.user), selectinload(CompanyMembership.company))
        .where(CompanyMembership.company_id == tenant.company_id)
    ).all()
    if not memberships:
        return 0

    engine, session_factory = _session_factory(database_url)
    Base.metadata.create_all(bind=engine)
    tenant_db = session_factory()
    updated = 0
    try:
        roles = {
            role.name: role
            for role in tenant_db.scalars(select(operational_models.Role).where(operational_models.Role.company_id == tenant.company_id)).all()
        }
        for membership in memberships:
            role_key = membership.role_key or "Operador"
            if role_key == "Superadmin":
                role_key = "Administrador"
            role = roles.get(role_key)
            if role is None:
                role = operational_models.Role(
                    company_id=tenant.company_id,
                    name=role_key,
                    permissions=DEFAULT_ROLE_PERMISSIONS.get(role_key, ""),
                )
                tenant_db.add(role)
                tenant_db.flush()
                roles[role.name] = role
            actor = tenant_db.scalar(
                select(operational_models.User).where(
                    operational_models.User.company_id == tenant.company_id,
                    operational_models.User.master_user_id == membership.user_id,
                )
            )
            if actor is None:
                actor = tenant_db.scalar(
                    select(operational_models.User).where(
                        operational_models.User.company_id == tenant.company_id,
                        operational_models.User.email == membership.user.email,
                    )
                )
            if actor is None:
                actor = operational_models.User(
                    company_id=tenant.company_id,
                    role_id=role.id,
                    email=membership.user.email,
                    name=membership.user.full_name,
                    password_hash=membership.user.password_hash,
                    is_active=membership.user.is_active and membership.is_active,
                    master_user_id=membership.user_id,
                    actor_type="human",
                )
                tenant_db.add(actor)
            else:
                actor.master_user_id = membership.user_id
                actor.actor_type = "human"
                actor.role_id = role.id
                actor.email = membership.user.email
                actor.name = membership.user.full_name
                actor.password_hash = membership.user.password_hash
                actor.is_active = membership.user.is_active and membership.is_active
            updated += 1
        tenant_db.commit()
        return updated
    finally:
        tenant_db.close()
        engine.dispose()


def find_tenant_actor_id(database_url: str, *, company_id: int, master_user_id: int) -> int | None:
    """Resolve the local actor projection without authenticating against it."""

    if not isinstance(database_url, str) or not database_url.strip():
        return None
    engine, session_factory = _session_factory(database_url)
    try:
        tenant_db = session_factory()
        try:
            actor = tenant_db.scalar(
                select(operational_models.User).where(
                    operational_models.User.company_id == company_id,
                    operational_models.User.master_user_id == master_user_id,
                    operational_models.User.actor_type == "human",
                )
            )
            return actor.id if actor is not None else None
        finally:
            tenant_db.close()
    except Exception:  # noqa: BLE001
        return None
    finally:
        engine.dispose()


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
    engine, session_factory = _session_factory(database_url)
    Base.metadata.create_all(bind=engine)
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
        engine.dispose()
    master_db.commit()
    return tenant_db, was_provisioned


def provision_local_tenant(
    master_db: Session,
    *,
    company: MasterCompany,
    admin_user: MasterUser,
    admin_role_key: str = "Administrador",
) -> MasterTenantDatabase:
    """Create an isolated local tenant and its first actor.

    This is intentionally limited to local SQLite development/demo mode. A
    production deployment must provide an explicit tenant database provisioner
    for the selected external database provider rather than silently reusing a
    shared connection.
    """

    database_url = tenant_database_url(company)
    tenant_db = _ensure_tenant_database_row(master_db, company, database_url)
    engine, session_factory = _session_factory(database_url)
    Base.metadata.create_all(bind=engine)
    tenant_session = session_factory()
    try:
        tenant_company = tenant_session.get(TenantCompany, company.id)
        if tenant_company is None:
            tenant_company = TenantCompany(
                id=company.id,
                name=company.name,
                legal_name=company.legal_name or company.name,
                active=True,
            )
            tenant_session.add(tenant_company)
            tenant_session.flush()
        role = tenant_session.scalar(
            select(operational_models.Role).where(
                operational_models.Role.company_id == company.id,
                operational_models.Role.name == admin_role_key,
            )
        )
        if role is None:
            role = operational_models.Role(
                company_id=company.id,
                name=admin_role_key,
                permissions=DEFAULT_ROLE_PERMISSIONS.get(admin_role_key, DEFAULT_ROLE_PERMISSIONS["Administrador"]),
            )
            tenant_session.add(role)
            tenant_session.flush()
        local_user = tenant_session.scalar(
            select(operational_models.User).where(
                operational_models.User.company_id == company.id,
                operational_models.User.email == admin_user.email,
            )
        )
        if local_user is None:
            local_user = operational_models.User(
                company_id=company.id,
                role_id=role.id,
                email=admin_user.email,
                name=admin_user.full_name,
                password_hash=admin_user.password_hash,
                is_active=admin_user.is_active,
                master_user_id=admin_user.id,
                actor_type="human",
            )
            tenant_session.add(local_user)
        else:
            local_user.role_id = role.id
            local_user.name = admin_user.full_name
            local_user.password_hash = admin_user.password_hash
            local_user.is_active = admin_user.is_active
            local_user.master_user_id = admin_user.id
        tenant_session.commit()
        ensure_tenant_schema(database_url, company_id=company.id)
        tenant_db.health_status = "ok"
        tenant_db.provisioned_at = tenant_db.provisioned_at or utcnow()
        tenant_db.notes = "Provisioned by Superadmin"
        company.provisioning_status = "ready"
        company.status = "active"
        master_db.commit()
        return tenant_db
    except Exception:
        tenant_session.rollback()
        tenant_db.health_status = "error"
        company.provisioning_status = "error"
        master_db.commit()
        raise
    finally:
        tenant_session.close()
        engine.dispose()


def provision_external_tenant(
    master_db: Session,
    *,
    tenant_database_url: str,
    company_name: str,
    company_slug: str,
    admin_email: str,
    admin_password: str,
) -> dict[str, str]:
    database_url = (tenant_database_url or "").strip()
    if not database_url:
        raise ValueError("TENANT_DATABASE_URL is required for external tenant provisioning")
    if database_url.startswith("sqlite"):
        raise ValueError("TENANT_DATABASE_URL cannot use sqlite in external tenant provisioning")

    company = _ensure_master_company(master_db, company_name, company_slug)
    user = _ensure_master_user(master_db, admin_email, f"Administrador {company_name}", admin_password)
    membership = _ensure_membership(master_db, user, company)
    tenant = _ensure_tenant_database_row(master_db, company, database_url)

    engine, tenant_session_factory = _session_factory(database_url)
    Base.metadata.create_all(bind=engine)

    tenant_session = tenant_session_factory()
    try:
        tenant_company = tenant_session.get(TenantCompany, company.id)
        if tenant_company is None:
            tenant_company = TenantCompany(
                id=company.id,
                name=company.name,
                legal_name=company.legal_name or company.name,
                active=True,
            )
            tenant_session.add(tenant_company)
        else:
            tenant_company.name = company.name
            tenant_company.legal_name = company.legal_name or company.name
            tenant_company.active = True
        tenant_session.commit()
    finally:
        tenant_session.close()
        engine.dispose()

    ensure_tenant_schema(database_url, company_id=company.id)

    session_engine, session_factory = _session_factory(database_url)
    tenant_db = session_factory()
    try:
        tenant_db.execute(text("SELECT 1"))
        tenant_db.commit()
    finally:
        tenant_db.close()
        session_engine.dispose()

    tenant.health_status = "ok"
    tenant.notes = "Provisioned against external database"
    master_db.commit()

    return {
        "company_id": str(company.id),
        "company_slug": company.slug,
        "tenant_database": tenant.database_url,
        "admin_email": admin_email,
        "membership_id": str(membership.id),
        "health_status": tenant.health_status,
    }


def provision_demo_external_tenant(
    master_db: Session,
    *,
    tenant_database_url: str,
    company_name: str = "Anchi Demo",
    company_slug: str = "anchi-demo",
    admin_email: str = "admin@anchi.local",
    admin_password: str = "AnchiDemo2026!",
) -> dict[str, str]:
    result = provision_external_tenant(
        master_db,
        tenant_database_url=tenant_database_url,
        company_name=company_name,
        company_slug=company_slug,
        admin_email=admin_email,
        admin_password=admin_password,
    )
    return {
        **result,
        "admin_password": admin_password,
    }
