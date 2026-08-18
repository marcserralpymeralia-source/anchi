from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("APP_ENV", "development")

import sys

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.dependencies import require_master_admin  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Customer, ScoringSettings  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.master.service import TenantRole, TenantUser, load_tenant_context  # noqa: E402
from app.tenancy.database import clear_tenant_schema_cache, ensure_tenant_schema, get_tenant_db  # noqa: E402


class FakeRequest:
    def __init__(self, session: dict | None = None, host: str = "localhost"):
        self.scope = {"session": session or {}}
        self.headers = {"host": host}
        self.state = SimpleNamespace()


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        clear_tenant_schema_cache()
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_a_path = base / "tenant-a.sqlite"
        self.tenant_b_path = base / "tenant-b.sqlite"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_a_engine = create_engine(f"sqlite:///{self.tenant_a_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_b_engine = create_engine(f"sqlite:///{self.tenant_b_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_a_engine)
        Base.metadata.create_all(self.tenant_b_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantASession = sessionmaker(bind=self.tenant_a_engine, autoflush=False, autocommit=False)
        self.TenantBSession = sessionmaker(bind=self.tenant_b_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        clear_tenant_schema_cache()
        self.master_engine.dispose()
        self.tenant_a_engine.dispose()
        self.tenant_b_engine.dispose()
        self.tempdir.cleanup()

    def _seed_master_company(self, *, company_id: int, slug: str, name: str, user_id: int, email: str, role_key: str, membership_id: int, active_company: bool = True, active_membership: bool = True, active_user: bool = True, database_url: str | None = None):
        db = self.MasterSession()
        company = db.get(MasterCompany, company_id)
        if company is None:
            company = MasterCompany(id=company_id, name=name, slug=slug, active=active_company)
        else:
            company.name = name
            company.slug = slug
            company.active = active_company

        user = db.get(MasterUser, user_id)
        if user is None:
            user = MasterUser(id=user_id, email=email, full_name=f"User {user_id}", password_hash=hash_password("admin123"), is_active=active_user)
        else:
            user.email = email
            user.full_name = f"User {user_id}"
            user.is_active = active_user

        membership = db.get(CompanyMembership, membership_id)
        if membership is None:
            membership = CompanyMembership(id=membership_id, user_id=user_id, company_id=company_id, role_key=role_key, is_active=active_membership, is_owner=role_key == "Superadmin")
        else:
            membership.user_id = user_id
            membership.company_id = company_id
            membership.role_key = role_key
            membership.is_active = active_membership
            membership.is_owner = role_key == "Superadmin"

        tenant_db = db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company_id))
        if tenant_db is None:
            tenant_db = MasterTenantDatabase(
                company_id=company_id,
                database_key=slug,
                database_url=database_url or f"sqlite:///{(self.tenant_a_path if company_id == 1 else self.tenant_b_path).as_posix()}",
                is_active=True,
                health_status="ok",
            )
        else:
            tenant_db.database_key = slug
            tenant_db.database_url = database_url or tenant_db.database_url
            tenant_db.is_active = True
            tenant_db.health_status = "ok"

        db.add_all([company, user, membership, tenant_db])
        db.commit()
        db.close()

    def _seed_tenant_customer(self, session_factory, *, customer_id: int, company_id: int, code: str, name: str):
        db = session_factory()
        db.add(Company(id=company_id, name=f"Company {company_id}", active=True))
        db.add(Customer(id=customer_id, company_id=company_id, code=code, fiscal_name=name, commercial_name=name))
        db.commit()
        db.close()

    def test_load_tenant_context_requires_complete_and_matching_session(self):
        self._seed_master_company(company_id=1, slug="demo-a", name="Demo A", user_id=1, email="a@example.com", role_key="Administrador", membership_id=1)

        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo-a"})
        master_db = self.MasterSession()
        context = load_tenant_context(request, master_db)
        self.assertIsNotNone(context)
        self.assertEqual(context.company.slug, "demo-a")
        self.assertEqual(context.user.company_id, 1)
        master_db.close()

        master_db = self.MasterSession()
        self.assertIsNone(load_tenant_context(FakeRequest(session={"user_id": 1, "company_id": 1, "company_slug": "demo-a"}), master_db))
        self.assertIsNone(load_tenant_context(FakeRequest(session={"membership_id": 1, "company_id": 1, "company_slug": "demo-a"}), master_db))
        self.assertIsNone(load_tenant_context(FakeRequest(session={"membership_id": 1, "user_id": 1, "company_slug": "demo-a"}), master_db))
        self.assertIsNone(load_tenant_context(FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo-a"}, host="evil.example.com"), master_db))
        self.assertIsNone(load_tenant_context(FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "wrong-slug"}), master_db))
        master_db.close()

    def test_load_tenant_context_skips_host_match_on_vercel(self):
        self._seed_master_company(company_id=1, slug="demo-a", name="Demo A", user_id=1, email="a@example.com", role_key="Administrador", membership_id=1)

        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo-a"}, host="demo-a.vercel.app")
        master_db = self.MasterSession()
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=False):
            context = load_tenant_context(request, master_db)
        self.assertIsNotNone(context)
        self.assertEqual(context.company.slug, "demo-a")
        master_db.close()

    def test_load_tenant_context_rejects_cross_user_or_inactive_membership(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        other_company = MasterCompany(id=2, name="Other", slug="other", active=False)
        user = MasterUser(id=1, email="user1@example.com", full_name="User 1", password_hash=hash_password("admin123"), is_active=True)
        other_user = MasterUser(id=2, email="user2@example.com", full_name="User 2", password_hash=hash_password("admin123"), is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=False)
        other_membership = CompanyMembership(id=2, user_id=2, company_id=2, role_key="Administrador", is_active=False, is_owner=False)
        tenant_db = MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_a_path.as_posix()}", is_active=True, health_status="ok")
        other_tenant_db = MasterTenantDatabase(company_id=2, database_key="other", database_url=f"sqlite:///{self.tenant_b_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, other_company, user, other_user, membership, other_membership, tenant_db, other_tenant_db])
        db.commit()

        self.assertIsNone(load_tenant_context(FakeRequest(session={"membership_id": 2, "user_id": 1, "company_id": 2, "company_slug": "other"}), db))
        self.assertIsNone(load_tenant_context(FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 2, "company_slug": "other"}), db))
        self.assertIsNone(load_tenant_context(FakeRequest(session={"membership_id": 2, "user_id": 2, "company_id": 2, "company_slug": "other"}), db))
        db.close()

    def test_get_tenant_db_keeps_same_ids_isolated_per_company(self):
        db = self.MasterSession()
        company_a = MasterCompany(id=1, name="Demo A", slug="demo-a", active=True)
        company_b = MasterCompany(id=2, name="Demo B", slug="demo-b", active=True)
        user = MasterUser(id=1, email="multi@example.com", full_name="Multi", password_hash=hash_password("admin123"), is_active=True)
        membership_a = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=False)
        membership_b = CompanyMembership(id=2, user_id=1, company_id=2, role_key="Administrador", is_active=True, is_owner=False)
        tenant_a = MasterTenantDatabase(company_id=1, database_key="demo-a", database_url=f"sqlite:///{self.tenant_a_path.as_posix()}", is_active=True, health_status="ok")
        tenant_b = MasterTenantDatabase(company_id=2, database_key="demo-b", database_url=f"sqlite:///{self.tenant_b_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company_a, company_b, user, membership_a, membership_b, tenant_a, tenant_b])
        db.commit()

        self._seed_tenant_customer(self.TenantASession, customer_id=1, company_id=1, code="A-1", name="Cliente A")
        self._seed_tenant_customer(self.TenantBSession, customer_id=1, company_id=2, code="B-1", name="Cliente B")

        request_a = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo-a"})
        request_b = FakeRequest(session={"membership_id": 2, "user_id": 1, "company_id": 2, "company_slug": "demo-b"})

        tenant_a_db = next(get_tenant_db(request_a, db))
        tenant_b_db = next(get_tenant_db(request_b, db))
        try:
            customer_a = tenant_a_db.get(Customer, 1)
            customer_b = tenant_b_db.get(Customer, 1)
            self.assertEqual(customer_a.commercial_name, "Cliente A")
            self.assertEqual(customer_b.commercial_name, "Cliente B")
        finally:
            tenant_a_db.close()
            tenant_b_db.close()
            db.close()

    def test_get_tenant_db_reports_error_when_authenticated_tenant_db_is_missing(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        user = MasterUser(id=1, email="admin@example.com", full_name="Admin", password_hash=hash_password("admin123"), is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=False)
        db.add_all([company, user, membership])
        db.commit()

        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo"})
        with self.assertRaises(HTTPException) as ctx:
            next(get_tenant_db(request, db))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(request.scope["session"]["membership_id"], 1)
        db.close()

    def test_get_tenant_db_bootstraps_missing_schema_before_yielding(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        user = MasterUser(id=1, email="admin@example.com", full_name="Admin", password_hash=hash_password("admin123"), is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=False)
        tenant_db = MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_a_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, user, membership, tenant_db])
        db.commit()

        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo"})
        tenant_session = next(get_tenant_db(request, db))
        try:
            self.assertIsNotNone(tenant_session.get_bind())
            self.assertTrue(tenant_session.query(ScoringSettings).filter(ScoringSettings.company_id == 1).count() >= 0)
        finally:
            tenant_session.close()
            db.close()

    def test_get_tenant_db_bootstraps_schema_once_per_database(self):
        db = self.MasterSession()
        company = MasterCompany(id=1, name="Demo", slug="demo", active=True)
        user = MasterUser(id=1, email="admin@example.com", full_name="Admin", password_hash=hash_password("admin123"), is_active=True)
        membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=False)
        tenant_db = MasterTenantDatabase(company_id=1, database_key="demo", database_url=f"sqlite:///{self.tenant_a_path.as_posix()}", is_active=True, health_status="ok")
        db.add_all([company, user, membership, tenant_db])
        db.commit()

        request = FakeRequest(session={"membership_id": 1, "user_id": 1, "company_id": 1, "company_slug": "demo"})
        with patch("app.tenancy.database.ensure_tenant_schema", wraps=ensure_tenant_schema) as mocked_ensure:
            tenant_session_1 = next(get_tenant_db(request, db))
            tenant_session_1.close()
            tenant_session_2 = next(get_tenant_db(request, db))
            tenant_session_2.close()
        self.assertEqual(mocked_ensure.call_count, 1)
        db.close()

    def test_tenant_admin_is_not_master_admin(self):
        tenant_admin = TenantUser(
            id=1,
            email="admin@example.com",
            name="Tenant Admin",
            is_active=True,
            company_id=1,
            company_name="Demo",
            company_slug="demo",
            role=TenantRole(name="Administrador", permissions=""),
            membership_id=1,
        )
        superadmin = TenantUser(
            id=2,
            email="super@example.com",
            name="Platform Admin",
            is_active=True,
            company_id=1,
            company_name="Demo",
            company_slug="demo",
            role=TenantRole(name="Superadmin", permissions=""),
            membership_id=2,
        )

        with self.assertRaises(HTTPException) as ctx:
            require_master_admin(tenant_admin)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(require_master_admin(superadmin).role.name, "Superadmin")


if __name__ == "__main__":
    unittest.main()
