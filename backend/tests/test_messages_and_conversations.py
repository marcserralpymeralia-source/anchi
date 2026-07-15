from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "development")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.services import AgentProcessingService  # noqa: E402
from app.core.encryption import encrypt_secret  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Conversation, Email, InboundMessage, LLMSettings, Order  # noqa: E402
from app.messages.service import normalize_direction, normalize_recipients, upsert_inbound_message  # noqa: E402


class MessagesAndConversationsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.tenant_path = base / "tenant.sqlite"
        self.engine = create_engine(f"sqlite:///{self.tenant_path.as_posix()}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed_llm(self) -> None:
        db = self.Session()
        db.add(LLMSettings(company_id=1, provider="openai", api_key_encrypted=encrypt_secret("test-token")))
        db.commit()
        db.close()

    def test_message_upsert_creates_conversation_and_dedupes(self):
        db = self.Session()
        received_at = datetime(2026, 7, 15, 10, 30, tzinfo=timezone.utc)

        message, conversation = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="msg-1",
            sender="cliente@example.com",
            recipients=["compras@example.com"],
            subject="Pedido demo",
            text_content="10 cajas",
            external_thread_id="thread-1",
            received_at=received_at,
            metadata={"message_id": "msg-1"},
            content_type="email",
            has_attachments=True,
            has_pdf=True,
        )
        db.commit()

        duplicate, duplicate_conversation = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="msg-1",
            sender="cliente@example.com",
            recipients=["compras@example.com"],
            subject="Pedido demo",
            text_content="20 cajas",
            external_thread_id="thread-1",
            received_at=received_at,
            metadata={"message_id": "msg-1"},
            content_type="email",
            has_attachments=True,
            has_pdf=True,
        )
        db.commit()

        self.assertEqual(message.id, duplicate.id)
        self.assertEqual(conversation.id, duplicate_conversation.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(InboundMessage)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(Conversation)), 1)
        self.assertEqual(message.provider, "imap")
        self.assertEqual(message.direction, "inbound")
        self.assertEqual(normalize_direction("externo"), "inbound")
        self.assertEqual(normalize_recipients("a@example.com; b@example.com"), ["a@example.com", "b@example.com"])
        db.close()

    def test_email_adapter_creates_conversation_and_pipeline_uses_it(self):
        self._seed_llm()
        db = self.Session()
        email = Email(company_id=1, external_id="mail-1", sender="cliente@example.com", subject="Pedido", body="10 cajas")
        db.add(email)
        db.commit()

        calls = {"count": 0}

        def fake_process_inbound_message(_self, session, inbound_message, user=None, force_order=False, email=None):  # noqa: ANN001
            calls["count"] += 1
            order = Order(
                company_id=inbound_message.company_id,
                conversation_id=inbound_message.conversation_id,
                email_id=email.id if email else None,
                customer_detected_name="Cliente demo",
                status="pedido_pendiente_revision",
                score=91,
            )
            session.add(order)
            session.flush()
            inbound_message.order_id = order.id
            inbound_message.status = "order_detected"
            inbound_message.processing_step = "completed"
            inbound_message.score = 91
            session.commit()
            return {"ok": True, "status": "order_detected", "message": f"Pedido {order.id} creado.", "order_id": order.id, "score": 91}

        with patch("app.agent.platform.UnifiedOrderPipelineService.process_inbound_message", new=fake_process_inbound_message):
            result_first = AgentProcessingService().process_email(db, email)
            result_second = AgentProcessingService().process_email(db, email)

        self.assertTrue(result_first["ok"])
        self.assertEqual(result_first["order_id"], result_second["order_id"])
        self.assertEqual(calls["count"], 1)

        refreshed_email = db.get(Email, email.id)
        self.assertIsNotNone(refreshed_email.conversation_id)
        inbound = db.scalar(select(InboundMessage).where(InboundMessage.company_id == 1, InboundMessage.source_external_id == "mail-1"))
        self.assertIsNotNone(inbound)
        self.assertEqual(inbound.conversation_id, refreshed_email.conversation_id)
        order = db.get(Order, result_first["order_id"])
        self.assertEqual(order.conversation_id, refreshed_email.conversation_id)
        db.close()

    def test_messages_without_thread_use_stable_fallback(self):
        db = self.Session()
        first, first_conversation = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="mail-a",
            sender="cliente@example.com",
            recipients=["ventas@example.com"],
            subject="Pedido repetido",
            text_content="1 caja",
            received_at=datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc),
            metadata={"message_id": "mail-a"},
            content_type="email",
        )
        second, second_conversation = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="mail-b",
            sender="cliente@example.com",
            recipients=["ventas@example.com"],
            subject="Pedido repetido",
            text_content="2 cajas",
            received_at=datetime(2026, 7, 15, 11, 5, tzinfo=timezone.utc),
            metadata={"message_id": "mail-b"},
            content_type="email",
        )
        db.commit()

        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first_conversation.id, second_conversation.id)
        self.assertEqual(db.scalar(select(func.count()).select_from(Conversation)), 2)
        db.close()


if __name__ == "__main__":
    unittest.main()
