from __future__ import annotations

import os
import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core import lifespan as lifespan_module  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.encryption import decrypt_secret, encrypt_secret  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.agent.model_catalog import DEFAULT_OPENAI_MODEL, LEGACY_OPENAI_MODEL_FALLBACK, resolve_openai_runtime_model  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import ChannelSetting, Company, Customer, EmailSettings, InputChannel, LLMSettings, Product, Role, User  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.migrations import upgrade_master_schema  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from app.migrations.helpers import ensure_columns  # noqa: E402
from app.settings.branding import get_or_create_branding  # noqa: E402
from app.settings.service import get_or_create_settings  # noqa: E402
from app.setup.service import get_setup_status, is_setup_operational  # noqa: E402
from app.tenancy.database import clear_tenant_schema_cache, ensure_tenant_schema, get_tenant_engine  # noqa: E402
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
        get_tenant_engine(self.tenant_url).dispose()
        get_tenant_engine.cache_clear()
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
            tenancy_database_module.get_tenant_engine(self.tenant_url).dispose()
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

    def test_demo_setup_is_operational_without_openai_and_production_still_requires_it(self):
        fixture = SetupFixture()
        try:
            with fixture.TenantSession() as db:
                company = db.get(Company, 1)
                company.name = "Setup Demo"
                company.legal_name = "Setup Demo SL"
                company.country = "España"
                company.language = "es"
                company.timezone = "Europe/Madrid"
                branding = get_or_create_branding(db, 1)
                branding.app_name = "Setup Demo"
                branding.company_name = "Setup Demo"
                db.add(InputChannel(company_id=1, key="demo", name="Demo", channel_type="message", is_active=True))
                db.add(Product(company_id=1, reference="P001", name="Producto demo"))
                db.add(Customer(company_id=1, code="C001", fiscal_name="Cliente Demo SL"))
                db.commit()

                with patch("app.setup.service.get_settings", return_value=SimpleNamespace(environment="demo")):
                    status = get_setup_status(db, 1)
                    self.assertTrue(status.is_operational)
                    self.assertTrue(is_setup_operational(db, 1))
                    self.assertFalse(status.openai_connected)
                    self.assertEqual(next(item["status"] for item in status.steps if item["key"] == "openai"), "Opcional")

                with patch("app.setup.service.get_settings", return_value=SimpleNamespace(environment="production")):
                    status = get_setup_status(db, 1)
                    self.assertFalse(status.is_operational)
                    self.assertFalse(is_setup_operational(db, 1))
                    self.assertEqual(next(item["status"] for item in status.steps if item["key"] == "openai"), "En progreso")
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
            response = client.get("/inicio", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/")
            follow_up = client.get("/", follow_redirects=True)
            self.assertEqual(follow_up.status_code, 200)
            self.assertIn("Anchi todavía no está lista", follow_up.text)
            self.assertIn("Continuar configuración", follow_up.text)
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

            with fixture.TenantSession() as db:
                email_channel = db.scalar(
                    select(InputChannel).where(
                        InputChannel.company_id == 1,
                        InputChannel.key == "email",
                    )
                )
                self.assertIsNotNone(email_channel)
                self.assertTrue(email_channel.is_active)

                whatsapp_channel = db.scalar(
                    select(InputChannel).where(
                        InputChannel.company_id == 1,
                        InputChannel.key == "whatsapp",
                    )
                )
                self.assertTrue(
                    whatsapp_channel is None or not whatsapp_channel.is_active
                )

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

    def test_whatsapp_embedded_signup_replaces_manual_setup_and_can_be_the_only_input_channel(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            client.post("/setup/company", data={"legal_name": "Setup Demo SL", "commercial_name": "Setup Demo", "country": "España", "language": "es", "timezone": "Europe/Madrid", "primary_color": "#157F6E"}, follow_redirects=False)
            page = client.get("/setup/channels")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Iniciar sesión con Meta", page.text)
            self.assertIn('data-testid="whatsapp-embedded-signup-button"', page.text)
            self.assertIn('data-meta-feature-type="whatsapp_business_app_onboarding"', page.text)
            self.assertIn("sin desconectarlo", page.text)
            self.assertNotIn('name="phone_number_id"', page.text)
            self.assertNotIn('name="access_token"', page.text)
            self.assertNotIn('name="app_secret"', page.text)
            self.assertNotIn('name="verify_token"', page.text)
            legacy_response = client.post("/setup/whatsapp", data={}, follow_redirects=False)
            self.assertIn(legacy_response.status_code, {404, 405})

            state = self._extract(page.text, 'data-signup-state="')
            completed = SimpleNamespace(
                business_account_id="12345678901",
                phone_number_id="10987654321",
                display_phone_number="+34 600 000 000",
                verified_name="Anchi Demo",
                onboarding_mode="coexistence",
                is_on_biz_app=True,
            )
            with patch(
                "app.settings.channels_routes.complete_embedded_signup",
                new=AsyncMock(return_value=completed),
            ) as complete_mock:
                response = client.post(
                    "/settings/channels/whatsapp/embedded-signup/complete",
                    json={
                        "code": "temporary-auth-code",
                        "waba_id": "12345678901",
                        "phone_number_id": "",
                        "business_id": "11223344556",
                        "onboarding_mode": "coexistence",
                        "state": state,
                        "return_to": "setup",
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["ok"])
            self.assertEqual(response.json()["redirect_url"], "/setup/channels?whatsapp=connected")
            complete_mock.assert_awaited_once()
            self.assertEqual(complete_mock.await_args.kwargs["onboarding_mode"], "coexistence")

            with fixture.TenantSession() as db:
                channel = db.scalar(
                    select(InputChannel).where(
                        InputChannel.company_id == 1,
                        InputChannel.key == "whatsapp",
                    )
                )
                if channel is None:
                    channel = InputChannel(
                        company_id=1,
                        key="whatsapp",
                        name="WhatsApp",
                        channel_type="message",
                    )
                    db.add(channel)
                    db.flush()
                channel.is_active = True
                values = {
                    "enabled": "true",
                    "provider": "meta",
                    "phone_number_id": "10987654321",
                    "business_account_id": "12345678901",
                    "access_token": encrypt_secret("tenant-access-token"),
                    "verify_token": encrypt_secret("tenant-verify-token"),
                    "connection_status": "connected",
                    "webhook_enabled": "true",
                    "bot_enabled": "true",
                }
                for key, value in values.items():
                    db.add(
                        ChannelSetting(
                            company_id=1,
                            channel_id=channel.id,
                            key=key,
                            value=value,
                            value_type="secret" if key in {"access_token", "verify_token"} else "string",
                            is_secret=key in {"access_token", "verify_token"},
                        )
                    )
                db.add(Product(company_id=1, reference="P001", name="Producto demo"))
                db.add(Customer(company_id=1, code="C001", fiscal_name="Cliente Demo SL"))
                llm = get_or_create_settings(db, LLMSettings, 1)
                llm.provider = "openai"
                llm.api_key_encrypted = encrypt_secret("test-api-key")
                db.commit()
                status = get_setup_status(db, 1)
                self.assertTrue(status.whatsapp_connected)
                self.assertTrue(status.is_operational)
                config = whatsapp_config(db, 1)
                self.assertEqual(config.access_token, "tenant-access-token")
                stored = db.scalar(
                    select(ChannelSetting).where(
                        ChannelSetting.company_id == 1,
                        ChannelSetting.key == "access_token",
                    )
                )
                self.assertNotEqual(stored.value, "tenant-access-token")
        finally:
            cleanup()
            fixture.cleanup()

    def test_settings_is_permanent_configuration_page(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            response = client.get("/settings")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Configuración", response.text)
            self.assertIn("Confianza y automatización", response.text)
            self.assertIn("/settings/channels", response.text)
            self.assertIn("/settings/email/receive", response.text)
            self.assertNotIn("Información adicional", response.text)
        finally:
            cleanup()
            fixture.cleanup()

    def test_settings_summary_defers_module_details_until_requested(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            page = client.get("/settings")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn('<dialog id="settings-email"', page.text)
            self.assertNotIn('<dialog id="settings-ai"', page.text)

            for module_key in ["general", "identity", "email", "ai", "scoring", "decision", "export", "ftp", "advanced"]:
                response = client.get(f"/settings/module/{module_key}")
                self.assertEqual(response.status_code, 200, module_key)
                self.assertIn(f'<dialog id="settings-{module_key}"', response.text)

            missing = client.get("/settings/module/not-a-module")
            self.assertEqual(missing.status_code, 404)
        finally:
            cleanup()
            fixture.cleanup()

    def test_llm_extraction_model_selector_supports_presets_custom_and_company_isolation(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)

            page = client.get("/settings")
            self.assertEqual(page.status_code, 200)
            for label in ["GPT-5.6 Luna", "GPT-5.6 Terra", "GPT-5.6 Sol", "GPT-4.1 mini", "GPT-4.1", "Personalizado"]:
                self.assertIn(label, page.text)

            with fixture.TenantSession() as db:
                llm = get_or_create_settings(db, LLMSettings, 1)
                self.assertEqual(llm.extraction_model, DEFAULT_OPENAI_MODEL)
                db.add(Company(id=2, name="Otra empresa", legal_name="Otra empresa", country="España", language="es", timezone="Europe/Madrid", active=True))
                other = LLMSettings(company_id=2, provider="openai", api_key_encrypted=encrypt_secret("other-api-key"), extraction_model="gpt-4.1", classification_model="gpt-4.1", validation_model="gpt-4.1")
                db.add(other)
                db.commit()

            response = client.post(
                "/settings/llm",
                data={
                    "agent_enabled": "on",
                    "use_same_model_for_all": "on",
                    "provider": "openai",
                    "api_key_encrypted": "tenant-api-key",
                    "classification_model": "gpt-4.1-mini",
                    "extraction_model_mode": "gpt-5.6-luna",
                    "extraction_model": "gpt-5.6-luna",
                    "validation_model": "gpt-4.1-mini",
                    "can_read_email": "on",
                    "can_extract_pdf": "on",
                    "can_classify_email": "on",
                    "can_extract_order": "on",
                    "can_suggest_customer": "on",
                    "can_suggest_products": "on",
                    "can_calculate_score": "on",
                    "can_create_pending_order": "on",
                    "can_mark_no_order": "on",
                    "allow_auto_confirm": "",
                    "allow_auto_export": "",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            with fixture.TenantSession() as db:
                current = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
                other = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 2))
                assert current is not None
                assert other is not None
                self.assertEqual(current.extraction_model, "gpt-5.6-luna")
                self.assertEqual(current.classification_model, "gpt-5.6-luna")
                self.assertEqual(current.validation_model, "gpt-5.6-luna")
                self.assertEqual(other.extraction_model, "gpt-4.1")

            response = client.post(
                "/settings/llm",
                data={
                    "agent_enabled": "on",
                    "use_same_model_for_all": "on",
                    "provider": "openai",
                    "api_key_encrypted": "tenant-api-key",
                    "classification_model": "gpt-4.1-mini",
                    "extraction_model_mode": "gpt-5.6-terra",
                    "extraction_model": "gpt-5.6-terra",
                    "validation_model": "gpt-4.1-mini",
                    "can_read_email": "on",
                    "can_extract_pdf": "on",
                    "can_classify_email": "on",
                    "can_extract_order": "on",
                    "can_suggest_customer": "on",
                    "can_suggest_products": "on",
                    "can_calculate_score": "on",
                    "can_create_pending_order": "on",
                    "can_mark_no_order": "on",
                    "allow_auto_confirm": "",
                    "allow_auto_export": "",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/settings#agent-ai")

            with fixture.TenantSession() as db:
                current = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
                other = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 2))
                assert current is not None
                assert other is not None
                self.assertEqual(current.extraction_model, "gpt-5.6-terra")
                self.assertEqual(current.classification_model, "gpt-5.6-terra")
                self.assertEqual(current.validation_model, "gpt-5.6-terra")
                self.assertEqual(other.extraction_model, "gpt-4.1")

            response = client.post(
                "/settings/llm",
                data={
                    "agent_enabled": "on",
                    "use_same_model_for_all": "on",
                    "provider": "openai",
                    "api_key_encrypted": "tenant-api-key",
                    "classification_model": "gpt-5.6-terra",
                    "extraction_model_mode": "custom",
                    "extraction_model_custom": "gpt-5.6-orion",
                    "extraction_model": "gpt-5.6-orion",
                    "validation_model": "gpt-5.6-terra",
                    "can_read_email": "on",
                    "can_extract_pdf": "on",
                    "can_classify_email": "on",
                    "can_extract_order": "on",
                    "can_suggest_customer": "on",
                    "can_suggest_products": "on",
                    "can_calculate_score": "on",
                    "can_create_pending_order": "on",
                    "can_mark_no_order": "on",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)

            with fixture.TenantSession() as db:
                current = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
                other = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 2))
                assert current is not None
                assert other is not None
                self.assertEqual(current.extraction_model, "gpt-5.6-orion")
                self.assertEqual(current.classification_model, "gpt-5.6-orion")
                self.assertEqual(current.validation_model, "gpt-5.6-orion")
                self.assertEqual(other.extraction_model, "gpt-4.1")
        finally:
            cleanup()
            fixture.cleanup()

    def test_legacy_blank_extraction_model_uses_runtime_fallback_without_rewriting(self):
        fixture = SetupFixture()
        client, cleanup = fixture.client()
        try:
            self._login(client)
            with fixture.TenantSession() as db:
                llm = get_or_create_settings(db, LLMSettings, 1)
                llm.provider = "openai"
                llm.api_key_encrypted = encrypt_secret("tenant-api-key")
                llm.extraction_model = ""
                llm.classification_model = "gpt-4.1-mini"
                llm.validation_model = "gpt-4.1-mini"
                db.commit()

            self.assertEqual(resolve_openai_runtime_model(None), LEGACY_OPENAI_MODEL_FALLBACK)
            self.assertEqual(resolve_openai_runtime_model(""), LEGACY_OPENAI_MODEL_FALLBACK)

            page = client.get("/settings")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(f'name="extraction_model" value="{LEGACY_OPENAI_MODEL_FALLBACK}"', page.text)

            ai_module = client.get("/settings/module/ai")
            self.assertEqual(ai_module.status_code, 200)
            self.assertIn(f'name="extraction_model" value="{LEGACY_OPENAI_MODEL_FALLBACK}"', ai_module.text)

            with fixture.TenantSession() as db:
                llm = db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1))
                assert llm is not None
                self.assertEqual(llm.extraction_model, "")
                self.assertEqual(llm.classification_model, "gpt-4.1-mini")
                self.assertEqual(llm.validation_model, "gpt-4.1-mini")
        finally:
            cleanup()
            fixture.cleanup()

    def _extract(self, html: str, prefix: str) -> str:
        start = html.index(prefix) + len(prefix)
        end = html.index('"', start)
        return html[start:end]


if __name__ == "__main__":
    unittest.main()
