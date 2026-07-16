from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import os
import sys

os.environ.setdefault("APP_ENV", "development")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.encryption import encrypt_secret  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Email, EmailSettings, LLMSettings, PromptExecution, PromptTemplate, PromptVersion  # noqa: E402
from app.master.database import MasterBase  # noqa: E402
from app.master.models import EmailSyncState, MasterCompany  # noqa: E402
from app.agent.prompt_runtime import run_prompt_execution, validate_prompt_output  # noqa: E402
from app.settings.integrations import backfill_imap_emails  # noqa: E402
from scripts.evaluate_agent import run_evaluation  # noqa: E402


class FakeImapClient:
    def __init__(self, messages: dict[str, bytes]) -> None:
        self.messages = messages

    def login(self, *_args, **_kwargs):
        return "OK", [b"logged in"]

    def select(self, *_args, **_kwargs):
        return "OK", [b"2"]

    def status(self, mailbox: str, *_args, **_kwargs):
        return "OK", [f"{mailbox} (UIDVALIDITY 777)".encode()]

    def search(self, *_args, **_kwargs):
        return "OK", [b"1 2"]

    def fetch(self, msg_id, *_args, **_kwargs):
        uid = msg_id.decode()
        raw = self.messages[uid]
        meta = f"{uid} (UID {uid} RFC822 {{123}})".encode()
        return "OK", [(meta, raw)]

    def logout(self):
        return "BYE", [b"logout"]


class EmailAiLearningTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.master_path = base / "master.sqlite"
        self.tenant_path = base / "tenant.sqlite"
        self.master_engine = create_engine(f"sqlite:///{self.master_path.as_posix()}", connect_args={"check_same_thread": False})
        self.tenant_engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        MasterBase.metadata.create_all(self.master_engine)
        Base.metadata.create_all(self.tenant_engine)
        self.MasterSession = sessionmaker(bind=self.master_engine, autoflush=False, autocommit=False)
        self.TenantSession = sessionmaker(bind=self.tenant_engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.master_engine.dispose()
        self.tenant_engine.dispose()
        self.tempdir.cleanup()

    def _seed_prompt(self, db):
        db.add(LLMSettings(company_id=1, api_key_encrypted=encrypt_secret("dummy-key")))
        template = PromptTemplate(company_id=1, name="Clasificacion", purpose="classification", active_version_id=None)
        db.add(template)
        db.flush()
        version = PromptVersion(company_id=1, template_id=template.id, version=1, content='{"role":"system"}', created_by_user_id=None)
        db.add(version)
        db.flush()
        template.active_version_id = version.id
        db.commit()

    def _seed_imap(self):
        master_db = self.MasterSession()
        master_db.add(MasterCompany(id=1, name="Demo", slug="demo", active=True))
        master_db.add(EmailSyncState(company_id=1, channel_key="email", enabled=True, frequency_seconds=60, status="idle"))
        master_db.commit()
        master_db.close()

        tenant_db = self.TenantSession()
        tenant_db.add(
            EmailSettings(
                company_id=1,
                imap_host="imap.example.com",
                imap_port=993,
                imap_use_ssl=True,
                imap_security="ssl_tls",
                imap_username="demo@example.com",
                imap_password_encrypted=encrypt_secret("demo-password"),
                mailbox="INBOX",
                inbox_folder="INBOX",
                read_unread_only=False,
                auto_process_on_fetch=False,
                mark_as_read_after_import=False,
            )
        )
        tenant_db.commit()
        tenant_db.close()

    def test_prompt_execution_records_prompt_and_validation(self):
        db = self.TenantSession()
        self._seed_prompt(db)

        calls = {"count": 0}

        def fake_provider(settings, messages, model):  # noqa: ANN001
            calls["count"] += 1
            self.assertEqual(model, "gpt-4.1-mini")
            self.assertTrue(messages[1]["content"].startswith("Pedido"))
            return {
                "ok": True,
                "content": '{"tipo_correo":"pedido","confianza":0.92,"motivo":"Solicitud clara"}',
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "estimated_cost": 0.0023},
            }

        result = run_prompt_execution(
            db,
            1,
            "classification",
            db.scalar(select(LLMSettings).where(LLMSettings.company_id == 1)),
            "Pedido urgente de 10 unidades.",
            provider_call=fake_provider,
            input_reference="mail-1",
        )

        self.assertTrue(result["validation_ok"])
        self.assertEqual(result["prompt_purpose"], "classification")
        self.assertEqual(result["validated_content"]["tipo_correo"], "pedido")
        self.assertEqual(calls["count"], 1)
        executions = db.scalars(select(PromptExecution)).all()
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].output_status, "valid")
        db.close()

    def test_backfill_imap_updates_checkpoint_and_deduplicates(self):
        self._seed_imap()
        tenant_db = self.TenantSession()
        master_db = self.MasterSession()
        settings = tenant_db.scalar(select(EmailSettings).where(EmailSettings.company_id == 1))
        state = master_db.scalar(select(EmailSyncState).where(EmailSyncState.company_id == 1, EmailSyncState.channel_key == "email"))
        messages = {
            "1": (
                b"From: compras@example.com\r\n"
                b"To: pedidos@example.com\r\n"
                b"Subject: Pedido A\r\n"
                b"Message-ID: <pedido-a@example.com>\r\n"
                b"\r\n"
                b"Pedido 1"
            ),
            "2": (
                b"From: compras@example.com\r\n"
                b"To: pedidos@example.com\r\n"
                b"Subject: Pedido B\r\n"
                b"Message-ID: <pedido-b@example.com>\r\n"
                b"\r\n"
                b"Pedido 2"
            ),
        }

        with patch("app.settings.integrations._imap_client", return_value=FakeImapClient(messages)):
            first = backfill_imap_emails(
                tenant_db,
                settings,
                1,
                from_date="2026-07-01",
                to_date="2026-07-16",
                limit=2,
                sync_state=state,
                sync_session=master_db,
            )
            second = backfill_imap_emails(
                tenant_db,
                settings,
                1,
                from_date="2026-07-01",
                to_date="2026-07-16",
                limit=2,
                sync_state=state,
                sync_session=master_db,
            )

        master_db.refresh(state)
        self.assertTrue(first["ok"])
        self.assertEqual(first["saved"], 2)
        self.assertTrue(second["ok"])
        self.assertEqual(second["saved"], 0)
        self.assertEqual(second["duplicates"], 2)
        self.assertEqual(master_db.get(EmailSyncState, state.id).backfill_status, "idle")
        self.assertEqual(master_db.get(EmailSyncState, state.id).backfill_last_uid, "2")
        self.assertEqual(master_db.get(EmailSyncState, state.id).backfill_created, 2)
        self.assertEqual(tenant_db.scalar(select(func.count()).select_from(Email)) or 0, 2)
        tenant_db.close()
        master_db.close()

    def test_prompt_validation_rejects_non_json(self):
        validation = validate_prompt_output("classification", "pedido")
        self.assertFalse(validation.ok)
        self.assertEqual(validation.status, "invalid_json")

    def test_evaluation_fixture_compares_expected_and_actual(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "agent_evaluation.json"
        summary = run_evaluation(fixture)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["exact_matches"], 3)
        self.assertEqual(summary["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
