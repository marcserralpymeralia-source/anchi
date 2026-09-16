from __future__ import annotations

import json
from datetime import datetime, timezone
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS
from app.core.security import hash_password
from app.db.database import Base
from app.db.models import Company as TenantCompany
from app.db.models import Role, User
from app.master.models import (
    CompanyMembership,
    MasterCompany,
    MasterTenantDatabase,
    MasterUser,
    PlatformAuditLog,
    TenantProvisioningRun,
    TenantHealthSnapshot,
    TenantUsageDaily,
)
from app.master.provisioning import tenant_database_path
from app.master.service import configured_platform_admin_email, slugify
from app.tenancy.migrations import ensure_tenant_schema


TENANT_ROLES = ("Administrador", "Supervisor", "Operador", "Solo lectura")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _database_url_for_company(company: MasterCompany, requested_url: str | None) -> str:
    requested_url = (requested_url or "").strip()
    if requested_url:
        return requested_url
    settings = get_settings()
    if settings.environment == "production":
        raise ValueError("En producción debes indicar la URL de la base de datos de la empresa")
    path: Path = tenant_database_path(company)
    return f"sqlite:///{path.as_posix()}"


def _engine_for(database_url: str):
    return create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )


def _ensure_tenant_roles(db: Session, company_id: int) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for role_name in TENANT_ROLES:
        role = db.scalar(select(Role).where(Role.company_id == company_id, Role.name == role_name))
        if role is None:
            role = Role(company_id=company_id, name=role_name, permissions=DEFAULT_ROLE_PERMISSIONS.get(role_name, ""))
            db.add(role)
            db.flush()
        roles[role_name] = role
    return roles


def _ensure_local_actor(db: Session, company_id: int, master_user: MasterUser, *, role_key: str, password: str | None = None) -> User:
    roles = _ensure_tenant_roles(db, company_id)
    role = roles.get(role_key) or roles["Operador"]
    local_user = db.scalar(
        select(User).where(User.company_id == company_id, User.master_user_id == master_user.id)
    )
    if local_user is None:
        local_user = db.scalar(select(User).where(User.company_id == company_id, User.email == master_user.email))
    if local_user is None:
        local_user = User(
            company_id=company_id,
            role_id=role.id,
            email=master_user.email,
            name=master_user.full_name,
            password_hash=master_user.password_hash if password is None else hash_password(password),
            is_active=True,
            master_user_id=master_user.id,
            actor_type="human",
        )
        db.add(local_user)
    else:
        local_user.master_user_id = master_user.id
        local_user.actor_type = "human"
        local_user.role_id = role.id
        local_user.name = master_user.full_name
        local_user.is_active = True
    db.flush()
    return local_user


def _ensure_master_user(
    master_db: Session,
    *,
    email: str,
    full_name: str,
    password: str | None,
) -> tuple[MasterUser, bool]:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        raise ValueError("Introduce un email válido")
    user = master_db.scalar(select(MasterUser).where(func.lower(MasterUser.email) == normalized))
    created = user is None
    if user is None:
        if not password:
            raise ValueError("La contraseña es obligatoria para un usuario nuevo")
        if len(password) < 12:
            raise ValueError("La contraseña debe tener al menos 12 caracteres")
        user = MasterUser(
            email=normalized,
            full_name=(full_name or normalized.split("@", 1)[0]).strip()[:200],
            password_hash=hash_password(password),
            is_active=True,
            platform_role_key="tenant_user",
            email_verified=False,
        )
        master_db.add(user)
        master_db.flush()
    else:
        user.full_name = (full_name or user.full_name).strip()[:200]
        user.is_active = True
        if user.platform_role_key != "superadmin":
            user.platform_role_key = "tenant_user"
    return user, created


def _ensure_tenant_database(master_db: Session, company: MasterCompany, database_url: str) -> MasterTenantDatabase:
    tenant_db = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company.id))
    database_type = database_url.split("://", 1)[0] if "://" in database_url else "unknown"
    if tenant_db is None:
        tenant_db = MasterTenantDatabase(
            company_id=company.id,
            database_key=slugify(company.slug),
            database_url=database_url,
            database_type=database_type,
            is_active=True,
            health_status="pending",
        )
        master_db.add(tenant_db)
    else:
        tenant_db.database_key = slugify(company.slug)
        tenant_db.database_url = database_url
        tenant_db.database_type = database_type
        tenant_db.is_active = True
        tenant_db.health_status = "pending"
    master_db.flush()
    return tenant_db


