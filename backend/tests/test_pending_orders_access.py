from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core import lifespan as lifespan_module  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import Company, Email, Order, utcnow  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from scripts.performance_data import build_performance_fixture, temporary_performance_environment  # noqa: E402


def _session_factory(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


class PendingOrdersAccessTests(unittest.TestCase):
    def _client_for(self, fixture):
        context = temporary_performance_environment(fixture)
        context.__enter__()
        app = create_app()
        lifespan_patches = (
            patch.object(lifespan_module, "start_email_sync_worker", lambda: None),
            patch.object(lifespan_module, "start_job_worker", lambda: None),
        )
        for item in lifespan_patches:
            item.__enter__()
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()

        def cleanup():
            client.__exit__(None, None, None)
            for item in reversed(lifespan_patches):
                item.__exit__(None, None, None)
            context.__exit__(None, None, None)

        return client, cleanup

    def test_unauthenticated_pending_orders_redirects_to_login_with_next(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            response = client.get("/inicio", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login?next=%2Finicio")
        finally:
            cleanup()
            fixture.cleanup()

    def test_login_with_pending_orders_destination_sets_cookie_and_returns_to_pending_orders(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            login_response = client.post(
                "/login",
                data={"email": fixture.admin_email, "password": fixture.admin_password, "next": "/inicio"},
                follow_redirects=False,
            )
            self.assertEqual(login_response.status_code, 303)
            self.assertEqual(login_response.headers["location"], "/inicio")
            self.assertIn(f"{get_settings().session_cookie}=", login_response.headers.get("set-cookie", ""))

            pending_response = client.get("/inicio", follow_redirects=False)
            self.assertEqual(pending_response.status_code, 200)
            self.assertIn("Pedidos pendientes", pending_response.text)
        finally:
            cleanup()
            fixture.cleanup()

    def test_authenticated_pending_orders_does_not_redirect_to_login_or_inicio(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            client.post("/login", data={"email": fixture.admin_email, "password": fixture.admin_password}, follow_redirects=False)
            response = client.get("/inicio", follow_redirects=False)

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("location", response.headers)
            self.assertIn("Pedidos pendientes", response.text)
        finally:
            cleanup()
            fixture.cleanup()

    def test_pending_orders_menu_uses_registered_relative_route(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            app_route_names = {route.name for route in client.app.routes}
            self.assertIn("dashboard", app_route_names)

            client.post("/login", data={"email": fixture.admin_email, "password": fixture.admin_password}, follow_redirects=False)
            response = client.get("/inicio", follow_redirects=False)

            self.assertEqual(response.status_code, 200)
            self.assertIn('href="http://testserver/inicio"', response.text)
            self.assertNotIn('href="#"', response.text)
            self.assertNotIn('href="http://127.0.0.1:8000/inicio"', response.text)
        finally:
            cleanup()
            fixture.cleanup()

    def test_next_rejects_external_url(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            response = client.post(
                "/login",
                data={"email": fixture.admin_email, "password": fixture.admin_password, "next": "https://example.com/pwn"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/inicio")
        finally:
            cleanup()
            fixture.cleanup()

    def test_inactive_membership_is_not_treated_as_anonymous(self):
        fixture = build_performance_fixture("small")
        master_engine, MasterSession = _session_factory(fixture.master_database_url)
        client, cleanup = self._client_for(fixture)
        try:
            login_response = client.post("/login", data={"email": fixture.admin_email, "password": fixture.admin_password}, follow_redirects=False)
            self.assertEqual(login_response.status_code, 303)
            with MasterSession() as db:
                membership = db.get(CompanyMembership, 1)
                membership.is_active = False
                db.commit()

            response = client.get("/inicio", follow_redirects=False)
            self.assertEqual(response.status_code, 403)
            self.assertNotEqual(response.headers.get("location"), "/login")
        finally:
            cleanup()
            master_engine.dispose()
            fixture.cleanup()

    def test_pending_orders_are_scoped_by_authenticated_tenant(self):
        fixture = build_performance_fixture("small")
        master_engine, MasterSession = _session_factory(fixture.master_database_url)
        tenant_engine, TenantSession = _session_factory(fixture.tenant_database_url)
        client, cleanup = self._client_for(fixture)
        try:
            with MasterSession() as db:
                db.add(MasterCompany(id=2, name="Tenant B", slug="tenant-b", active=True))
                db.add(MasterUser(id=2, email="admin@tenant-b.local", full_name="Admin B", password_hash=hash_password("admin123"), is_active=True))
                db.add(CompanyMembership(id=2, user_id=2, company_id=2, role_key="Administrador", is_active=True, is_owner=True))
                db.add(MasterTenantDatabase(company_id=2, database_key="tenant-b", database_url=fixture.tenant_database_url, is_active=True, health_status="ok"))
                db.commit()
            with TenantSession() as db:
                db.add(Company(id=2, name="Tenant B", active=True))
                email_a = Email(
                    company_id=1,
                    sender="a@example.com",
                    subject="Pedido visible tenant A",
                    body="A",
                    status="processed",
                    agent_status="processed_doubtful",
                    detected_type="pedido",
                    received_at=utcnow(),
                )
                email_b = Email(
                    company_id=2,
                    sender="b@example.com",
                    subject="Pedido visible tenant B",
                    body="B",
                    status="processed",
                    agent_status="processed_doubtful",
                    detected_type="pedido",
                    received_at=utcnow(),
                )
                db.add_all([email_a, email_b])
                db.flush()
                db.add(
                    Order(
                        company_id=1,
                        email_id=email_a.id,
                        customer_detected_name="Pedido visible tenant A",
                        score=65,
                        status="pending_review",
                        created_at=utcnow(),
                    )
                )
                db.add(
                    Order(
                        company_id=2,
                        email_id=email_b.id,
                        customer_detected_name="Pedido visible tenant B",
                        score=65,
                        status="pending_review",
                        created_at=utcnow(),
                    )
                )
                db.commit()

            client.post("/login", data={"email": fixture.admin_email, "password": fixture.admin_password}, follow_redirects=False)
            tenant_a = client.get("/inicio", follow_redirects=False)
            self.assertEqual(tenant_a.status_code, 200)
            self.assertIn("Pedido visible tenant A", tenant_a.text)
            self.assertNotIn("Pedido visible tenant B", tenant_a.text)

            client.post("/logout", follow_redirects=False)
            client.post("/login", data={"email": "admin@tenant-b.local", "password": "admin123"}, follow_redirects=False)
            tenant_b = client.get("/inicio", follow_redirects=False)
            self.assertEqual(tenant_b.status_code, 200)
            self.assertIn("Pedido visible tenant B", tenant_b.text)
            self.assertNotIn("Pedido visible tenant A", tenant_b.text)
        finally:
            cleanup()
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_session_cookie_configuration_is_consistent(self):
        get_settings.cache_clear()
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=False):
            app = create_app()
            session_middleware = next(middleware for middleware in app.user_middleware if middleware.cls.__name__ == "SessionMiddleware")
            self.assertEqual(session_middleware.kwargs["session_cookie"], get_settings().session_cookie)
            self.assertFalse(session_middleware.kwargs["https_only"])
            self.assertEqual(session_middleware.kwargs["same_site"], "lax")
        get_settings.cache_clear()

        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "MASTER_DATABASE_URL": "postgresql://user:pass@localhost/master",
                "TENANT_DB_MODE": "external",
                "TENANT_DATABASE_URL": "postgresql://user:pass@localhost/tenant",
                "ALLOWED_HOSTS": "anchi.example.com",
                "SECRET_KEY": "production-secret-key-with-enough-length-000000",
                "ENCRYPTION_KEY": "CKHCB4gFGn7kJVxowWH2pEdPucfPaZugSsMgoJU6eNE=",
                "DEFAULT_ADMIN_EMAIL": "ops@example.com",
                "DEFAULT_ADMIN_PASSWORD": "ProductionPassword2026!",
                "PERFORMANCE_PROFILING_ENABLED": "false",
                "ENABLE_PERFORMANCE_PROFILING": "false",
            },
            clear=False,
        ):
            app = create_app()
            session_middleware = next(middleware for middleware in app.user_middleware if middleware.cls.__name__ == "SessionMiddleware")
            self.assertTrue(session_middleware.kwargs["https_only"])
        get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
