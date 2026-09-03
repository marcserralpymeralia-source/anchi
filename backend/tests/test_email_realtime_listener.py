from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.channels.service import get_or_create_channel
from app.core.encryption import encrypt_secret  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Email, EmailSettings  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import EmailSyncState, MasterCompany, MasterTenantDatabase  # noqa: E402
from app.tenancy.database import get_tenant_engine  # noqa: E402
from app.workers.email_listener import reconcile_tenant_email  # noqa: E402


class FakeImapClient:
    def __init__(self, messages: dict[str, bytes], search_result: bytes = b"2") -> None:
        self.messages = messages
        self.search_result = search_result
        self.uid_calls: list[tuple] = []

    def login(self, *_args, **_kwargs):
        return "OK", [b"logged in"]

    def select(self, *_args, **_kwargs):
        return "OK", [b"1"]

    def status(self, mailbox: str, *_args, **_kwargs):
        return "OK", [f"{mailbox} (UIDVALIDITY 777)".encode()]

    def uid(self, command, *args, **_kwargs):  # noqa: ANN001
        self.uid_calls.append((command, *args))
        if command == "search":
            return "OK", [self.search_result]
        if command == "fetch":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            return "OK", [(f"{uid} (UID {uid} RFC822 {{123}})".encode(), self.messages[uid])]
        return "OK", [b""]

    def logout(self):
        return "BYE", [b"logout"]


class EmailRealtimeListenerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_one_path = base / "tenant-one.sqlite"
        self.tenant_two_path = base / "tenant-two.sqlite"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_one_engine = create_engine(f"sqlite:///{self.tenant_one_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_two_engine = create_engine(f"sqlite:///{self.tenant_two_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_one_engine)
        Base.metadata.create_all(self.tenant_two_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantOneSession = sessionmaker(bind=self.tenant_one_engine, autoflush=False, autocommit=False)
        self.TenantTwoSession = sessionmaker(bind=self.tenant_two_engine, autoflush=False, autocommit=False)
        with self.MasterSession() as db:
            db.add_all(
                [
                    MasterCompany(id=1, name="Tenant Uno", slug="tenant-uno", active=True),
                    MasterCompany(id=2, name="Tenant Dos", slug="tenant-dos", active=True),
                    MasterTenantDatabase(company_id=1, database_key="tenant_uno", database_url=f"sqlite:///{self.tenant_one_path.as_posix()}", database_type="sqlite", is_active=True),
                    MasterTenantDatabase(company_id=2, database_key="tenant_dos", database_url=f"sqlite:///{self.tenant_two_path.as_posix()}", database_type="sqlite", is_active=True),
                    EmailSyncState(company_id=1, channel_key="email", enabled=True, frequency_seconds=60, status="idle", uidvalidity="777", last_seen_uid="1"),
                    EmailSyncState(company_id=2, channel_key="email", enabled=True, frequency_seconds=60, status="idle", uidvalidity="777", last_seen_uid="1"),
                ]
            )
            db.commit()
        for company_id, Session in ((1, self.TenantOneSession), (2, self.TenantTwoSession)):
            with Session() as db:
                db.add(
                    EmailSettings(
                        company_id=company_id,
                        provider="gmail",
                        imap_host="imap.gmail.com",
                        imap_port=993,
                        imap_use_ssl=True,
                        imap_security="ssl_tls",
                        imap_username=f"tenant{company_id}@example.com",
                        imap_password_encrypted=encrypt_secret("demo-password"),
                        mailbox="INBOX",
                        inbox_folder="INBOX",
                        auto_sync_enabled=True,
                        auto_process_on_fetch=False,
                        read_unread_only=False,
                        read_limit=10,
                    )
                )
                channel = get_or_create_channel(db, company_id, "email")
                channel.is_active = True
                db.commit()

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_one_engine.dispose()
        self.tenant_two_engine.dispose()
        get_tenant_engine.cache_clear()
        self.tempdir.cleanup()

    def test_reconcile_tenant_email_isolated_by_database(self):
        raw_message = (
            b"From: compras@example.com\r\n"
            b"Subject: Pedido tenant uno\r\n"
            b"Message-ID: <tenant-one@example.com>\r\n"
            b"\r\n"
            b"Pedido aislado"
        )
        fake_client = FakeImapClient({"2": raw_message})
        with self.MasterSession() as master_db:
            tenant = master_db.scalar(select(MasterTenantDatabase).where(MasterTenantDatabase.company_id == 1))
            assert tenant is not None
            with patch("app.settings.integrations._imap_client", return_value=fake_client):
                result = reconcile_tenant_email(master_db, tenant, owner="test-listener", force=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["saved"], 1)
            state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == 1, EmailSyncState.channel_key == "email"))
            assert state is not None
            self.assertEqual(state.listener_status, "polling")
            self.assertIsNotNone(state.listener_last_heartbeat_at)
            self.assertEqual(state.last_seen_uid, "2")

        with self.TenantOneSession() as db:
            saved = db.scalar(select(Email).where(Email.company_id == 1))
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved.subject, "Pedido tenant uno")

        with self.TenantTwoSession() as db:
            self.assertIsNone(db.scalar(select(Email).where(Email.company_id == 2)))


if __name__ == "__main__":
    unittest.main()
