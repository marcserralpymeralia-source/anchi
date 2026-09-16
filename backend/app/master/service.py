from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.permissions import DEFAULT_ROLE_PERMISSIONS
from app.core.security import hash_password
from app.core.security import verify_password
from app.auth.sessions import validate_server_session
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser, utcnow

DEMO_ADMIN_PASSWORD_FALLBACKS = {"AnchiDemo2026!"}


def configured_platform_admin_email() -> str:
    """Return the only identity allowed to act as Anchi's platform owner."""

    settings = get_settings()
    return (os.getenv("PLATFORM_ADMIN_EMAIL") or settings.default_admin_email or "").strip().lower()


def is_configured_platform_owner(user: MasterUser | None) -> bool:
    return bool(
        user
        and user.platform_role_key == "superadmin"
        and user.email.strip().lower() == configured_platform_admin_email()
    )


@dataclass(slots=True)
class TenantRole:
    name: str
    permissions: str = ""


@dataclass(slots=True)
class TenantCompany:
    id: int
    name: str
    slug: str
    legal_name: str | None = None
    database_url: str | None = None
    database_key: str | None = None


@dataclass(slots=True)
class TenantUser:
    id: int
    email: str
    name: str
    is_active: bool
    company_id: int | None
    company_name: str
    company_slug: str
    role: TenantRole
    membership_id: int | None
    database_url: str | None = None
    platform_role_key: str | None = None
    session_version: int = 1
    master_user_id: int | None = None
    tenant_actor_id: int | None = None

    @property
    def actor_id(self) -> int:
        """Return the local tenant actor id used by operational foreign keys."""

        if self.tenant_actor_id is None:
            raise RuntimeError("La identidad local del tenant no está disponible")
        return int(self.tenant_actor_id)


@dataclass(slots=True)
class TenantContext:
    company: TenantCompany
    user: TenantUser | None = None


def slugify(text: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in text.strip())
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized.strip("-") or "tenant"


def _email_slug(email: str) -> str:
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1].split(".", 1)[0]
    return domain.replace("_", "-").strip().lower()


def _company_to_context(company: MasterCompany, tenant_db: MasterTenantDatabase | None = None) -> TenantCompany:
    return TenantCompany(
        id=company.id,
        name=company.name,
        slug=company.slug,
        legal_name=company.legal_name,
        database_url=tenant_db.database_url if tenant_db else None,
        database_key=tenant_db.database_key if tenant_db else None,
    )


def _membership_to_user(
    membership: CompanyMembership,
    tenant_db: MasterTenantDatabase | None = None,
    *,
    tenant_actor_id: int | None = None,
) -> TenantUser:
    role_name = membership.role_key or "Usuario"
    if role_name == "Superadmin":
        role_name = "Administrador"
    return TenantUser(
        id=membership.user_id,
        email=membership.user.email,
        name=membership.user.full_name,
        is_active=membership.user.is_active and membership.is_active,
        company_id=membership.company_id,
        company_name=membership.company.name,
        company_slug=membership.company.slug,
        role=TenantRole(name=role_name, permissions=DEFAULT_ROLE_PERMISSIONS.get(role_name, "")),
        membership_id=membership.id,
        database_url=tenant_db.database_url if tenant_db else None,
        platform_role_key=(
            membership.user.platform_role_key
            if is_configured_platform_owner(membership.user)
            else None
        ),
        session_version=int(membership.user.session_version or 1),
        master_user_id=membership.user_id,
        tenant_actor_id=tenant_actor_id,
    )


def _platform_user_to_context(user: MasterUser) -> TenantUser:
    """Represent a platform identity without inventing a tenant context."""

    return TenantUser(
        id=user.id,
        email=user.email,
        name=user.full_name,
        is_active=user.is_active,
        company_id=None,
        company_name="Plataforma Anchi",
        company_slug="platform",
        role=TenantRole(name="Superadmin", permissions="manage_tenants,manage_users,view_logs"),
        membership_id=None,
        platform_role_key=user.platform_role_key or "superadmin",
        session_version=int(user.session_version or 1),
        master_user_id=user.id,
    )


