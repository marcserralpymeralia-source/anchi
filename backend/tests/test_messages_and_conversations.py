from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import selectinload, sessionmaker

os.environ.setdefault("APP_ENV", "development")

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.services import AgentProcessingService  # noqa: E402
from app.channels.service import get_or_create_channel  # noqa: E402
from app.core.encryption import encrypt_secret  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.db.models import Conversation, DecisionSettings, Email, EmailSettings, ExportJob, InboundMessage, LLMSettings, Order, OrderLine, OrderReview, ScoringSettings  # noqa: E402
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

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
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


    def test_force_reprocess_reuses_existing_order(self):
        db = self.Session()

        email = Email(
            company_id=1,
            external_id="mail-reprocess-1",
            sender="cliente@example.com",
            subject="Pedido",
            body="Pedido inicial",
        )
        db.add(email)
        db.commit()

        channel = get_or_create_channel(db, 1, "email")
        channel.is_active = True
        db.commit()

        calls = {"count": 0}

        def fake_process(_self, session, inbound_message, user=None, force_order=False, email=None):
            calls["count"] += 1

            if not force_order and inbound_message.order_id:
                order = session.get(Order, inbound_message.order_id)
                return {
                    "ok": True,
                    "status": "order_detected",
                    "message": f"Pedido {order.id} ya habia sido creado.",
                    "order_id": order.id,
                    "score": order.score,
                }

            existing_order = session.get(Order, inbound_message.order_id) if inbound_message.order_id else None

            if existing_order:
                order = existing_order
                order.lines.clear()
                session.flush()
            else:
                order = Order(
                    company_id=inbound_message.company_id,
                    email_id=email.id if email else None,
                    customer_detected_name="Cliente demo",
                    status="pedido_pendiente_revision",
                    score=91,
                )
                session.add(order)
                session.flush()

            session.add(
                OrderLine(
                    company_id=inbound_message.company_id,
                    order_id=order.id,
                    original_text="Linea reprocesada" if force_order else "Linea inicial",
                    quantity=2 if force_order else 1,
                    unit="cajas",
                    extraction_confidence=0.95,
                    validation_status="validated",
                )
            )

            inbound_message.order_id = order.id
            inbound_message.status = "order_detected"
            inbound_message.processing_step = "completed"
            session.commit()

            return {
                "ok": True,
                "status": "order_detected",
                "message": f"Pedido {order.id} procesado.",
                "order_id": order.id,
                "score": 91,
            }

        with patch(
            "app.agent.platform.UnifiedOrderPipelineService.process_inbound_message",
            new=fake_process,
        ):
            first = AgentProcessingService().process_email(db, email)
            original_order_id = first["order_id"]

            second = AgentProcessingService().process_email(
                db,
                email,
                force_order=True,
            )

        self.assertEqual(original_order_id, second["order_id"])
        self.assertEqual(
            db.scalar(select(func.count()).select_from(Order)),
            1,
        )

        order = db.scalar(
            select(Order)
            .where(Order.id == original_order_id)
            .options(selectinload(Order.lines))
        )

        self.assertEqual(len(order.lines), 1)
        self.assertEqual(order.lines[0].original_text, "Linea reprocesada")
        self.assertEqual(order.lines[0].quantity, 2)
        self.assertEqual(calls["count"], 2)

        inbound = db.scalar(
            select(InboundMessage).where(
                InboundMessage.company_id == 1,
                InboundMessage.source_external_id == "mail-reprocess-1",
            )
        )
        self.assertEqual(inbound.order_id, original_order_id)

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

    def test_process_email_rejects_disabled_email_channel(self):
        db = self.Session()
        try:
            email = Email(
                company_id=1,
                external_id="disabled-email-1",
                sender="cliente@example.com",
                subject="Pedido",
                body="10 cajas",
            )
            db.add(email)
            db.flush()

            channel = get_or_create_channel(db, 1, "email")
            channel.is_active = False
            db.flush()

            with self.assertRaisesRegex(
                ValueError,
                "Email channel is disabled for this tenant",
            ):
                AgentProcessingService().process_email(db, email)
        finally:
            db.close()


    def _run_auto_confirm_case(
        self,
        *,
        channel_key: str,
        decision_human_review: bool,
        email_human_review: bool = False,
        allow_auto_export: bool = False,
    ):
        db = self.Session()

        try:
            db.add(
                LLMSettings(
                    company_id=1,
                    provider="openai",
                    api_key_encrypted=encrypt_secret("test-token"),
                    allow_auto_confirm=True,
                    allow_auto_export=allow_auto_export,
                )
            )
            db.add(
                DecisionSettings(
                    company_id=1,
                    always_human_review=decision_human_review,
                )
            )
            db.add(
                EmailSettings(
                    company_id=1,
                    always_human_review=email_human_review,
                )
            )
            db.add(
                ScoringSettings(
                    company_id=1,
                    safe_threshold=90,
                    review_threshold=75,
                    doubtful_threshold=50,
                    block_without_customer=False,
                    block_without_reference=False,
                    block_without_quantity=False,
                    block_below_threshold=False,
                )
            )
            db.commit()

            email = None
            if channel_key == "email":
                email = Email(
                    company_id=1,
                    external_id="auto-confirm-email-1",
                    sender="cliente@example.com",
                    subject="Pedido automático",
                    body="Pedido de prueba con contenido suficiente",
                )
                db.add(email)
                db.commit()

            message, _ = persist_normalized_message(
                db,
                NormalizedMessage(
                    company_id=1,
                    channel_key=channel_key,
                    provider="imap" if channel_key == "email" else "whatsapp",
                    external_id=f"auto-confirm-{channel_key}-1",
                    sender="cliente@example.com" if channel_key == "email" else "34600000000",
                    recipients=["pedidos@example.com"] if channel_key == "email" else ["34900000000"],
                    subject="Pedido automático",
                    text_content="Pedido de prueba con contenido suficiente para procesar",
                    external_thread_id=f"thread-auto-confirm-{channel_key}",
                    metadata={"source": channel_key},
                ),
                content_type="email" if channel_key == "email" else "whatsapp_text",
            )
            db.commit()

            pipeline = UnifiedOrderPipelineService()

            def fake_create_order(
                session,
                inbound_message,
                current_email,
                extraction,
                normalized,
                existing_order=None,
            ):
                order = existing_order or Order(
                    company_id=inbound_message.company_id,
                    conversation_id=inbound_message.conversation_id,
                    email_id=current_email.id if current_email else None,
                    customer_detected_name="Cliente demo",
                    status="pedido_pendiente_revision",
                )
                session.add(order)
                session.flush()
                return order

            with (
                patch.object(
                    pipeline,
                    "_classify",
                    return_value={
                        "tipo_correo": "pedido",
                        "confianza": 0.99,
                    },
                ),
                patch.object(
                    pipeline,
                    "_extract",
                    return_value={
                        "customer": {},
                        "lines": [],
                    },
                ),
                patch.object(
                    pipeline,
                    "_create_order",
                    side_effect=fake_create_order,
                ),
                patch.object(
                    pipeline.scoring,
                    "score_order",
                    return_value=SimpleNamespace(total_score=95.0),
                ),
            ):
                result = pipeline.process_inbound_message(
                    db,
                    message,
                    email=email,
                )

            order = db.get(Order, result["order_id"])
            review_count = db.scalar(
                select(func.count()).select_from(OrderReview)
            )
            export_count = db.scalar(
                select(func.count()).select_from(ExportJob)
            )

            return {
                "result": result,
                "order_status": order.status,
                "confirmed_at": order.confirmed_at,
                "review_count": review_count,
                "export_count": export_count,
            }
        finally:
            db.close()

    def test_safe_whatsapp_auto_confirms_without_pending_review(self):
        outcome = self._run_auto_confirm_case(
            channel_key="whatsapp",
            decision_human_review=False,
        )

        self.assertTrue(outcome["result"]["ok"])
        self.assertTrue(outcome["result"]["auto_confirmed"])
        self.assertIsNone(outcome["result"]["review_id"])
        self.assertEqual(outcome["order_status"], "pedido_confirmado")
        self.assertIsNotNone(outcome["confirmed_at"])
        self.assertEqual(outcome["review_count"], 0)

    def test_global_human_review_blocks_auto_confirmation(self):
        outcome = self._run_auto_confirm_case(
            channel_key="whatsapp",
            decision_human_review=True,
        )

        self.assertTrue(outcome["result"]["ok"])
        self.assertFalse(outcome["result"]["auto_confirmed"])
        self.assertIsNotNone(outcome["result"]["review_id"])
        self.assertEqual(
            outcome["order_status"],
            "pedido_pendiente_revision",
        )
        self.assertIsNone(outcome["confirmed_at"])
        self.assertEqual(outcome["review_count"], 1)

    def test_email_human_review_blocks_auto_confirmation(self):
        outcome = self._run_auto_confirm_case(
            channel_key="email",
            decision_human_review=False,
            email_human_review=True,
        )

        self.assertTrue(outcome["result"]["ok"])
        self.assertFalse(outcome["result"]["auto_confirmed"])
        self.assertIsNotNone(outcome["result"]["review_id"])
        self.assertEqual(
            outcome["order_status"],
            "pedido_pendiente_revision",
        )
        self.assertIsNone(outcome["confirmed_at"])
        self.assertEqual(outcome["review_count"], 1)

    def test_auto_export_does_not_queue_unconfirmed_order(self):
        outcome = self._run_auto_confirm_case(
            channel_key="whatsapp",
            decision_human_review=True,
            allow_auto_export=True,
        )

        self.assertFalse(outcome["result"]["auto_confirmed"])
        self.assertEqual(
            outcome["order_status"],
            "pedido_pendiente_revision",
        )
        self.assertEqual(outcome["review_count"], 1)
        self.assertEqual(outcome["export_count"], 0)


if __name__ == "__main__":
    unittest.main()
