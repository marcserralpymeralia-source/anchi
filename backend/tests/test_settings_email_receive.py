from __future__ import annotations

import os
import unittest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")
os.environ.setdefault("PERFORMANCE_PROFILING_ENABLED", "true")
os.environ.setdefault("ENABLE_PERFORMANCE_PROFILING", "true")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.encryption import decrypt_secret
from app.db.models import EmailSettings, User
from app.master.models import CompanyMembership, EmailSyncState
from scripts.performance_data import build_performance_fixture, performance_test_client


class SettingsEmailReceiveHttpTests(unittest.TestCase):
    def test_save_email_settings_real_http_flow(self):
        fixture = build_performance_fixture("small")
        master_engine = create_engine(fixture.master_database_url, connect_args={"check_same_thread": False})
        tenant_engine = create_engine(fixture.tenant_database_url, connect_args={"check_same_thread": False})
        MasterSession = sessionmaker(bind=master_engine, autoflush=False, autocommit=False)
        TenantSession = sessionmaker(bind=tenant_engine, autoflush=False, autocommit=False)
        try:
            with MasterSession() as db:
                membership = db.scalar(select(CompanyMembership).where(CompanyMembership.company_id == fixture.company_id))
                if membership:
                    membership.role_key = "Administrador"
                    db.commit()
            with TenantSession() as db:
                db.query(User).delete()
                db.commit()

            with performance_test_client(fixture) as client:
                settings_page = client.get("/settings")
                self.assertEqual(settings_page.status_code, 200)
                self.assertIn("/setup/channels", settings_page.text)
                payload = {
                    "provider": "gmail",
                    "connected_email": "demo.user@example.com",
                    "imap_host": "imap.gmail.com",
                    "imap_port": "993",
                    "imap_security": "ssl_tls",
                    "imap_use_ssl": "on",
                    "imap_username": "demo.user@example.com",
                    "imap_password_encrypted": "DemoAppPassword123!",
                    "inbox_folder": "INBOX",
                    "mailbox": "INBOX",
                    "initial_history_mode": "new",
                    "initial_history_limit": "20",
                    "auto_sync_enabled": "",
                    "auto_process_on_fetch": "",
                    "read_unread_only": "",
                    "mark_as_read_after_import": "",
                    "move_after_processing": "",
                }

                response = client.post("/settings/email/receive", data=payload, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/settings#email-receive")
                self.assertNotIn("internal_error", response.text.lower())

                with TenantSession() as db:
                    saved = db.scalar(select(EmailSettings).where(EmailSettings.company_id == fixture.company_id))
                    self.assertIsNotNone(saved)
                    assert saved is not None
                    self.assertEqual(saved.provider, "gmail")
                    self.assertEqual(saved.imap_host, "imap.gmail.com")
                    self.assertEqual(saved.imap_port, 993)
                    self.assertEqual(saved.imap_security, "ssl_tls")
                    self.assertTrue(saved.imap_use_ssl)
                    self.assertEqual(saved.imap_username, "demo.user@example.com")
                    self.assertEqual(saved.inbox_folder, "INBOX")
                    self.assertEqual(saved.initial_history_mode, "new")
                    self.assertEqual(saved.initial_history_limit, 20)
                    self.assertFalse(saved.auto_sync_enabled)
                    self.assertFalse(saved.auto_process_on_fetch)
                    self.assertIsNone(saved.updated_by)
                    encrypted_password = saved.imap_password_encrypted

                self.assertIsNotNone(encrypted_password)
                assert encrypted_password is not None
                self.assertNotEqual(encrypted_password, "DemoAppPassword123!")
                self.assertEqual(decrypt_secret(encrypted_password), "DemoAppPassword123!")

                with MasterSession() as db:
                    state = db.scalar(
                        select(EmailSyncState).where(
                            EmailSyncState.company_id == fixture.company_id,
                            EmailSyncState.channel_key == "email",
                        )
                    )
                    self.assertIsNotNone(state)
                    assert state is not None
                    self.assertFalse(state.enabled)
                    self.assertEqual(state.mailbox, "INBOX")
                    self.assertEqual(state.source_provider, "gmail")
                    self.assertEqual(state.source_host, "imap.gmail.com")
                    self.assertEqual(state.source_username, "demo.user@example.com")

                payload["imap_password_encrypted"] = "********"
                payload["initial_history_limit"] = "20"
                follow_response = client.post("/settings/email/receive", data=payload, follow_redirects=True)
                self.assertEqual(follow_response.status_code, 200)
                self.assertNotIn("internal_error", follow_response.text.lower())

                with TenantSession() as db:
                    reloaded = db.scalar(select(EmailSettings).where(EmailSettings.company_id == fixture.company_id))
                    self.assertIsNotNone(reloaded)
                    assert reloaded is not None
                    self.assertEqual(reloaded.imap_host, "imap.gmail.com")
                    self.assertEqual(reloaded.imap_username, "demo.user@example.com")
                    self.assertEqual(reloaded.imap_password_encrypted, encrypted_password)
                    self.assertEqual(decrypt_secret(reloaded.imap_password_encrypted), "DemoAppPassword123!")
        finally:
            master_engine.dispose()
            tenant_engine.dispose()
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
