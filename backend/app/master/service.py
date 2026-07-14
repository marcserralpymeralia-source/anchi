from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import verify_password
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser


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
    company_id: int
    company_name: str
    company_slug: str
    role: TenantRole
    membership_id: int
    database_url: str | None = None


@dataclass(slots=True)
class TenantContext:
    company: TenantCompany
    user: TenantUser | None = None


def slugify(text: str) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in text.strip())
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized.strip("-") or "tenant"


def _company_to_context(company: MasterCompany, tenant_db: MasterTenantDatabase | None = None) -> TenantCompany:
    return TenantCompany(
        id=company.id,
        name=company.name,
        slug=company.slug,
        legal_name=company.legal_name,
        database_url=tenant_db.database_url if tenant_db else None,
        database_key=tenant_db.database_key if tenant_db else None,
    )


def _membership_to_user(membership: CompanyMembership, tenant_db: MasterTenantDatabase | None = None) -> TenantUser:
    return TenantUser(
        id=membership.user_id,
        email=membership.user.email,
        name=membership.user.full_name,
        is_active=membership.user.is_active and membership.is_active,
        company_id=membership.company_id,
        company_name=membership.company.name,
        company_slug=membership.company.slug,
        role=TenantRole(name=membership.role_key or "Usuario", permissions=""),
        membership_id=membership.id,
        database_url=tenant_db.database_url if tenant_db else None,
    )


def authenticate_master_user(master_db: Session, email: str, password: str) -> TenantUser | None:
    membership = master_db.scalar(
        select(CompanyMembership)
        .join(CompanyMembership.user)
        .options(selectinload(CompanyMembership.user), selectinload(CompanyMembership.company))
        .where(MasterUser.email == email, MasterUser.is_active.is_(True), CompanyMembership.is_active.is_(True))
    )
    if not membership:
        return None
    if not verify_password(password, membership.user.password_hash):
        return None
    tenant_db = master_db.scalar(
        select(MasterTenantDatabase).where(
            MasterTenantDatabase.company_id == membership.company_id,
            MasterTenantDatabase.is_active.is_(True),
        )
    )
    return _membership_to_user(membership, tenant_db)


def load_tenant_context(request, master_db: Session) -> TenantContext | None:
    session = request.scope.get("session") or {}
    membership_id = session.get("membership_id")
    user_id = session.get("user_id")
    company_id = session.get("company_id")
    company_slug = session.get("company_slug")
    host = (request.headers.get("host") or "").split(":")[0].lower()

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
    if company_slug and membership.company.slug != company_slug:
        return None
    if host and host not in {"localhost", "127.0.0.1"} and "." in host:
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
    user = _membership_to_user(membership, tenant_db)
    return TenantContext(company=company, user=user)
