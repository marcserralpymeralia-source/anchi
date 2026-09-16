from __future__ import annotations

import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.db.models import User
from app.master.database import MasterBase
from app.master.bootstrap import ensure_platform_admin
from app.master.models import CompanyMembership, MasterTenantDatabase, MasterUser
from app.master.service import authenticate_master_user
from app.superadmin.service import create_company, create_company_user, platform_stats, toggle_user
from app.tenancy.database import clear_tenant_engine_cache, tenant_db_session
from app.whatsapp.service import resolve_company_from_whatsapp_identifiers, upsert_master_whatsapp_endpoint


class SuperadminProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.master_engine = create_engine(
            f"sqlite:///{Path(self.temp_dir.name, 'master.db').as_posix()}",
            connect_args={"check_same_thread": False},
        )
        MasterBase.metadata.create_all(self.master_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.db = self.MasterSession()
        self.db.add(
            MasterUser(
                id=1,
                email="root@example.com",
                full_name="Platform Root",
                password_hash=hash_password("RootPassword123!"),
                is_active=True,
                platform_role_key="superadmin",
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.master_engine.dispose()
        clear_tenant_engine_cache()
        self.temp_dir.cleanup()

    def test_company_and_user_are_provisioned_and_login_uses_master_identity(self):
        tenant_url = f"sqlite:///{Path(self.temp_dir.name, 'tenant.db').as_posix()}"
        company = create_company(
            self.db,
            name="Acme Test",
            slug="acme-test",
            database_url=tenant_url,
            actor_user_id=1,
        )

        membership = create_company_user(
            self.db,
            company_id=company.id,
            full_name="Operator One",
            email="operator@acme.test",
            password="OperatorPass123!",
            role_key="Operador",
            actor_user_id=1,
        )

        authenticated = authenticate_master_user(self.db, "operator@acme.test", "OperatorPass123!")
        self.assertIsNotNone(authenticated)
        self.assertEqual(authenticated.company_id, company.id)
        self.assertEqual(authenticated.role.name, "Operador")

        tenant_db = tenant_db_session(tenant_url)()
        try:
            local_actor = tenant_db.scalar(select(User).where(User.master_user_id == membership.user_id))
            self.assertIsNotNone(local_actor)
            self.assertEqual(local_actor.actor_type, "human")
            self.assertEqual(local_actor.email, "operator@acme.test")
        finally:
            tenant_db.close()

        stats = platform_stats(self.db)
        self.assertEqual(stats["companies_total"], 1)
        self.assertEqual(stats["provisioned_companies"], 1)
        self.assertEqual(stats["memberships_total"], 1)
        self.assertGreaterEqual(stats["audit_events_total"], 2)

    def test_company_creation_does_not_create_a_login_or_membership(self):
        tenant_url = f"sqlite:///{Path(self.temp_dir.name, 'company-only.db').as_posix()}"
        company = create_company(
            self.db,
            name="Company Without Login",
            slug="company-without-login",
            database_url=tenant_url,
            actor_user_id=1,
        )

        self.assertIsNone(
            self.db.scalar(
                select(CompanyMembership).where(CompanyMembership.company_id == company.id)
            )
        )
        self.assertIsNone(
            self.db.scalar(
                select(MasterUser).where(MasterUser.email == "admin@company-without-login.test")
            )
        )

    def test_platform_owner_cannot_be_assigned_as_a_company_user(self):
        tenant_url = f"sqlite:///{Path(self.temp_dir.name, 'platform-owner.db').as_posix()}"
        company = create_company(
            self.db,
            name="Tenant Without Platform Owner",
            slug="tenant-without-platform-owner",
            database_url=tenant_url,
            actor_user_id=1,
        )

        with self.assertRaisesRegex(ValueError, "identidad global"):
            create_company_user(
                self.db,
                company_id=company.id,
                full_name="Platform Root",
                email="root@example.com",
                password=None,
                role_key="Administrador",
                actor_user_id=1,
            )

    def test_global_superadmin_role_cannot_be_assigned_to_a_company_user(self):
        tenant_url = f"sqlite:///{Path(self.temp_dir.name, 'tenant-role-guard.db').as_posix()}"
        company = create_company(
            self.db,
            name="Tenant Role Guard",
            slug="tenant-role-guard",
            database_url=tenant_url,
            actor_user_id=1,
        )

        with self.assertRaisesRegex(ValueError, "Rol no válido"):
            create_company_user(
                self.db,
                company_id=company.id,
                full_name="Tenant Superadmin",
                email="tenant-superadmin@example.test",
                password="TenantPassword123!",
                role_key="Superadmin",
                actor_user_id=1,
            )

    def test_suspending_user_revokes_active_session_version(self):
        user = MasterUser(
            email="user@example.com",
            full_name="Tenant User",
            password_hash=hash_password("UserPassword123!"),
            is_active=True,
            platform_role_key="tenant_user",
        )
        self.db.add(user)
        self.db.commit()
        version_before = user.session_version

        toggle_user(self.db, user.id, actor_user_id=1)

        self.assertFalse(user.is_active)
        self.assertEqual(user.session_version, version_before + 1)

    def test_last_active_owner_cannot_be_suspended(self):
        tenant_url = f"sqlite:///{Path(self.temp_dir.name, 'owner-guard.db').as_posix()}"
        company = create_company(
            self.db,
            name="Owner Guard",
            slug="owner-guard",
            database_url=tenant_url,
            actor_user_id=1,
        )
        create_company_user(
            self.db,
            company_id=company.id,
            full_name="Owner",
            email="owner@owner-guard.test",
            password="OwnerPassword123!",
            role_key="Administrador",
            actor_user_id=1,
        )
        owner = self.db.scalar(select(MasterUser).where(MasterUser.email == "owner@owner-guard.test"))
        with self.assertRaisesRegex(ValueError, "último propietario"):
            toggle_user(self.db, owner.id, actor_user_id=1)

        self.db.refresh(owner)
        self.assertTrue(owner.is_active)

    def test_production_bootstrap_does_not_reactivate_suspended_admin(self):
        admin = self.db.get(MasterUser, 1)
        admin.is_active = False
        self.db.commit()

        production_settings = SimpleNamespace(
            environment="production",
            default_admin_email="root@example.com",
            default_admin_password="RootPassword123!",
        )
        with patch.dict(os.environ, {"PLATFORM_ADMIN_PASSWORD": "RootPassword123!"}, clear=False), patch(
            "app.master.bootstrap.get_settings", return_value=production_settings
        ):
            ensure_platform_admin(self.db)

        self.db.refresh(admin)
        self.assertFalse(admin.is_active)

    def test_whatsapp_endpoint_index_routes_without_scanning_tenant_databases(self):
        tenant_url = f"sqlite:///{Path(self.temp_dir.name, 'tenant-whatsapp.db').as_posix()}"
        company = create_company(
            self.db,
            name="WhatsApp Tenant",
            slug="whatsapp-tenant",
            database_url=tenant_url,
            actor_user_id=1,
        )
        upsert_master_whatsapp_endpoint(
            self.db,
            company_id=company.id,
            business_account_id="123456789",
            phone_number_id="987654321",
        )
        self.db.commit()

        resolved_company, resolved_tenant = resolve_company_from_whatsapp_identifiers(
            self.db,
            business_account_id="123456789",
            phone_number_id="987654321",
        )

        self.assertEqual(resolved_company.id, company.id)
        self.assertEqual(resolved_tenant.database_url, tenant_url)


if __name__ == "__main__":
    unittest.main()
