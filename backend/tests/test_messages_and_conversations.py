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
from app.settings.integrations import (  # noqa: E402
    _existing_email_for_imap,
    _normalized_email_external_id,
)

from app.agent.platform import UnifiedOrderPipelineService

from app.messages.service import (  # noqa: E402
    NormalizedMessage,
    normalize_direction,
    normalize_recipients,
    persist_normalized_message,
    upsert_inbound_message,
    link_message_to_order,
)


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


    def test_normalized_message_is_common_channel_contract(self):
        db = self.Session()

        email = NormalizedMessage(
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="email-001",
            sender="cliente@example.com",
            recipients=["pedidos@example.com"],
            subject="Pedido",
            text_content="10 cajas",
            external_thread_id="thread-email-001",
            received_at=datetime.now(timezone.utc),
            metadata={"source": "email"},
        )

        whatsapp = NormalizedMessage(
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="wa-001",
            sender="34600000000",
            recipients=["34900000000"],
            subject="WhatsApp",
            text_content="10 cajas",
            external_thread_id="thread-wa-001",
            received_at=datetime.now(timezone.utc),
            metadata={"source": "whatsapp"},
        )

        email_message, email_conversation = persist_normalized_message(
            db,
            email,
            content_type="email",
        )
        whatsapp_message, whatsapp_conversation = persist_normalized_message(
            db,
            whatsapp,
            content_type="whatsapp_text",
        )
        db.commit()

        self.assertEqual(email_message.company_id, 1)
        self.assertEqual(whatsapp_message.company_id, 1)
        self.assertEqual(email_message.provider, "imap")
        self.assertEqual(whatsapp_message.provider, "meta")
        self.assertEqual(email_message.source_external_id, "email-001")
        self.assertEqual(whatsapp_message.source_external_id, "wa-001")
        self.assertIsNotNone(email_conversation.id)
        self.assertIsNotNone(whatsapp_conversation.id)

        self.assertEqual(
            db.scalar(select(func.count()).select_from(InboundMessage)),
            2,
        )
        self.assertEqual(
            db.scalar(select(func.count()).select_from(Conversation)),
            2,
        )

        db.close()


    def test_message_identity_is_scoped_by_tenant_channel_and_provider(self):
        db = self.Session()

        first, _ = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="same-id",
            sender="a@example.com",
            text_content="pedido",
        )

        duplicate, _ = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="same-id",
            sender="a@example.com",
            text_content="pedido repetido",
        )

        other_channel, _ = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="whatsapp",
            provider="meta",
            external_id="same-id",
            sender="34600000000",
            text_content="pedido",
        )

        other_tenant, _ = upsert_inbound_message(
            db,
            company_id=2,
            channel_key="email",
            provider="imap",
            external_id="same-id",
            sender="b@example.com",
            text_content="pedido",
        )

        db.commit()

        self.assertEqual(first.id, duplicate.id)
        self.assertNotEqual(first.id, other_channel.id)
        self.assertNotEqual(first.id, other_tenant.id)

        self.assertEqual(
            db.scalar(select(func.count()).select_from(InboundMessage)),
            3,
        )

        db.close()



    def test_email_and_whatsapp_share_same_pipeline_contract(self):
        self._seed_llm()
        db = self.Session()

        email_message, email_conversation = persist_normalized_message(
            db,
            NormalizedMessage(
                company_id=1,
                channel_key="email",
                provider="imap",
                external_id="contract-email-1",
                sender="cliente@email.com",
                recipients=["pedidos@email.com"],
                subject="Pedido email",
                text_content="10 cajas",
                external_thread_id="thread-email",
                metadata={"source": "email"},
            ),
            content_type="email",
        )

        whatsapp_message, whatsapp_conversation = persist_normalized_message(
            db,
            NormalizedMessage(
                company_id=1,
                channel_key="whatsapp",
                provider="meta",
                external_id="contract-whatsapp-1",
                sender="34600000000",
                recipients=["34900000000"],
                subject="Pedido whatsapp",
                text_content="10 cajas",
                external_thread_id="thread-whatsapp",
                metadata={"source": "whatsapp"},
            ),
            content_type="whatsapp_text",
        )

        db.commit()

        received = []

        def fake_process(_self, session, inbound_message, user=None, force_order=False, email=None):
            received.append(inbound_message)
            return {
                "ok": True,
                "status": "received",
            }

        with patch(
            "app.agent.platform.UnifiedOrderPipelineService.process_inbound_message",
            new=fake_process,
        ):
            pipeline = UnifiedOrderPipelineService()
            pipeline.process_inbound_message(db, email_message)
            pipeline.process_inbound_message(db, whatsapp_message)

        self.assertEqual(len(received), 2)
        self.assertEqual(received[0].company_id, 1)
        self.assertEqual(received[1].company_id, 1)

        self.assertNotEqual(
            received[0].channel_id,
            received[1].channel_id,
        )

        self.assertEqual(
            received[0].conversation_id,
            email_conversation.id,
        )
        self.assertEqual(
            received[1].conversation_id,
            whatsapp_conversation.id,
        )

        db.close()

    def test_imap_external_id_is_stable_without_message_id(self):
        self.assertEqual(
            _normalized_email_external_id("INBOX", "999", "42"),
            "INBOX:999:42",
        )
        self.assertEqual(
            _normalized_email_external_id("INBOX", None, "42"),
            "INBOX:unknown:42",
        )

    def test_imap_duplicate_detection_works_without_message_id(self):
        db = self.Session()

        external_id = _normalized_email_external_id(
            "INBOX",
            "12345",
            "77",
        )

        email = Email(
            company_id=1,
            external_id=external_id,
            message_id=None,
            imap_mailbox="INBOX",
            imap_uidvalidity="12345",
            imap_uid="77",
            sender="cliente@example.com",
            subject="Pedido sin Message-ID",
            body="10 cajas",
        )
        db.add(email)
        db.commit()

        duplicate = _existing_email_for_imap(
            db,
            company_id=1,
            mailbox="INBOX",
            uidvalidity="12345",
            uid="77",
            message_id=None,
            external_id=external_id,
        )

        other_tenant = _existing_email_for_imap(
            db,
            company_id=2,
            mailbox="INBOX",
            uidvalidity="12345",
            uid="77",
            message_id=None,
            external_id=external_id,
        )

        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate.id, email.id)
        self.assertIsNone(other_tenant)

        db.close()


    def test_message_link_cannot_touch_conversation_from_other_tenant(self):
        db = self.Session()

        tenant_one, _ = upsert_inbound_message(
            db,
            company_id=1,
            channel_key="email",
            provider="imap",
            external_id="tenant-one-message",
            sender="a@example.com",
            text_content="pedido",
        )

        tenant_two, conversation_two = upsert_inbound_message(
            db,
            company_id=2,
            channel_key="email",
            provider="imap",
            external_id="tenant-two-message",
            sender="b@example.com",
            text_content="pedido",
        )

        db.commit()

        link_message_to_order(
            db,
            tenant_one,
            999,
        )

        conversation = db.get(Conversation, conversation_two.id)

        self.assertEqual(conversation.company_id, 2)

        db.close()


if __name__ == "__main__":
    unittest.main()