def _provision_tenant_schema(company: MasterCompany, database_url: str, *, display_name: str) -> None:
    engine = _engine_for(database_url)
    try:
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        tenant_db = factory()
        try:
            tenant_company = tenant_db.get(TenantCompany, company.id)
            if tenant_company is None:
                tenant_db.add(TenantCompany(id=company.id, name=display_name, legal_name=company.legal_name or display_name, active=True))
            else:
                tenant_company.name = display_name
                tenant_company.active = True
            _ensure_tenant_roles(tenant_db, company.id)
            tenant_db.commit()
        finally:
            tenant_db.close()
        ensure_tenant_schema(database_url, company_id=company.id)
    finally:
        engine.dispose()


def record_platform_audit(
    master_db: Session,
    *,
    actor_user_id: int | None,
    action: str,
    target_type: str,
    target_id: int | str | None = None,
    company_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    request=None,
) -> None:
    """Persist metadata-only control-plane audit events without secrets."""

    master_db.add(
        PlatformAuditLog(
            actor_user_id=actor_user_id,
            company_id=company_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            outcome="success",
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            created_at=_utcnow(),
        )
    )


def create_company(
    master_db: Session,
    *,
    name: str,
    slug: str | None,
    database_url: str | None,
    actor_user_id: int,
    provision_async: bool = False,
) -> MasterCompany:
    """Create a tenant workspace without creating an identity.

    Companies are control-plane resources. Login credentials and memberships
    are created explicitly later through :func:`create_company_user`.
    """

    name = (name or "").strip()[:200]
    company_slug = slugify(slug or name)
    if not name:
        raise ValueError("El nombre de la empresa es obligatorio")
    if master_db.scalar(select(MasterCompany).where((MasterCompany.name == name) | (MasterCompany.slug == company_slug))):
        raise ValueError("Ya existe una empresa con ese nombre o slug")

    company = MasterCompany(
        name=name,
        slug=company_slug,
        legal_name=name,
        active=False,
        status="provisioning",
        provisioning_status="provisioning",
    )
    master_db.add(company)
    master_db.flush()
    run = TenantProvisioningRun(
        company_id=company.id,
        requested_by_user_id=actor_user_id,
        operation="create",
        status="pending" if provision_async else "running",
        current_step="queued" if provision_async else "schema",
        started_at=None if provision_async else _utcnow(),
    )
    master_db.add(run)
    master_db.flush()
    try:
        resolved_url = _database_url_for_company(company, database_url)
        tenant_row = _ensure_tenant_database(master_db, company, resolved_url)
        master_db.flush()
        if provision_async:
            record_platform_audit(
                master_db,
                actor_user_id=actor_user_id,
                action="company.create_queued",
                target_type="company",
                target_id=company.id,
                company_id=company.id,
                metadata={"slug": company.slug},
            )
            master_db.commit()
            return company
        _provision_tenant_schema(company, resolved_url, display_name=name)
        tenant_row.health_status = "ok"
        tenant_row.provisioned_at = tenant_row.provisioned_at or _utcnow()
        company.active = True
        company.status = "active"
        company.provisioning_status = "ready"
        run.status = "completed"
        run.current_step = "ready"
        run.finished_at = _utcnow()
        record_platform_audit(master_db, actor_user_id=actor_user_id, action="company.create", target_type="company", target_id=company.id, company_id=company.id, metadata={"slug": company.slug})
        master_db.commit()
        return company
    except Exception as exc:
        master_db.rollback()
        company = master_db.get(MasterCompany, company.id)
        if company:
            company.active = False
            company.status = "error"
            company.provisioning_status = "failed"
            run = master_db.scalar(select(TenantProvisioningRun).where(TenantProvisioningRun.company_id == company.id).order_by(TenantProvisioningRun.id.desc()))
            if run:
                run.status = "failed"
                run.error_message = exc.__class__.__name__
                run.finished_at = _utcnow()
            record_platform_audit(
                master_db,
                actor_user_id=actor_user_id,
                action="company.create_failed",
                target_type="company",
                target_id=company.id,
                company_id=company.id,
                metadata={"error_type": exc.__class__.__name__},
            )
            master_db.commit()
        raise