def _is_demo_admin_password(password: str, settings) -> bool:
    return password == settings.default_admin_password or password in DEMO_ADMIN_PASSWORD_FALLBACKS


def _repair_demo_master_access(master_db: Session, email: str, password: str, settings) -> bool:
    if not _is_demo_admin_password(password, settings):
        return False
    company_slug = _email_slug(email)
    if not company_slug:
        return False
    company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == company_slug))
    if not company:
        return False

    user = master_db.scalar(select(MasterUser).where(MasterUser.email == email))
    if not user:
        user = MasterUser(
            email=email,
            full_name=f"Administrador {company.name}",
            password_hash=hash_password(password),
            is_active=True,
            platform_role_key="superadmin" if email == settings.default_admin_email.strip().lower() else "tenant_user",
        )
        master_db.add(user)
        master_db.flush()
    else:
        user.full_name = user.full_name or f"Administrador {company.name}"
        user.is_active = True
        if not verify_password(password, user.password_hash):
            user.password_hash = hash_password(password)
        master_db.flush()

    membership = master_db.scalar(
        select(CompanyMembership).where(
            CompanyMembership.user_id == user.id,
            CompanyMembership.company_id == company.id,
        )
    )
    if not membership:
        membership = CompanyMembership(
            user_id=user.id,
            company_id=company.id,
            role_key="Administrador",
            is_active=True,
            is_owner=True,
        )
        master_db.add(membership)
    else:
        membership.role_key = "Administrador"
        membership.is_active = True
        membership.is_owner = True
    master_db.flush()
    return True


def authenticate_master_user(master_db: Session, email: str, password: str) -> TenantUser | None:
    email = (email or "").strip().lower()
    settings = get_settings()
    # Account recovery by email-domain is a local development convenience
    # only.  It must never run on a shared/demo/production deployment where an
    # attacker could manufacture an address and obtain company ownership.
    demo_runtime = settings.environment == "development" or (
        settings.environment == "demo"
        and email == settings.default_admin_email.strip().lower()
    )
    platform_user = master_db.scalar(
        select(MasterUser).where(
            MasterUser.email == email,
            MasterUser.is_active.is_(True),
            MasterUser.platform_role_key == "superadmin",
        )
    )
    if is_configured_platform_owner(platform_user):
        now = utcnow()
        if platform_user.locked_until and platform_user.locked_until > now:
            return None
        if not verify_password(password, platform_user.password_hash):
            platform_user.failed_login_count = (platform_user.failed_login_count or 0) + 1
            if platform_user.failed_login_count >= 5:
                platform_user.locked_until = now + timedelta(minutes=15)
            master_db.commit()
            return None
        platform_user.failed_login_count = 0
        platform_user.locked_until = None
        platform_user.last_login_at = utcnow()
        master_db.commit()
        return _platform_user_to_context(platform_user)
    memberships = master_db.scalars(
        select(CompanyMembership)
        .join(CompanyMembership.user)
        .options(selectinload(CompanyMembership.user), selectinload(CompanyMembership.company))
        .where(
            MasterUser.email == email,
            MasterUser.is_active.is_(True),
            CompanyMembership.is_active.is_(True),
            MasterCompany.active.is_(True),
            MasterCompany.status == "active",
        )
        .join(MasterCompany, MasterCompany.id == CompanyMembership.company_id)
        .order_by(CompanyMembership.is_owner.desc(), CompanyMembership.id.asc())
    ).all()
    if demo_runtime and _is_demo_admin_password(password, settings) and not memberships:
        if _repair_demo_master_access(master_db, email, password, settings):
            master_db.commit()
            memberships = master_db.scalars(
                select(CompanyMembership)
                .join(CompanyMembership.user)
                .options(selectinload(CompanyMembership.user), selectinload(CompanyMembership.company))
                .where(
                    MasterUser.email == email,
                    MasterUser.is_active.is_(True),
                    CompanyMembership.is_active.is_(True),
                    MasterCompany.active.is_(True),
                    MasterCompany.status == "active",
                )
                .join(MasterCompany, MasterCompany.id == CompanyMembership.company_id)
                .order_by(CompanyMembership.is_owner.desc(), CompanyMembership.id.asc())
            ).all()
    if not memberships:
        return None
    master_user = memberships[0].user
    now = utcnow()
    if master_user.locked_until and master_user.locked_until > now:
        return None
    password_ok = verify_password(password, master_user.password_hash)
    if not password_ok and demo_runtime and _is_demo_admin_password(password, settings):
        password_ok = True
    if not password_ok:
        master_user.failed_login_count = (master_user.failed_login_count or 0) + 1
        if master_user.failed_login_count >= 5:
            master_user.locked_until = now + timedelta(minutes=15)
        master_db.commit()
        return None

    master_user.failed_login_count = 0
    master_user.locked_until = None
    master_user.last_login_at = now
    master_db.commit()

    email_slug = ""
    if "@" in email:
        email_slug = email.split("@", 1)[1].split(".", 1)[0].replace("_", "-").strip().lower()

    def _tenant_db_for(membership: CompanyMembership):
        return master_db.scalar(
            select(MasterTenantDatabase).where(
                MasterTenantDatabase.company_id == membership.company_id,
                MasterTenantDatabase.is_active.is_(True),
            )
        )

    ordered_memberships = sorted(
        memberships,
        key=lambda membership: (
            0 if email_slug and membership.company.slug.lower() == email_slug else 1,
            0 if _tenant_db_for(membership) else 1,
            0 if membership.company.active else 1,
            0 if membership.is_owner else 1,
            membership.id,
        ),
    )
    membership = ordered_memberships[0]
    tenant_db = _tenant_db_for(membership)
    return _membership_to_user(membership, tenant_db)


