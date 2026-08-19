from __future__ import annotations

import os
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from app.db.models import Email, EmailSettings  # noqa: E402
from app.master.models import EmailSyncState  # noqa: E402
from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


def _tenant_session(tenant_path: Path):
    engine = create_engine(f"sqlite:///{tenant_path.as_posix()}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class MailInboxRoutesTests(unittest.TestCase):
    def test_mail_routes_are_registered(self):
        routes = {(route.path, tuple(sorted(route.methods or []))) for route in __import__("app.main", fromlist=["app"]).app.routes if hasattr(route, "methods")}
        expected = {
            ("/mail", ("GET",)),
            ("/mail/{email_id}", ("GET",)),
            ("/mail/{email_id}/process", ("POST",)),
            ("/cron/email-sync", ("GET", "POST")),
        }
        for item in expected:
            self.assertIn(item, routes)

    def test_mail_inbox_page_and_detail_are_available(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_path)
        try:
            with SessionLocal() as db:
                email_id = db.scalar(select(Email.id).where(Email.company_id == fixture.company_id).order_by(Email.id))
            self.assertIsNotNone(email_id)

            with performance_test_client(fixture) as client:
                inbox = client.get("/mail")
                detail = client.get(f"/mail/{email_id}")

            self.assertEqual(inbox.status_code, 200)
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Bandeja de entrada", inbox.text)
            self.assertIn("Detalle de correo", detail.text)
            self.assertNotIn("Internal Server Error", inbox.text)
            self.assertNotIn("Internal Server Error", detail.text)
        finally:
            fixture.cleanup()

    def test_mail_inbox_defaults_to_ten_and_filters_active_account(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(fixture.master_database_url, connect_args={"check_same_thread": False})
        tenant_engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            with MasterSession() as master_db:
                state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == fixture.company_id, EmailSyncState.channel_key == "email"))
                self.assertIsNotNone(state)
                assert state is not None
                state.mailbox = "INBOX"
                state.uidvalidity = "777"
                master_db.commit()

            with TenantSession() as tenant_db:
                for index in range(12):
                    tenant_db.add(
                        Email(
                            company_id=fixture.company_id,
                            external_id=f"active-{index}",
                            message_id=f"<active-{index}@example.com>",
                            imap_mailbox="INBOX",
                            imap_uidvalidity="777",
                            imap_uid=str(200 + index),
                            sender="nuevo@example.com",
                            subject=f"Activo {index}",
                            body="Pedido activo",
                            received_at=datetime(2026, 7, 31, 12, index, tzinfo=timezone.utc),
                        )
                    )
                for index in range(2):
                    tenant_db.add(
                        Email(
                            company_id=fixture.company_id,
                            external_id=f"legacy-{index}",
                            message_id=f"<legacy-{index}@example.com>",
                            imap_mailbox="INBOX",
                            imap_uidvalidity="111",
                            imap_uid=str(50 + index),
                            sender="legacy@example.com",
                            subject=f"Antiguo {index}",
                            body="Pedido antiguo",
                            received_at=datetime(2026, 7, 30, 12, index, tzinfo=timezone.utc),
                        )
                    )
                tenant_db.commit()

            with performance_test_client(fixture) as client:
                inbox = client.get("/mail")

            self.assertEqual(inbox.status_code, 200)
            self.assertIn("Activo 11", inbox.text)
            self.assertIn("Activo 2", inbox.text)
            self.assertNotIn("Activo 0", inbox.text)
            self.assertNotIn("Antiguo 0", inbox.text)
            self.assertIn("Mostrando 1-10", inbox.text)
            self.assertIn("Cuenta activa", inbox.text)
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()

    def test_mail_inbox_redirects_to_settings_when_imap_is_not_ready(self):
        fixture = build_performance_fixture("small")
        tenant_engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            with TenantSession() as tenant_db:
                settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == fixture.company_id))
                self.assertIsNotNone(settings)
                assert settings is not None
                settings.imap_host = None
                settings.imap_username = None
                settings.imap_password_encrypted = None
                tenant_db.commit()

            with performance_test_client(fixture) as client:
                response = client.get("/mail", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/settings#email-receive")
        finally:
            tenant_engine.dispose()
            fixture.cleanup()

    def test_mail_inbox_falls_back_when_scope_resolution_fails(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client, patch("app.mail.routes._active_mail_scope", side_effect=SQLAlchemyError("boom")):
                response = client.get("/mail")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Bandeja de entrada", response.text)
            self.assertNotIn("Internal Server Error", response.text)
            self.assertNotIn("Location", response.headers)
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
