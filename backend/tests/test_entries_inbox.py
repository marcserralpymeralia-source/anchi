from __future__ import annotations

import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.core import lifespan as lifespan_module  # noqa: E402
from app.core.app_factory import create_app  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.models import BackgroundJob, Company, Email, EmailAttachment, InboundMessage, InputChannel, Order, utcnow  # noqa: E402
from app.master.models import CompanyMembership, MasterCompany, MasterTenantDatabase, MasterUser  # noqa: E402
from scripts.performance_data import build_performance_fixture, temporary_performance_environment  # noqa: E402


def _session_factory(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


class EntriesInboxTests(unittest.TestCase):
    def _client_for(self, fixture):
        context = temporary_performance_environment(fixture)
        context.__enter__()
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
            context.__exit__(None, None, None)

        return client, cleanup

    def _login(self, client, fixture, *, email: str | None = None, password: str | None = None, next_url: str = "/entries"):
        return client.post(
            "/login",
            data={"email": email or fixture.admin_email, "password": password or fixture.admin_password, "next": next_url},
            follow_redirects=False,
        )

    def test_entries_routes_are_registered(self):
        app = create_app()
        routes = {(route.path, tuple(sorted(route.methods or []))) for route in app.routes if hasattr(route, "methods")}
        self.assertIn(("/entries", ("GET",)), routes)
        self.assertIn(("/entries/sync", ("POST",)), routes)
        self.assertIn(("/entries/{entry_id}", ("GET",)), routes)
        self.assertIn(("/entries/{entry_id}/resolve", ("GET",)), routes)
        self.assertIn(("/entries/{entry_id}/process", ("POST",)), routes)

    def test_unauthenticated_entries_returns_to_entries_after_login(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            anonymous = client.get("/entries", follow_redirects=False)
            self.assertEqual(anonymous.status_code, 303)
            self.assertEqual(anonymous.headers["location"], "/login?next=%2Fentries")

            login = self._login(client, fixture)
            self.assertEqual(login.status_code, 303)
            self.assertEqual(login.headers["location"], "/entries")
            self.assertIn(f"{get_settings().session_cookie}=", login.headers.get("set-cookie", ""))

            entries = client.get("/entries", follow_redirects=False)
            self.assertEqual(entries.status_code, 200)
            self.assertIn("Entradas", entries.text)
        finally:
            cleanup()
            fixture.cleanup()

    def test_inactive_membership_gets_403_not_login_loop(self):
        fixture = build_performance_fixture("small")
        master_engine, MasterSession = _session_factory(fixture.master_database_url)
        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            with MasterSession() as db:
                membership = db.get(CompanyMembership, 1)
                membership.is_active = False
                db.commit()

            response = client.get("/entries", follow_redirects=False)
            self.assertEqual(response.status_code, 403)
            self.assertNotEqual(response.headers.get("location"), "/login")
        finally:
            cleanup()
            master_engine.dispose()
            fixture.cleanup()

    def test_entries_lists_email_whatsapp_attachments_and_chat_without_imap(self):
        fixture = build_performance_fixture("small")
        tenant_engine, TenantSession = _session_factory(fixture.tenant_database_url)
        now = utcnow()
        try:
            with TenantSession() as db:
                email = Email(
                    company_id=1,
                    sender="compras@example.com",
                    subject="Pedido inbox visible",
                    body="Necesito 4 cajas de producto demo.",
                    status="pending",
                    agent_status="not_processed",
                    detected_type="pedido",
                    received_at=now,
                    has_attachments=True,
                    has_pdf=True,
                )
                db.add(email)
                db.flush()
                db.add(
                    EmailAttachment(
                        company_id=1,
                        email_id=email.id,
                        filename="pedido.pdf",
                        content_type="application/pdf",
                        size_bytes=2048,
                        is_pdf=True,
                        extracted_text="PDF de pedido",
                    )
                )
                channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
                if channel is None:
                    channel = InputChannel(company_id=1, key="whatsapp", name="WhatsApp", channel_type="message")
                    db.add(channel)
                    db.flush()
                db.add(
                    InboundMessage(
                        company_id=1,
                        channel_id=channel.id,
                        provider="manual_import",
                        source_external_id="whatsapp-demo-visible",
                        sender="+34 600 000 001",
                        subject="Pedido WhatsApp Demo",
                        original_content="Cliente: Hola, necesito 10 unidades\nAnchi: Lo revisamos ahora",
                        raw_payload_json=json.dumps(
                            {
                                "messages": [
                                    {"speaker": "Cliente", "direction": "inbound", "text": "Hola, necesito 10 unidades", "time": "09:01"},
                                    {"speaker": "Anchi", "direction": "outbound", "text": "Lo revisamos ahora", "time": "09:02"},
                                ]
                            },
                            ensure_ascii=False,
                        ),
                        status="doubtful",
                        detected_type="pedido",
                        score=58,
                        received_at=now + timedelta(minutes=1),
                    )
                )
                db.commit()

            client, cleanup = self._client_for(fixture)
            try:
                self._login(client, fixture)
                with patch("app.settings.integrations.imaplib.IMAP4_SSL") as imap_ssl, patch("app.settings.integrations.imaplib.IMAP4") as imap_plain:
                    response = client.get("/entries", follow_redirects=False)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(imap_ssl.called)
                self.assertFalse(imap_plain.called)
            finally:
                cleanup()

            self.assertIn("Sincronizar ahora", response.text)
            self.assertIn("Importar entrada", response.text)
            self.assertIn("Pedido inbox visible", response.text)
            self.assertIn("Pendiente de procesar", response.text)
            self.assertIn("pedido.pdf", response.text)
            self.assertIn("Pedido WhatsApp Demo", response.text)
            self.assertIn("WhatsApp", response.text)
            self.assertIn("Hola, necesito 10 unidades", response.text)
            self.assertIn("Lo revisamos ahora", response.text)
            self.assertIn("Pendiente de validar", response.text)
        finally:
            tenant_engine.dispose()
            fixture.cleanup()

    def test_entries_filters_and_pagination_are_stable(self):
        fixture = build_performance_fixture("small")
        tenant_engine, TenantSession = _session_factory(fixture.tenant_database_url)
        try:
            with TenantSession() as db:
                older = Email(
                    company_id=1,
                    sender="old@example.com",
                    subject="Pedido pagina antigua",
                    body="Antiguo",
                    status="pending",
                    agent_status="not_processed",
                    received_at=utcnow() - timedelta(minutes=2),
                )
                newer = Email(
                    company_id=1,
                    sender="new@example.com",
                    subject="Pedido pagina reciente",
                    body="Reciente",
                    status="pending",
                    agent_status="not_processed",
                    received_at=utcnow(),
                )
                channel = db.scalar(select(InputChannel).where(InputChannel.company_id == 1, InputChannel.key == "whatsapp"))
                if channel is None:
                    channel = InputChannel(company_id=1, key="whatsapp", name="WhatsApp", channel_type="message")
                    db.add(channel)
                    db.flush()
                whatsapp = InboundMessage(
                    company_id=1,
                    channel_id=channel.id,
                    provider="manual_import",
                    source_external_id="whatsapp-filter",
                    sender="+34 600 000 002",
                    subject="Solo en WhatsApp",
                    original_content="Pedido por WhatsApp",
                    status="received",
                    received_at=utcnow() + timedelta(minutes=1),
                )
                db.add_all([older, newer, whatsapp])
                db.commit()

            client, cleanup = self._client_for(fixture)
            try:
                self._login(client, fixture)
                page_one = client.get("/entries?tab=all&page_size=10&page=1", follow_redirects=False)
                page_two = client.get("/entries?tab=all&page_size=10&page=2", follow_redirects=False)
                email_tab = client.get("/entries?tab=email&search=Pedido%20pagina", follow_redirects=False)
                whatsapp_tab = client.get("/entries?tab=whatsapp&search=Solo%20en%20WhatsApp", follow_redirects=False)
            finally:
                cleanup()

            self.assertEqual(page_one.status_code, 200)
            self.assertEqual(page_two.status_code, 200)
            self.assertIn("Solo en WhatsApp", page_one.text)
            self.assertIn("Pagina 2 de", page_two.text)
            self.assertEqual(email_tab.status_code, 200)
            self.assertIn("Pedido pagina reciente", email_tab.text)
            self.assertIn("Pedido pagina antigua", email_tab.text)
            self.assertNotIn("Solo en WhatsApp", email_tab.text)
            self.assertEqual(whatsapp_tab.status_code, 200)
            self.assertIn("Solo en WhatsApp", whatsapp_tab.text)
        finally:
            tenant_engine.dispose()
            fixture.cleanup()

    def test_entries_actions_enqueue_jobs_with_dedupe(self):
        fixture = build_performance_fixture("small")
        tenant_engine, TenantSession = _session_factory(fixture.tenant_database_url)
        try:
            with TenantSession() as db:
                email = Email(
                    company_id=1,
                    sender="process@example.com",
                    subject="Pedido para procesar desde entradas",
                    body="Procesar",
                    status="pending",
                    agent_status="not_processed",
                    received_at=utcnow(),
                )
                db.add(email)
                db.commit()
                email_id = email.id

            client, cleanup = self._client_for(fixture)
            try:
                self._login(client, fixture)
                first = client.post(f"/entries/email-{email_id}/process", follow_redirects=False)
                second = client.post(f"/entries/email-{email_id}/process", follow_redirects=False)
                sync_first = client.post("/entries/sync", follow_redirects=False)
                sync_second = client.post("/entries/sync", follow_redirects=False)
            finally:
                cleanup()

            self.assertEqual(first.status_code, 303)
            self.assertEqual(second.status_code, 303)
            self.assertEqual(sync_first.status_code, 303)
            self.assertEqual(sync_first.headers["location"], "/entries?sync=queued")
            self.assertEqual(sync_second.status_code, 303)
            with TenantSession() as db:
                process_jobs = db.scalars(select(BackgroundJob).where(BackgroundJob.company_id == 1, BackgroundJob.job_type == "process_email")).all()
                sync_jobs = db.scalars(select(BackgroundJob).where(BackgroundJob.company_id == 1, BackgroundJob.job_type == "email_sync")).all()
            self.assertEqual(len(process_jobs), 1)
            self.assertEqual(len(sync_jobs), 1)
        finally:
            tenant_engine.dispose()
            fixture.cleanup()

    def test_entries_legacy_routes_redirect_once(self):
        fixture = build_performance_fixture("small")
        client, cleanup = self._client_for(fixture)
        try:
            self._login(client, fixture)
            channels = client.get("/channels?tab=email", follow_redirects=False)
            history = client.get("/history", follow_redirects=False)
        finally:
            cleanup()
            fixture.cleanup()

        self.assertEqual(channels.status_code, 303)
        self.assertEqual(channels.headers["location"], "/entries?tab=email")
        self.assertEqual(history.status_code, 303)
        self.assertEqual(history.headers["location"], "/entries?tab=processed&date_range=30d")

    def test_entries_are_scoped_by_authenticated_tenant(self):
        fixture = build_performance_fixture("small")
        master_engine, MasterSession = _session_factory(fixture.master_database_url)
        tenant_engine, TenantSession = _session_factory(fixture.tenant_database_url)
        client, cleanup = self._client_for(fixture)
        try:
            with MasterSession() as db:
                db.add(MasterCompany(id=2, name="Tenant B", slug="tenant-b", active=True))
                db.add(MasterUser(id=2, email="admin@tenant-b.local", full_name="Admin B", password_hash=hash_password("admin123"), is_active=True))
                db.add(CompanyMembership(id=2, user_id=2, company_id=2, role_key="Administrador", is_active=True, is_owner=True))
                db.add(MasterTenantDatabase(company_id=2, database_key="tenant-b", database_url=fixture.tenant_database_url, database_type="sqlite", is_active=True, health_status="ok"))
                db.commit()
            with TenantSession() as db:
                db.add(Company(id=2, name="Tenant B", active=True))
                db.add(
                    Email(
                        company_id=1,
                        external_id="same-provider-id",
                        sender="tenant-a@example.com",
                        subject="Entrada visible tenant A",
                        body="A",
                        status="pending",
                        agent_status="not_processed",
                        received_at=utcnow(),
                    )
                )
                db.add(
                    Email(
                        company_id=2,
                        external_id="same-provider-id",
                        sender="tenant-b@example.com",
                        subject="Entrada visible tenant B",
                        body="B",
                        status="pending",
                        agent_status="not_processed",
                        received_at=utcnow(),
                    )
                )
                db.commit()

            self._login(client, fixture)
            tenant_a = client.get("/entries", follow_redirects=False)
            self.assertEqual(tenant_a.status_code, 200)
            self.assertIn("Entrada visible tenant A", tenant_a.text)
            self.assertNotIn("Entrada visible tenant B", tenant_a.text)

            client.post("/logout", follow_redirects=False)
            self._login(client, fixture, email="admin@tenant-b.local", password="admin123")
            tenant_b = client.get("/entries", follow_redirects=False)
            self.assertEqual(tenant_b.status_code, 200)
            self.assertIn("Entrada visible tenant B", tenant_b.text)
            self.assertNotIn("Entrada visible tenant A", tenant_b.text)
        finally:
            cleanup()
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