def load_tenant_context(request, master_db: Session) -> TenantContext | None:
    session = request.scope.get("session") or {}
    membership_id = session.get("membership_id")
    user_id = session.get("user_id")
    company_id = session.get("company_id")
    company_slug = session.get("company_slug")
    host = (request.headers.get("host") or "").split(":")[0].lower()
    settings = get_settings()
    running_on_vercel = settings.environment == "demo" or os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))

    if not membership_id or not user_id or not company_id:
        return None

    membership = master_db.scalar(
        select(CompanyMembership)
        .options(selectinload(CompanyMembership.user), selectinload(CompanyMembership.company))
        .where(
            CompanyMembership.id == membership_id,
            CompanyMembership.user_id == user_id,
            CompanyMembership.company_id == company_id,
            CompanyMembership.is_active.is_(True),
        )
    )
    if not membership or not membership.user.is_active or not membership.company.active:
        return None
    if not validate_server_session(request, master_db, membership.user):
        request.state.auth_invalid = True
        return None
    expected_version = session.get("tenant_session_version")
    try:
        if expected_version is not None and int(expected_version) != int(membership.user.session_version or 1):
            request.state.auth_invalid = True
            return None
    except (TypeError, ValueError):
        request.state.auth_invalid = True
        return None
    if company_slug and membership.company.slug != company_slug:
        return None
    if not running_on_vercel and host and host not in {"localhost", "127.0.0.1"} and "." in host:
        subdomain = host.split(".", 1)[0]
        if subdomain != membership.company.slug:
            return None

    tenant_db = master_db.scalar(
        select(MasterTenantDatabase).where(
            MasterTenantDatabase.company_id == membership.company_id,
            MasterTenantDatabase.is_active.is_(True),
        )
    )
    company = _company_to_context(membership.company, tenant_db)
    actor_id = session.get("tenant_actor_id")
    try:
        actor_id = int(actor_id) if actor_id is not None else None
    except (TypeError, ValueError):
        actor_id = None
    user = _membership_to_user(membership, tenant_db, tenant_actor_id=actor_id)
    return TenantContext(company=company, user=user)
