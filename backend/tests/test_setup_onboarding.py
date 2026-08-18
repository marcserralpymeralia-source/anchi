from __future__ import annotations

import os
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core import lifespan as lifespan_module  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.encryption import decrypt_secret  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import ChannelSetting, Company, Customer, EmailSettings, InputChannel, LLMSettings, Product, Role, User  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.migrations import upgrade_master_schema  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.migrations.helpers import ensure_columns  # noqa: E402
from app.settings.branding import get_or_create_branding  # noqa: E402
from app.settings.service import get_or_create_settings  # noqa: E402
from app.setup.service import get_setup_status  # noqa: E402
from app.tenancy.database import clear_tenant_schema_cache, ensure_tenant_schema  # noqa: E402
from app.whatsapp.service import whatsapp_config  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402


class SetupFixture:
    def __init__(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.master_url = f"sqlite:///{(root / 'master.db').as_posix()}"
        self.tenant_url = f"sqlite:///{(root / 'tenant.db').as_posix()}"
        self.previous_env = {key: os.environ.get(key) for key in ["APP_ENV", "MASTER_DATABASE_URL", "TENANT_DATABASE_URL", "DATABASE_URL", "DEFAULT_ADMIN_EMAIL", "DEFAULT_ADMIN_PASSWORD"]}
        os.environ["APP_ENV"] = "test"
        os.environ["MASTER_DATABASE_URL"] = self.master_url
        os.environ["TENANT_DATABASE_URL"] = self.tenant_url
        os.environ["DATABASE_URL"] = self.tenant_url
        os.environ["DEFAULT_ADMIN_EMAIL"] = "admin@setup.local"
        os.environ["DEFAULT_ADMIN_PASSWORD"] = "setup-password"
        get_settings.cache_clear()
        clear_tenant_schema_cache()
        self.master_engine = create_engine(self.master_url, connect_args={"check_same_thread": False})
        self.tenant_engine = create_engine(self.tenant_url, connect_args={"check_same_thread": False})
        MasterBase.metadata.create_all(self.master_engine)
        upgrade_master_schema(self.master_engine, baseline=True)
        ensure_columns(
            self.master_engine,
            "email_sync_state",
            {
                "source_provider": "VARCHAR(50)",
                "source_host": "VARCHAR(255)",
                "source_username": "VARCHAR(255)",
                "source_connected_email": "VARCHAR(255)",
            },
        )
        Base.metadata.create_all(self.tenant_engine)
        ensure_tenant_schema(self.tenant_url, company_id=1)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)
        with self.MasterSession() as db:
            company = MasterCompany(id=1, name="Setup Demo", slug="setup", legal_name="Setup Demo", active=True)
            user = MasterUser(id=1, email="admin@setup.local", full_name="Admin Setup", password_hash=hash_password("setup-password"), is_active=True)
            membership = CompanyMembership(id=1, user_id=1, company_id=1, role_key="Administrador", is_active=True, is_owner=True)
            tenant = MasterTenantDatabase(company_id=1, database_key="setup", database_url=self.tenant_url, database_type="sqlite", is_active=True)
            db.add_all([company, user, membership, tenant])
            db.commit()
        with self.TenantSession() as db:
            company = Company(id=1, name="", legal_name=None, country=None, language="es", timezone="Europe/Madrid", active=True)
            role = Role(id=1, company_id=1, name="Administrador", permissions="")
            user = User(id=1, company_id=1, role_id=1, email="admin@setup.local", name="Admin Setup", password_hash=hash_password("setup-password"), is_active=True)
            db.add_all([company, role, user])
            db.commit()

    def client(self):
        master_database_module = importlib.import_module("app.master.database")
        operational_database_module = importlib.import_module("app.db.database")
        lifespan_module_local = importlib.import_module("app.core.lifespan")
        middleware_module = importlib.import_module("app.core.middleware")
        jobs_worker_module = importlib.import_module("app.workers.jobs_worker")
        tenancy_database_module = importlib.import_module("app.tenancy.database")
        previous = {
            "master_engine": master_database_module.engine,
            "master_session": master_database_module.MasterSessionLocal,
            "operational_engine": operational_database_module.engine,
            "operational_session": operational_database_module.SessionLocal,
            "lifespan_master_session": lifespan_module_local.MasterSessionLocal,
            "middleware_master_session": middleware_module.MasterSessionLocal,
            "jobs_worker_master_session": jobs_worker_module.MasterSessionLocal,
        }
        master_database_module.engine = self.master_engine
        master_database_module.MasterSessionLocal = self.MasterSession
        operational_database_module.engine = self.tenant_engine
        operational_database_module.SessionLocal = self.TenantSession
        lifespan_module_local.MasterSessionLocal = self.MasterSession
        middleware_module.MasterSessionLocal = self.MasterSession
        jobs_worker_module.MasterSessionLocal = self.MasterSession
        tenancy_database_module.get_tenant_engine.cache_clear()
        app = create_app()
        patches = (
            patch.object(lifespan_module, "start_email_sync_worker", lambda: None),
            patch.object(lifespan_module, "start_job_worker", lambda: None),
        )
        for item in patches:
            item.__enter__()
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()

        def cleanup():
            client.__exit__(None, None, None)
            for item in reversed(patches):
                item.__exit__(None, None, None)
            master_database_module.engine = previous["master_engine"]
            master_database_module.MasterSessionLocal = previous["master_session"]
            operational_database_module.engine = previous["operational_engine"]
            operational_database_module.SessionLocal = previous["operational_session"]
            lifespan_module_local.MasterSessionLocal = previous["lifespan_master_session"]
            middleware_module.MasterSessionLocal = previous["middleware_master_session"]
            jobs_worker_module.MasterSessionLocal = previous["jobs_worker_master_session"]
            tenancy_database_module.get_tenant_engine.cache_clear()

        return client, cleanup

    def cleanup(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        clear_tenant_schema_cache()


class SetupOnboardingTests(unittest.TestCase):
    def _login(self, client):
        return client.post("/login", data={"email": "admin@setup.local", "password": "setup-password"}, follow_redirects=False)

    def test_setup_status_operational_requires_company_channel_products_customers_and_openai(self):
        fixture = SetupFixture()
        try:
            with fixture.TenantSession() as db:
                status = get_setup_status(db, 1)
                self.assertFalse(status.is_operational)
                company = db.get(Company, 1)
                company.name = "Setup Demo"
                company.legal_name = "Setup Demo SL"
                company.country = "España"
                company.language = "es"
                company.timezone = "Europe/Madrid"
                branding = get_or_create_branding(db, 1)
                branding.app_name = "Setup Demo"
                branding.company_name = "Setup Demo"
                email = get_or_create_settings(db, EmailSettings, 1)
                email.imap_host = "imap.example.com"
                email.imap_username = "pedidos@example.com"
                email.imap_password_encrypted = "encrypted"
                db.add(InputChannel(company_id=1, key="email", name="Email", channel_type="email", is_active=True))
                db.add(Product(company_id=1, reference="P001", name="Producto demo"))
                db.add(Customer(company_id=1, code="C001", fiscal_name="Cliente Demo SL"))
                llm = get_or_create_settings(db, LLMSettings, 1)
                llm.provider = "openai"
                llm.api_key_encrypted = "encrypted"
                db.commit()
                with patch("app.setup.service.decrypt_secret", return_value="plain"):
                    status = get_setup_status(db, 1)
                self.assertTrue(status.is_operational)
                self.assertTrue(status.email_connected)
        finally:
            fixture.cleanup()

    def test_login_redirects_incomplete_tenant_to_setup(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            response = self._login(client)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/setup")
            setup_page = client.get("/setup/company")
            self.assertEqual(setup_page.status_code, 200)
            self.assertIn("Pedidos pendientes", setup_page.text)
            self.assertIn("Configura Anchi", setup_page.text)
            self.assertIn("Configuración completada", setup_page.text)
        finally:
            cleanup()
            fixture.cleanup()

    def test_incomplete_setup_keeps_authenticated_user_inside_app(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            response = client.get("/inicio")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Anchi todavía no está lista", response.text)
            self.assertIn("Continuar configuración", response.text)
            self.assertIn("Pedidos pendientes", response.text)
            self.assertNotIn("/login", response.headers.get("location", ""))
        finally:
            cleanup()
            fixture.cleanup()

    def test_complete_email_onboarding_flow_with_imports_and_openai(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            company = client.post(
                "/setup/company",
                data={"legal_name": "Setup Demo SL", "commercial_name": "Setup Demo", "country": "España", "language": "es", "timezone": "Europe/Madrid", "primary_color": "#157F6E"},
                follow_redirects=False,
            )
            self.assertEqual(company.headers["location"], "/setup/channels")
            with patch("app.setup.routes.test_imap_connection", return_value={"ok": True, "message": "Correo conectado correctamente"}):
                email = client.post(
                    "/setup/email",
                    data={"connected_email": "pedidos@example.com", "imap_host": "imap.example.com", "imap_port": "993", "imap_username": "pedidos@example.com", "imap_password": "app-password", "imap_use_ssl": "on", "inbox_folder": "INBOX"},
                    follow_redirects=False,
                )
            self.assertEqual(email.status_code, 303)
            products_csv = b"SKU,Descripcion producto,Unidad,Precio venta\nP001,Producto Demo,uds,12.5\n"
            product_preview = client.post("/setup/products/preview", files={"file": ("products.csv", products_csv, "text/csv")})
            self.assertEqual(product_preview.status_code, 200)
            product_import = client.post("/setup/products/import", data={"token": self._extract(product_preview.text, 'name="token" value="'), "filename": "products.csv", "mapping:SKU": "reference", "mapping:Descripcion producto": "name", "mapping:Unidad": "sale_unit", "mapping:Precio venta": "sale_price"}, follow_redirects=False)
            self.assertEqual(product_import.headers["location"].split("?")[0], "/setup/customers")
            customers_csv = b"Codigo cliente,Razon social,Email principal,Telefono\nC001,Cliente Demo SL,compras@example.com,600000000\n"
            customer_preview = client.post("/setup/customers/preview", files={"file": ("customers.csv", customers_csv, "text/csv")})
            self.assertEqual(customer_preview.status_code, 200)
            customer_import = client.post("/setup/customers/import", data={"token": self._extract(customer_preview.text, 'name="token" value="'), "filename": "customers.csv", "mapping:Codigo cliente": "code", "mapping:Razon social": "fiscal_name", "mapping:Email principal": "primary_email", "mapping:Telefono": "phone"}, follow_redirects=False)
            self.assertEqual(customer_import.headers["location"].split("?")[0], "/setup/customer-knowledge")
            skip = client.post("/setup/customer-knowledge/skip", follow_redirects=False)
            self.assertEqual(skip.headers["location"], "/setup/openai")
            openai = client.post("/setup/openai", data={"api_key": "sk-test-onboarding-key"}, follow_redirects=False)
            self.assertEqual(openai.headers["location"], "/setup/complete")
            complete = client.get("/setup/complete")
            self.assertEqual(complete.status_code, 200)
            self.assertIn("Anchi está lista", complete.text)
            with fixture.TenantSession() as db:
                status = get_setup_status(db, 1)
                self.assertTrue(status.is_operational)
                llm = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
                self.assertNotEqual(llm.api_key_encrypted, "sk-test-onboarding-key")
                self.assertEqual(decrypt_secret(llm.api_key_encrypted), "sk-test-onboarding-key")
        finally:
            cleanup()
            fixture.cleanup()

    def test_whatsapp_can_be_the_only_input_channel_and_secrets_are_redacted(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            client.post("/setup/company", data={"legal_name": "Setup Demo SL", "commercial_name": "Setup Demo", "country": "España", "language": "es", "timezone": "Europe/Madrid", "primary_color": "#157F6E"}, follow_redirects=False)
            response = client.post(
                "/setup/whatsapp",
                data={"phone_number_id": "1234567890", "business_account_id": "999", "access_token": "EAAG-token-demo", "app_secret": "secret-demo-value", "verify_token": "verify-demo-value"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            with fixture.TenantSession() as db:
                config = whatsapp_config(db, 1)
                self.assertTrue(config.enabled)
                self.assertEqual(config.access_token, "EAAG-token-demo")
                stored = db.scalars(select(ChannelSetting).where(ChannelSetting.company_id == 1, ChannelSetting.key == "access_token")).first()
                self.assertNotEqual(stored.value, "EAAG-token-demo")
                db.add(Product(company_id=1, reference="P001", name="Producto demo"))
                db.add(Customer(company_id=1, code="C001", fiscal_name="Cliente Demo SL"))
                llm = get_or_create_settings(db, LLMSettings, 1)
                llm.provider = "openai"
                llm.api_key_encrypted = "encrypted"
                db.commit()
                with patch("app.setup.service.decrypt_secret", return_value="plain"):
                    self.assertTrue(get_setup_status(db, 1).is_operational)
        finally:
            cleanup()
            fixture.cleanup()

    def test_settings_is_simple_configuration_summary(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            response = client.get("/settings")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Empresa", response.text)
            self.assertIn("OpenAI", response.text)
            self.assertIn("Información adicional", response.text)
        finally:
            cleanup()
            fixture.cleanup()

    def _extract(self, html: str, prefix: str) -> str:
        start = html.index(prefix) + len(prefix)
        end = html.index('"', start)
        return html[start:end]


if __name__ == "__main__":
    unittest.main()
