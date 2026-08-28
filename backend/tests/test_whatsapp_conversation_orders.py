from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import Company, Conversation, InboundMessage, InputChannel, Order
from app.whatsapp.conversation_orders import (
    build_transcript,
    evaluate_conversation_order,
    has_explicit_order_close,
)


class WhatsAppConversationOrderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        db.add(Company(id=1, name="Test"))
        db.add(
            InputChannel(
                id=1,
                company_id=1,
                key="whatsapp",
                name="WhatsApp",
                channel_type="message",
            )
        )
        db.add(
            Conversation(
                id=1,
                company_id=1,
                channel_id=1,
                provider="meta",
                external_thread_id="+34600000000",
            )
        )
        db.commit()
        db.close()

    def _message(
        self,
        db,
        message_id: int,
        text: str,
        *,
        direction: str = "inbound",
        received_at: datetime | None = None,
    ) -> InboundMessage:
        message = InboundMessage(
            id=message_id,
            company_id=1,
            channel_id=1,
            provider="meta",
            conversation_id=1,
            source_external_id=f"wa-{message_id}",
            direction=direction,
            sender="+34600000000",
            original_content=text,
            content_type="text",
            received_at=received_at or datetime.now(timezone.utc),
        )
        db.add(message)
        db.flush()
        return message

    def test_explicit_close_detection(self):
        self.assertTrue(has_explicit_order_close("Nada más, gracias"))
        self.assertTrue(has_explicit_order_close("Eso es todo"))
        self.assertTrue(has_explicit_order_close("Confirma el pedido"))
        self.assertFalse(has_explicit_order_close("Ponme 4 cajas de tomate"))

    def test_transcript_preserves_roles_and_order(self):
        db = self.Session()
        first = self._message(db, 1, "Ponme tomate", direction="inbound")
        second = self._message(db, 2, "¿Cuántas cajas?", direction="outbound")

        transcript = build_transcript([first, second])

        self.assertEqual(
            transcript,
            "CLIENTE: Ponme tomate\nEMPRESA: ¿Cuántas cajas?",
        )
        db.close()

    def test_transcript_uses_processed_attachment_text(self):
        message = SimpleNamespace(
            direction="inbound",
            original_content="",
            attachments=[
                SimpleNamespace(
                    extracted_text=None,
                    ocr_text=None,
                    transcription_text="Ponme cuatro cajas de tomate",
                )
            ],
        )

        transcript = build_transcript([message])

        self.assertEqual(
            transcript,
            "CLIENTE: Ponme cuatro cajas de tomate",
        )

    def test_open_conversation_stays_collecting(self):
        db = self.Session()
        self._message(db, 1, "Ponme 4 cajas de tomate")
        latest = self._message(db, 2, "Y dos de calabacín")
        db.commit()

        result = evaluate_conversation_order(db, message=latest)

        self.assertEqual(result.state, "collecting")
        self.assertIsNone(result.closing_message_id)
        self.assertEqual(len(result.messages), 2)
        db.close()

    def test_explicit_close_marks_conversation_ready(self):
        db = self.Session()
        self._message(db, 1, "Ponme 4 cajas de tomate")
        latest = self._message(db, 2, "Nada más, gracias")
        db.commit()

        result = evaluate_conversation_order(db, message=latest)

        self.assertEqual(result.state, "ready")
        self.assertEqual(result.closing_message_id, latest.id)
        self.assertIn("CLIENTE: Ponme 4 cajas de tomate", result.transcript)
        self.assertIn("CLIENTE: Nada más, gracias", result.transcript)
        db.close()

    def test_earlier_message_does_not_see_future_close(self):
        db = self.Session()
        now = datetime.now(timezone.utc)

        first = self._message(
            db,
            1,
            "Ponme 4 cajas de tomate",
            received_at=now,
        )
        self._message(
            db,
            2,
            "Nada más, gracias",
            received_at=now + timedelta(seconds=10),
        )
        db.commit()

        result = evaluate_conversation_order(db, message=first)

        self.assertEqual(result.state, "collecting")
        self.assertEqual(len(result.messages), 1)
        self.assertNotIn("Nada más", result.transcript)
        db.close()


    def test_previous_order_is_not_included_in_new_context(self):
        db = self.Session()
        now = datetime.now(timezone.utc)

        self._message(
            db,
            1,
            "Pedido anterior",
            received_at=now - timedelta(minutes=10),
        )

        db.add(
            Order(
                id=1,
                company_id=1,
                conversation_id=1,
                status="confirmed",
                created_at=now - timedelta(minutes=5),
            )
        )

        latest = self._message(
            db,
            2,
            "Ponme dos cajas de limones",
            received_at=now,
        )
        db.commit()

        result = evaluate_conversation_order(db, message=latest)

        self.assertEqual(len(result.messages), 1)
        self.assertNotIn("Pedido anterior", result.transcript)
        self.assertIn("limones", result.transcript)
        db.close()




class WhatsAppConversationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        db.add(Company(id=1, name="Test"))
        db.add(
            InputChannel(
                id=1,
                company_id=1,
                key="whatsapp",
                name="WhatsApp",
                channel_type="message",
            )
        )
        db.add(
            Conversation(
                id=1,
                company_id=1,
                channel_id=1,
                provider="meta",
                external_thread_id="+34600000000",
            )
        )
        db.commit()
        db.close()

    def _message(self, db, message_id: int, text: str) -> InboundMessage:
        message = InboundMessage(
            id=message_id,
            company_id=1,
            channel_id=1,
            provider="meta",
            conversation_id=1,
            source_external_id=f"wa-worker-{message_id}",
            direction="inbound",
            sender="+34600000000",
            original_content=text,
            content_type="text",
            received_at=datetime.now(timezone.utc) + timedelta(seconds=message_id),
        )
        db.add(message)
        db.flush()
        return message

    def test_partial_whatsapp_does_not_call_pipeline(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from app.workers.jobs_worker import _process_job

        db = self.Session()
        message = self._message(db, 10, "Ponme 4 cajas de tomate")
        db.commit()

        job = SimpleNamespace(
            job_type="process_inbound_message",
            company_id=1,
            created_by_user_id=None,
            payload_json='{"inbound_message_id": %d, "channel": "whatsapp"}' % message.id,
        )

        with patch(
            "app.workers.jobs_worker.AgentProcessingService"
        ) as processing_service:
            pipeline = MagicMock()
            processing_service.return_value.pipeline = pipeline

            result = _process_job(db, job)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "collecting")
        pipeline.process_inbound_message.assert_not_called()

        db.refresh(message)
        self.assertEqual(message.processing_step, "whatsapp_order_collecting")
        self.assertIsNone(message.order_id)
        db.close()

    def test_manual_import_whatsapp_bypasses_live_conversation_gate(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from app.workers.jobs_worker import _process_job

        db = self.Session()
        message = self._message(db, 30, "Pedido importado manualmente")
        message.provider = "manual_import"
        db.commit()

        job = SimpleNamespace(
            job_type="process_inbound_message",
            company_id=1,
            created_by_user_id=None,
            payload_json='{"inbound_message_id": %d, "channel": "whatsapp"}' % message.id,
        )

        pipeline = MagicMock()
        pipeline.process_inbound_message.return_value = {
            "ok": True,
            "status": "no_order",
        }

        with patch(
            "app.workers.jobs_worker.AgentProcessingService"
        ) as processing_service:
            processing_service.return_value.pipeline = pipeline

            result = _process_job(db, job)

        self.assertTrue(result["ok"])
        pipeline.process_inbound_message.assert_called_once_with(db, message)
        db.close()


    def test_closing_message_processes_full_transcript_and_links_messages(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from app.workers.jobs_worker import _process_job

        db = self.Session()
        first = self._message(db, 20, "Ponme 4 cajas de tomate")
        closing = self._message(db, 21, "Nada más, gracias")
        db.commit()

        job = SimpleNamespace(
            job_type="process_inbound_message",
            company_id=1,
            created_by_user_id=None,
            payload_json='{"inbound_message_id": %d, "channel": "whatsapp"}' % closing.id,
        )

        pipeline = MagicMock()
        pipeline.process_inbound_message.return_value = {
            "ok": True,
            "status": "order_detected",
            "order_id": 77,
            "score": 95,
        }

        with patch(
            "app.workers.jobs_worker.AgentProcessingService"
        ) as processing_service:
            processing_service.return_value.pipeline = pipeline

            result = _process_job(db, job)

        self.assertTrue(result["ok"])
        self.assertEqual(result["order_id"], 77)

        args, kwargs = pipeline.process_inbound_message.call_args
        self.assertEqual(args[1].id, closing.id)
        self.assertIn(
            "CLIENTE: Ponme 4 cajas de tomate",
            kwargs["source_text_override"],
        )
        self.assertIn(
            "CLIENTE: Nada más, gracias",
            kwargs["source_text_override"],
        )

        db.refresh(first)
        db.refresh(closing)
        self.assertEqual(first.order_id, 77)
        self.assertEqual(closing.order_id, 77)
        db.close()


if __name__ == "__main__":
    unittest.main()