def create_company_user(
    master_db: Session,
    *,
    company_id: int,
    full_name: str,
    email: str,
    password: str | None,
    role_key: str,
    actor_user_id: int,
) -> CompanyMembership:
    """Create or assign a tenant login; never elevate it to platform access."""

    company = master_db.get(MasterCompany, company_id)
    tenant_row = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company_id, MasterTenantDatabase.is_active.is_(True)))
    if not company or not company.active or not tenant_row or not tenant_row.database_url:
        raise ValueError("La empresa no está disponible para crear usuarios")
    if role_key not in TENANT_ROLES:
        raise ValueError("Rol no válido")
    normalized_email = normalize_email(email)
    reserved_platform_email = configured_platform_admin_email()
    if normalized_email == reserved_platform_email:
        raise ValueError("El email reservado para el Superadmin de plataforma no puede asignarse a una empresa")
    user, created = _ensure_master_user(master_db, email=email, full_name=full_name, password=password)
    if user.platform_role_key == "superadmin":
        raise ValueError("La identidad global de Anchi no puede convertirse en un usuario de empresa")
    membership = master_db.scalar(select(CompanyMembership).where(CompanyMembership.user_id == user.id, CompanyMembership.company_id == company_id))
    if membership is None:
        has_active_owner = bool(
            master_db.scalar(
                select(func.count(CompanyMembership.id)).where(
                    CompanyMembership.company_id == company_id,
                    CompanyMembership.is_owner.is_(True),
                    CompanyMembership.is_active.is_(True),
                )
            )
        )
        membership = CompanyMembership(
            user_id=user.id,
            company_id=company_id,
            role_key=role_key,
            is_active=True,
            is_owner=role_key == "Administrador" and not has_active_owner,
        )
        master_db.add(membership)
    else:
        membership.role_key = role_key
        membership.is_active = True
    master_db.flush()
    engine = _engine_for(tenant_row.database_url)
    try:
        tenant_db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
        try:
            _ensure_local_actor(tenant_db, company_id, user, role_key=role_key, password=password if created else None)
            tenant_db.commit()
        finally:
            tenant_db.close()
    finally:
        engine.dispose()
    record_platform_audit(master_db, actor_user_id=actor_user_id, action="user.create" if created else "membership.create", target_type="user", target_id=user.id, company_id=company_id, metadata={"role": role_key})
    master_db.commit()
    return membership


def toggle_company(master_db: Session, company_id: int, actor_user_id: int) -> MasterCompany:
    company = master_db.get(MasterCompany, company_id)
    if not company:
        raise ValueError("Empresa no encontrada")
    if not company.active and company.provisioning_status != "ready":
        raise ValueError("La empresa todavía no está aprovisionada y no se puede activar")
    company.active = not company.active
    company.status = "active" if company.active else "suspended"
    company.provisioning_status = "ready" if company.active else company.provisioning_status
    record_platform_audit(master_db, actor_user_id=actor_user_id, action="company.activate" if company.active else "company.suspend", target_type="company", target_id=company.id, company_id=company.id, metadata={})
    master_db.commit()
    return company


