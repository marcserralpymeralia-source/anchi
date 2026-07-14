from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser
from app.master.provisioning import provision_company_database
from app.master.service import slugify


def sync_master_from_legacy_db(master_db: Session, legacy_db: Session) -> dict[str, int]:
    from app.db.models import Company, Role, User

    created_companies = 0
    created_users = 0
    created_memberships = 0
    created_tenant_dbs = 0

    companies = legacy_db.scalars(select(Company).order_by(Company.id.asc())).all()
    for company in companies:
        slug = slugify(company.name)
        master_company = master_db.scalar(select(MasterCompany).where(MasterCompany.slug == slug))
        if not master_company:
            master_company = MasterCompany(
                id=company.id,
                name=company.name,
                slug=slug,
                legal_name=company.legal_name,
                active=company.active,
                default_language=company.default_language,
                default_timezone=company.timezone,
            )
            master_db.add(master_company)
            master_db.flush()
            created_companies += 1
        else:
            master_company.name = company.name
            master_company.legal_name = company.legal_name
            master_company.active = company.active

        tenant_db, was_provisioned = provision_company_database(master_db, legacy_db, master_company)
        created_tenant_dbs += 1 if was_provisioned else 0

        roles_by_id = {role.id: role for role in legacy_db.scalars(select(Role).where(Role.company_id == company.id)).all()}
        for legacy_user in legacy_db.scalars(select(User).where(User.company_id == company.id)).all():
            master_user = master_db.scalar(select(MasterUser).where(MasterUser.email == legacy_user.email))
            if not master_user:
                master_user = MasterUser(
                    id=legacy_user.id,
                    email=legacy_user.email,
                    full_name=legacy_user.name,
                    password_hash=legacy_user.password_hash,
                    is_active=legacy_user.is_active,
                )
                master_db.add(master_user)
                master_db.flush()
                created_users += 1
            else:
                master_user.full_name = legacy_user.name
                master_user.password_hash = legacy_user.password_hash
                master_user.is_active = legacy_user.is_active

            role = roles_by_id.get(legacy_user.role_id)
            membership = master_db.scalar(
                select(CompanyMembership).where(
                    CompanyMembership.user_id == master_user.id,
                    CompanyMembership.company_id == master_company.id,
                )
            )
            if not membership:
                membership = CompanyMembership(
                    user_id=master_user.id,
                    company_id=master_company.id,
                    role_key=role.name if role else "Administrador",
                    is_active=legacy_user.is_active,
                    is_owner=(role.name if role else "").lower() == "superadmin",
                )
                master_db.add(membership)
                created_memberships += 1
            else:
                membership.role_key = role.name if role else membership.role_key
                membership.is_active = legacy_user.is_active
                membership.is_owner = (role.name if role else "").lower() == "superadmin"

        master_db.flush()
    master_db.commit()
    return {
        "companies": created_companies,
        "users": created_users,
        "memberships": created_memberships,
        "tenant_databases": created_tenant_dbs,
    }
