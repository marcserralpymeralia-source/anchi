from __future__ import annotations

import os
import re
import unittest
from html import unescape

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


def _extract_receive_payload(html: str) -> dict[str, str]:
    marker = 'action="/settings/email/receive"'
    start = html.find(marker)
    if start == -1:
        raise AssertionError("No se encontró el formulario de recepción de correo")
    form_start = html.rfind("<form", 0, start)
    form_end = html.find("</form>", start)
    form_html = html[form_start : form_end + 7]
    payload: dict[str, str] = {}

    for match in re.finditer(r'<input([^>]*)name="([^"]+)"([^>]*)>', form_html, re.S):
        attrs = f"{match.group(1)} {match.group(3)}"
        name = match.group(2)
        if 'type="checkbox"' in attrs:
            if "checked" in attrs:
                payload[name] = "on"
            continue
        value_match = re.search(r'value="([^"]*)"', attrs)
        payload[name] = unescape(value_match.group(1)) if value_match else ""

    for match in re.finditer(r'<textarea([^>]*)name="([^"]+)"[^>]*>(.*?)</textarea>', form_html, re.S):
        payload[match.group(2)] = unescape(match.group(3))

    for match in re.finditer(r'<select([^>]*)name="([^"]+)"[^>]*>(.*?)</select>', form_html, re.S):
        name = match.group(2)
        body = match.group(3)
        selected = re.search(r'<option value="([^"]*)" selected', body)
        if selected:
            payload[name] = unescape(selected.group(1))
            continue
        first = re.search(r'<option value="([^"]*)"', body)
        if first and name not in payload:
            payload[name] = unescape(first.group(1))

    return payload


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
                settings_html = client.get("/settings").text
                payload = _extract_receive_payload(settings_html)
                payload.update(
                    {
                        "provider": "gmail",
                        "imap_host": "imap.gmail.com",
                        "imap_port": "993",
                        "imap_security": "ssl_tls",
                        "imap_use_ssl": "on",
                        "imap_username": "demo.user@example.com",
                        "imap_password_encrypted": "DemoAppPassword123!",
                        "inbox_folder": "INBOX",
                        "initial_history_mode": "new",
                        "initial_history_limit": "20",
                        "auto_sync_enabled": "",
                        "auto_process_on_fetch": "",
                    }
                )

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