def retry_company_provisioning(master_db: Session, company_id: int, actor_user_id: int) -> MasterCompany:
    """Queue a failed tenant setup for the provisioning worker."""

    company = master_db.get(MasterCompany, company_id)
    if not company:
        raise ValueError("Empresa no encontrada")
    tenant_row = master_db.scalar(
        select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company_id)
    )
    if not tenant_row or not tenant_row.database_url:
        raise ValueError("La empresa no tiene una base de datos configurada")
    active_run = master_db.scalar(
        select(TenantProvisioningRun).where(
            TenantProvisioningRun.company_id == company_id,
            TenantProvisioningRun.status.in_(("pending", "running")),
        )
    )
    if active_run is not None:
        raise ValueError("La empresa ya tiene un aprovisionamiento en curso")

    run = TenantProvisioningRun(
        company_id=company_id,
        requested_by_user_id=actor_user_id,
        operation="retry",
        status="pending",
        current_step="queued",
    )
    company.active = False
    company.status = "provisioning"
    company.provisioning_status = "provisioning"
    master_db.add(run)
    record_platform_audit(
        master_db,
        actor_user_id=actor_user_id,
        action="company.provisioning.retry_queued",
        target_type="company",
        target_id=company_id,
        company_id=company_id,
        metadata={},
    )
    master_db.commit()
    return company


def toggle_user(master_db: Session, user_id: int, actor_user_id: int) -> MasterUser:
    user = master_db.get(MasterUser, user_id)
    if not user:
        raise ValueError("Usuario no encontrado")
    if user.id == actor_user_id and user.platform_role_key == "superadmin":
        raise ValueError("No puedes desactivar tu propio acceso de plataforma")
    if user.is_active:
        owned_companies = master_db.scalars(
            select(CompanyMembership).where(
                CompanyMembership.user_id == user.id,
                CompanyMembership.is_owner.is_(True),
                CompanyMembership.is_active.is_(True),
            )
        ).all()
        for membership in owned_companies:
            active_owners = int(
                master_db.scalar(
                    select(func.count(CompanyMembership.id)).where(
                        CompanyMembership.company_id == membership.company_id,
                        CompanyMembership.is_owner.is_(True),
                        CompanyMembership.is_active.is_(True),
                        CompanyMembership.user_id != user.id,
                    )
                )
                or 0
            )
            if active_owners == 0:
                raise ValueError(
                    "No puedes suspender al último propietario activo de una empresa. "
                    "Transfiere la propiedad antes de continuar."
                )
    user.is_active = not user.is_active
    user.password_version = (user.password_version or 1) + 1
    user.session_version = (user.session_version or 1) + 1
    record_platform_audit(master_db, actor_user_id=actor_user_id, action="user.activate" if user.is_active else "user.suspend", target_type="user", target_id=user.id, metadata={})
    master_db.commit()
    return user


def platform_stats(master_db: Session) -> dict[str, Any]:
    companies_total = int(master_db.scalar(select(func.count(MasterCompany.id))) or 0)
    active_companies = int(master_db.scalar(select(func.count(MasterCompany.id)).where(MasterCompany.active.is_(True))) or 0)
    users_total = int(master_db.scalar(select(func.count(MasterUser.id))) or 0)
    active_users = int(master_db.scalar(select(func.count(MasterUser.id)).where(MasterUser.is_active.is_(True))) or 0)
    memberships_total = int(master_db.scalar(select(func.count(CompanyMembership.id))) or 0)
    provisioned = int(master_db.scalar(select(func.count(MasterTenantDatabase.id)).where(MasterTenantDatabase.health_status == "ok", MasterTenantDatabase.is_active.is_(True))) or 0)
    audit_total = int(master_db.scalar(select(func.count(PlatformAuditLog.id))) or 0)
    today = date.today()
    usage_today = {
        "orders_today": int(master_db.scalar(select(func.coalesce(func.sum(TenantUsageDaily.orders_total), 0)).where(TenantUsageDaily.usage_date == today)) or 0),
        "messages_today": int(master_db.scalar(select(func.coalesce(func.sum(TenantUsageDaily.messages_total), 0)).where(TenantUsageDaily.usage_date == today)) or 0),
        "active_users_today": int(master_db.scalar(select(func.coalesce(func.sum(TenantUsageDaily.active_users_total), 0)).where(TenantUsageDaily.usage_date == today)) or 0),
    }
    return {
        "companies_total": companies_total,
        "active_companies": active_companies,
        "users_total": users_total,
        "active_users": active_users,
        "memberships_total": memberships_total,
        "provisioned_companies": provisioned,
        "audit_events_total": audit_total,
        "usage_snapshots": int(master_db.scalar(select(func.count(TenantUsageDaily.id))) or 0),
        **usage_today,
    }
