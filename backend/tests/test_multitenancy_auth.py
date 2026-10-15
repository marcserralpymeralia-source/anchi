from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("SECRET_KEY", "multi-tenant-auth-test-secret-key-0123456789")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import middleware as middleware_module  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Company, Email, Role, User  # noqa: E402
from app.master.database import MasterBase, get_master_db  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser, PlatformAuditLog  # noqa: E402
from app.master.provisioning import synchronize_tenant_actors  # noqa: E402
from app.tenancy import database as tenancy_database_module  # noqa: E402
from app.tenancy.database import clear_tenant_engine_cache, clear_tenant_schema_cache, ensure_tenant_schema  # noqa: E402


class MultiTenancyAuthenticationTests(unittest.TestCase):
    """End-to-end coverage for the multi-company browser session flow."""

    def setUp(self):
        clear_tenant_engine_cache()
        clear_tenant_schema_cache()
        middleware_module._BRANDING_CACHE.clear()

        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.master_engine = create_engine(f"sqlite:///{(root / 'master.sqlite').as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_engines = {
            1: create_engine(f"sqlite:///{(root / 'tenant-alpha.sqlite').as_posix()}", connect_args={"check_same_thread": False}),
            2: create_engine(f"sqlite:///{(root / 'tenant-beta.sqlite').as_posix()}", connect_args={"check_same_thread": False}),
        }
        MasterBase.metadata.create_all(self.master_engine)
        for engine in self.tenant_engines.values():
            Base.metadata.create_all(engine)

        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self._seed_master()
        self._seed_tenants()

    def tearDown(self):
        clear_tenant_engine_cache()
        clear_tenant_schema_cache()
        middleware_module._BRANDING_CACHE.clear()
        for engine in self.tenant_engines.values():
            engine.dispose()
        self.master_engine.dispose()
        self.tempdir.cleanup()

    def _seed_master(self):
        root = Path(self.tempdir.name)
        with self.MasterSession() as db:
            db.add_all(
                [
                    MasterCompany(id=1, name="Empresa Alfa", slug="empresa-alfa", active=True),
                    MasterCompany(id=2, name="Empresa Beta", slug="empresa-beta", active=True),
                    MasterUser(
                        id=1,
                        email="multiempresa@example.test",
                        full_name="Usuario Multiempresa",
                        password_hash=hash_password("test-password"),
                        is_active=True,
                        session_version=1,
                    ),
                    CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True),
                    CompanyMembership(id=2, user_id=1, company_id=2, role_key="Administrador", is_active=True, is_owner=False),
                    MasterTenantDatabase(
                        company_id=1,
                        database_key="test-empresa-alfa",
                        database_url=f"sqlite:///{(root / 'tenant-alpha.sqlite').as_posix()}",
                        database_type="sqlite",
                        is_active=True,
                        health_status="ok",
                    ),
                    MasterTenantDatabase(
                        company_id=2,
                        database_key="test-empresa-beta",
                        database_url=f"sqlite:///{(root / 'tenant-beta.sqlite').as_posix()}",
                        database_type="sqlite",
                        is_active=True,
                        health_status="ok",
                    ),
                ]
            )
            db.commit()

    def _seed_tenants(self):
        with sessionmaker(bind=self.tenant_engines[1], autoflush=False, autocommit=False)() as db:
            db.add(Company(id=1, name="Empresa Alfa", active=True))
            db.commit()

        with sessionmaker(bind=self.tenant_engines[2], autoflush=False, autocommit=False)() as db:
            db.add(Company(id=2, name="Empresa Beta", active=True))
            db.add(
                Email(
                    id=1,
                    company_id=2,
                    sender="beta@example.test",
                    subject="Mensaje exclusivo de Beta",
                    body="Dato sintético del tenant beta.",
                    received_at=datetime.now(timezone.utc),
                    status="pending",
                    agent_status="not_processed",
                )
            )
            db.commit()

        # Navigation now performs a read-only schema check. The fixture must
        # model the real startup/provisioning path explicitly instead of
        # relying on a request to mutate the tenant database.
        for company_id, engine in self.tenant_engines.items():
            ensure_tenant_schema(f"sqlite:///{engine.url.database}", company_id=company_id)
        with self.MasterSession() as db:
            for company_id in self.tenant_engines:
                tenant = db.scalar(
                    select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == company_id)
                )
                synchronize_tenant_actors(db, tenant)

    def _client(self):
        app = create_app()

        def override_master_db():
            db = self.MasterSession()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_master_db] = override_master_db
        return app, TestClient(app, raise_server_exceptions=False)

    def _login(self, client: TestClient, next_url: str = "/dashboard/summary"):
        return client.post(
            "/login",
            data={
                "email": "multiempresa@example.test",
                "password": "test-password",
                "next": next_url,
            },
            follow_redirects=False,
        )

    def test_user_with_two_memberships_is_sent_to_company_selection_after_login(self):
        app, client = self._client()
        try:
            with patch.object(middleware_module, "MasterSessionLocal", self.MasterSession), patch.object(
                tenancy_database_module, "ensure_tenant_schema_once", return_value={}
            ):
                response = self._login(client)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/select-company")

                selection = client.get("/select-company", follow_redirects=False)

            self.assertEqual(selection.status_code, 200)
            self.assertIn("Empresa Alfa", selection.text)
            self.assertIn("Empresa Beta", selection.text)
        finally:
            client.close()

    def test_configured_platform_owner_logs_into_superadmin_without_tenant_context(self):
        with self.MasterSession() as db:
            db.add(
                MasterUser(
                    id=2,
                    email="admin@anchi.local",
                    full_name="Anchi Owner",
                    password_hash=hash_password("platform-password"),
                    is_active=True,
                    platform_role_key="superadmin",
                )
            )
            db.commit()

        app, client = self._client()
        try:
            with patch.object(middleware_module, "MasterSessionLocal", self.MasterSession), patch.object(
                tenancy_database_module, "ensure_tenant_schema_once", return_value={}
            ):
                response = client.post(
                    "/login",
                    data={"email": "admin@anchi.local", "password": "platform-password"},
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/superadmin")
                self.assertEqual(client.get("/superadmin", follow_redirects=False).status_code, 200)
                self.assertNotEqual(client.get("/orders", follow_redirects=False).status_code, 200)
        finally:
            client.close()

    def test_platform_owner_entering_company_can_open_configuration_and_email_channel(self):
        with self.MasterSession() as db:
            db.add(
                MasterUser(
                    id=2,
                    email="admin@anchi.local",
                    full_name="Anchi Owner",
                    password_hash=hash_password("platform-password"),
                    is_active=True,
                    platform_role_key="superadmin",
                )
            )
            db.commit()

        app, client = self._client()
        try:
            with patch.dict(os.environ, {"PLATFORM_ADMIN_EMAIL": "admin@anchi.local"}), patch.object(
                middleware_module, "MasterSessionLocal", self.MasterSession
            ), patch.object(tenancy_database_module, "ensure_tenant_schema_once", return_value={}):
                login = client.post(
                    "/login",
                    data={"email": "admin@anchi.local", "password": "platform-password"},
                    follow_redirects=False,
                )
                self.assertEqual(login.headers["location"], "/superadmin")
                entered = client.post("/superadmin/companies/1/enter", follow_redirects=False)
                settings = client.get("/settings", follow_redirects=False)
                channels = client.get("/settings/channels", follow_redirects=False)
                users = client.get("/users", follow_redirects=False)
                superadmin = client.get("/superadmin", follow_redirects=False)

            self.assertEqual(entered.headers["location"], "/")
            self.assertEqual(settings.status_code, 200)
            self.assertIn('title="Configuración"', settings.text)
            self.assertEqual(channels.status_code, 200)
            self.assertIn("Email", channels.text)
            self.assertIn("WhatsApp", channels.text)
            self.assertEqual(users.status_code, 200)
            self.assertNotIn("Soporte de plataforma", users.text)
            self.assertEqual(superadmin.status_code, 200)
            with sessionmaker(bind=self.tenant_engines[1], autoflush=False, autocommit=False)() as db:
                support_actor = db.scalar(
                    select(User).where(
                        User.company_id == 1,
                        User.actor_type == "platform_support",
                    )
                )
                self.assertIsNotNone(support_actor)
                self.assertIsNone(support_actor.master_user_id)
                support_actor_id = support_actor.id
                support_actor_role_id = support_actor.role_id
            update_support_actor = client.post(
                f"/users/{support_actor_id}",
                data={"name": "Cambio no permitido", "role_id": support_actor_role_id, "is_active": "true"},
                follow_redirects=False,
            )
            self.assertEqual(update_support_actor.status_code, 303)
            with sessionmaker(bind=self.tenant_engines[1], autoflush=False, autocommit=False)() as db:
                support_actor = db.get(User, support_actor_id)
                self.assertEqual(support_actor.name, "Soporte de plataforma")
            with self.MasterSession() as db:
                support_membership = db.scalar(
                    select(CompanyMembership).where(
                        CompanyMembership.user_id == 2,
                        CompanyMembership.company_id == 1,
                    )
                )
                support_audit = db.scalar(
                    select(PlatformAuditLog).where(
                        PlatformAuditLog.action == "company.support_access",
                        PlatformAuditLog.company_id == 1,
                        PlatformAuditLog.actor_user_id == 2,
                    )
                )
            self.assertIsNone(support_membership)
            self.assertIsNotNone(support_audit)
        finally:
            client.close()

    def test_legacy_platform_membership_is_hidden_from_company_users(self):
        with self.MasterSession() as db:
            platform_owner = MasterUser(
                id=2,
                email="admin@anchi.local",
                full_name="Anchi Owner",
                password_hash=hash_password("platform-password"),
                is_active=True,
                platform_role_key="superadmin",
            )
            db.add(platform_owner)
            db.flush()
            legacy_membership = CompanyMembership(
                user_id=platform_owner.id,
                company_id=1,
                role_key="Solo lectura",
                is_active=True,
                is_owner=False,
            )
            db.add(legacy_membership)
            db.commit()

        with sessionmaker(bind=self.tenant_engines[1], autoflush=False, autocommit=False)() as db:
            admin_role = db.scalar(select(Role).where(Role.company_id == 1, Role.name == "Administrador"))
            db.add(
                User(
                    company_id=1,
                    role_id=admin_role.id,
                    email="admin@anchi.local",
                    name="Legacy platform projection",
                    password_hash=hash_password("legacy-disabled-password"),
                    is_active=True,
                    master_user_id=2,
                    actor_type="human",
                )
            )
            db.commit()

        app, client = self._client()
        try:
            with patch.dict(os.environ, {"PLATFORM_ADMIN_EMAIL": "admin@anchi.local"}), patch.object(
                middleware_module, "MasterSessionLocal", self.MasterSession
            ), patch.object(tenancy_database_module, "ensure_tenant_schema_once", return_value={}):
                login = client.post(
                    "/login",
                    data={"email": "admin@anchi.local", "password": "platform-password"},
                    follow_redirects=False,
                )
                self.assertEqual(login.headers["location"], "/superadmin")
                self.assertEqual(client.post("/superadmin/companies/1/enter", follow_redirects=False).headers["location"], "/")
                users = client.get("/users", follow_redirects=False)

            self.assertEqual(users.status_code, 200)
            self.assertNotIn("Legacy platform projection", users.text)
            with self.MasterSession() as db:
                legacy_membership = db.scalar(
                    select(CompanyMembership).where(
                        CompanyMembership.user_id == 2,
                        CompanyMembership.company_id == 1,
                    )
                )
            self.assertIsNotNone(legacy_membership)
        finally:
            client.close()

    def test_revoking_platform_session_invalidates_temporary_company_access(self):
        with self.MasterSession() as db:
            db.add(
                MasterUser(
                    id=2,
                    email="admin@anchi.local",
                    full_name="Anchi Owner",
                    password_hash=hash_password("platform-password"),
                    is_active=True,
                    platform_role_key="superadmin",
                )
            )
            db.commit()

        app, client = self._client()
        try:
            with patch.dict(os.environ, {"PLATFORM_ADMIN_EMAIL": "admin@anchi.local"}), patch.object(
                middleware_module, "MasterSessionLocal", self.MasterSession
            ), patch.object(tenancy_database_module, "ensure_tenant_schema_once", return_value={}):
                login = client.post(
                    "/login",
                    data={"email": "admin@anchi.local", "password": "platform-password"},
                    follow_redirects=False,
                )
                self.assertEqual(login.headers["location"], "/superadmin")
                self.assertEqual(client.post("/superadmin/companies/1/enter", follow_redirects=False).headers["location"], "/")
                self.assertEqual(client.get("/settings/channels", follow_redirects=False).status_code, 200)
                with self.MasterSession() as db:
                    platform_owner = db.get(MasterUser, 2)
                    platform_owner.session_version += 1
                    db.commit()
                revoked = client.get("/settings/channels", follow_redirects=False)

            self.assertEqual(revoked.status_code, 303)
            self.assertTrue(revoked.headers["location"].startswith("/login"))
        finally:
            client.close()

    def test_selecting_membership_uses_the_selected_tenant_database(self):
        app, client = self._client()
        try:
            with patch.object(middleware_module, "MasterSessionLocal", self.MasterSession), patch.object(
                tenancy_database_module, "ensure_tenant_schema_once", return_value={}
            ):
                self.assertEqual(self._login(client).headers["location"], "/select-company")
                selected = client.post(
                    "/select-company",
                    data={"membership_id": "2", "next": "/dashboard/summary"},
                    follow_redirects=False,
                )
                self.assertEqual(selected.status_code, 303)
                self.assertEqual(selected.headers["location"], "/dashboard/summary")

                summary = client.get("/dashboard/summary", follow_redirects=False)
                settings = client.get("/settings", follow_redirects=False)
                channels = client.get("/settings/channels", follow_redirects=False)
                superadmin = client.get("/superadmin", follow_redirects=False)

            self.assertEqual(summary.status_code, 200)
            self.assertNotIn('class="superadmin-app-link', summary.text)
            self.assertEqual(settings.status_code, 200)
            self.assertIn('title="Configuración"', settings.text)
            self.assertEqual(channels.status_code, 200)
            self.assertIn("Email", channels.text)
            self.assertIn("WhatsApp", channels.text)
            self.assertNotIn('class="superadmin-app-link', channels.text)
            subjects = [item["subject"] for item in summary.json()["latest_items"]]
            self.assertEqual(subjects, ["Mensaje exclusivo de Beta"])
            self.assertEqual(superadmin.status_code, 403)
        finally:
            client.close()

    def test_incrementing_session_version_invalidates_previous_session(self):
        app, client = self._client()
        try:
            with patch.object(middleware_module, "MasterSessionLocal", self.MasterSession), patch.object(
                tenancy_database_module, "ensure_tenant_schema_once", return_value={}
            ):
                self.assertEqual(self._login(client, next_url="/knowledge").headers["location"], "/select-company")
                selected = client.post(
                    "/select-company",
                    data={"membership_id": "1", "next": "/knowledge"},
                    follow_redirects=False,
                )
                self.assertEqual(selected.status_code, 303)
                self.assertEqual(selected.headers["location"], "/knowledge")
                self.assertEqual(client.get("/knowledge", follow_redirects=False).headers["location"], "/customers?view=knowledge")

                with self.MasterSession() as db:
                    user = db.get(MasterUser, 1)
                    user.session_version += 1
                    db.commit()

                invalidated = client.get("/knowledge", follow_redirects=False)

            self.assertEqual(invalidated.status_code, 303)
            self.assertEqual(invalidated.headers["location"], "/login?next=%2Fknowledge")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
