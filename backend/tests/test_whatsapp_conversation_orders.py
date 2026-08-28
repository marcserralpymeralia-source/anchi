from __future__ import annotations

import unittest
import unittest.mock
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
    is_short_order_confirmation,
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

    def test_short_confirmation_detection_is_conservative(self):
        self.assertTrue(is_short_order_confirmation("Sí"))
        self.assertTrue(is_short_order_confirmation("Correcto"))
        self.assertTrue(is_short_order_confirmation("Adelante"))
        self.assertFalse(is_short_order_confirmation("Sí, pero añade dos cajas"))
        self.assertFalse(is_short_order_confirmation("Sí quiero cuatro cajas"))

    def test_confirmation_after_ready_prompt_releases_order(self):
        db = self.Session()
        now = datetime.now(timezone.utc)

        self._message(
            db,
            1,
            "Ponme 4 cajas de tomate",
            received_at=now,
        )
        outbound = self._message(
            db,
            2,
            "¿Confirmas 4 cajas de tomate?",
            direction="outbound",
            received_at=now + timedelta(seconds=1),
        )
        outbound.raw_payload_json = (
            '{"auto_response": true, '
            '"semantic_state": "ready_for_confirmation", '
            '"trigger_message_id": 1}'
        )
        confirmation = self._message(
            db,
            3,
            "Sí",
            received_at=now + timedelta(seconds=2),
        )
        db.commit()

        evaluator = unittest.mock.MagicMock()

        result = evaluate_conversation_order(
            db,
            message=confirmation,
            semantic_evaluator=evaluator,
        )

        self.assertEqual(result.state, "ready")
        self.assertEqual(result.closing_message_id, confirmation.id)
        evaluator.assert_not_called()
        db.close()

    def test_confirmation_without_ready_prompt_does_not_release_order(self):
        db = self.Session()
        latest = self._message(db, 1, "Sí")
        db.commit()

        evaluator = unittest.mock.MagicMock(
            return_value=SimpleNamespace(
                intent="other",
                state="collecting",
                missing_or_uncertain=[],
                reply_needed=False,
                suggested_reply="",
                confidence=0.7,
                prompt_execution_id=300,
            )
        )

        result = evaluate_conversation_order(
            db,
            message=latest,
            semantic_evaluator=evaluator,
        )

        self.assertEqual(result.state, "collecting")
        evaluator.assert_called_once()
        db.close()

    def test_confirmation_with_modification_does_not_release_order(self):
        db = self.Session()
        now = datetime.now(timezone.utc)

        self._message(
            db,
            1,
            "Ponme 4 cajas de tomate",
            received_at=now,
        )
        outbound = self._message(
            db,
            2,
            "¿Confirmas 4 cajas de tomate?",
            direction="outbound",
            received_at=now + timedelta(seconds=1),
        )
        outbound.raw_payload_json = (
            '{"auto_response": true, '
            '"semantic_state": "ready_for_confirmation", '
            '"trigger_message_id": 1}'
        )
        latest = self._message(
            db,
            3,
            "Sí, pero añade dos cajas de calabacín",
            received_at=now + timedelta(seconds=2),
        )
        db.commit()

        evaluator = unittest.mock.MagicMock(
            return_value=SimpleNamespace(
                intent="order",
                state="ready_for_confirmation",
                missing_or_uncertain=[],
                reply_needed=True,
                suggested_reply="¿Confirmas también las dos cajas de calabacín?",
                confidence=0.96,
                prompt_execution_id=301,
            )
        )

        result = evaluate_conversation_order(
            db,
            message=latest,
            semantic_evaluator=evaluator,
        )

        self.assertEqual(result.state, "collecting")
        self.assertEqual(result.semantic_state, "ready_for_confirmation")
        evaluator.assert_called_once()
        db.close()

    def test_old_explicit_close_does_not_close_later_message(self):
        db = self.Session()
        now = datetime.now(timezone.utc)

        self._message(
            db,
            1,
            "Confirma el pedido",
            received_at=now,
        )
        latest = self._message(
            db,
            2,
            "Espera, cambia el tomate por calabacín",
            received_at=now + timedelta(seconds=1),
        )
        db.commit()

        evaluator = unittest.mock.MagicMock(
            return_value=SimpleNamespace(
                intent="order",
                state="needs_clarification",
                missing_or_uncertain=["pedido modificado"],
                reply_needed=True,
                suggested_reply="¿Qué cantidad de calabacín necesitas?",
                confidence=0.9,
                prompt_execution_id=302,
            )
        )

        result = evaluate_conversation_order(
            db,
            message=latest,
            semantic_evaluator=evaluator,
        )

        self.assertEqual(result.state, "collecting")
        self.assertEqual(result.semantic_state, "needs_clarification")
        evaluator.assert_called_once()
        db.close()


    def test_semantic_clarification_is_exposed_without_releasing_pipeline(self):
        db = self.Session()
        latest = self._message(db, 1, "Ponme 4 de tomate")
        db.commit()

        evaluator = unittest.mock.MagicMock(
            return_value=SimpleNamespace(
                intent="order",
                state="needs_clarification",
                missing_or_uncertain=["unidad de tomate"],
                reply_needed=True,
                suggested_reply="¿Las 4 de tomate son cajas o unidades?",
                confidence=0.93,
                prompt_execution_id=101,
            )
        )

        result = evaluate_conversation_order(
            db,
            message=latest,
            semantic_evaluator=evaluator,
        )

        self.assertEqual(result.state, "collecting")
        self.assertEqual(result.semantic_state, "needs_clarification")
        self.assertEqual(result.semantic_intent, "order")
        self.assertEqual(result.missing_or_uncertain, ["unidad de tomate"])
        self.assertTrue(result.reply_needed)
        self.assertIn("cajas o unidades", result.suggested_reply)
        self.assertEqual(result.semantic_confidence, 0.93)
        self.assertEqual(result.prompt_execution_id, 101)

        evaluator.assert_called_once()
        _, kwargs = evaluator.call_args
        self.assertEqual(kwargs["company_id"], 1)
        self.assertIn("CLIENTE: Ponme 4 de tomate", kwargs["transcript"])
        self.assertEqual(
            kwargs["input_reference"],
            f"conversation:1:message:{latest.id}",
        )
        db.close()

    def test_semantic_ready_for_confirmation_does_not_create_ready_state_yet(self):
        db = self.Session()
        latest = self._message(
            db,
            1,
            "Ponme 4 cajas de tomate y 2 cajas de calabacín",
        )
        db.commit()

        evaluator = unittest.mock.MagicMock(
            return_value=SimpleNamespace(
                intent="order",
                state="ready_for_confirmation",
                missing_or_uncertain=[],
                reply_needed=True,
                suggested_reply="¿Confirmas 4 cajas de tomate y 2 de calabacín?",
                confidence=0.97,
                prompt_execution_id=102,
            )
        )

        result = evaluate_conversation_order(
            db,
            message=latest,
            semantic_evaluator=evaluator,
        )

        self.assertEqual(result.state, "collecting")
        self.assertEqual(result.semantic_state, "ready_for_confirmation")
        self.assertTrue(result.reply_needed)
        self.assertEqual(result.missing_or_uncertain, [])
        db.close()

    def test_explicit_close_keeps_priority_over_semantic_evaluation(self):
        db = self.Session()
        latest = self._message(db, 1, "Nada más, confirma el pedido")
        db.commit()

        evaluator = unittest.mock.MagicMock()

        result = evaluate_conversation_order(
            db,
            message=latest,
            semantic_evaluator=evaluator,
        )

        self.assertEqual(result.state, "ready")
        self.assertEqual(result.closing_message_id, latest.id)
        self.assertIsNone(result.semantic_state)
        evaluator.assert_not_called()
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

        semantic_result = SimpleNamespace(
            intent="order",
            state="collecting",
            missing_or_uncertain=[],
            reply_needed=False,
            suggested_reply="",
            confidence=0.85,
            prompt_execution_id=150,
        )

        with patch(
            "app.workers.jobs_worker.evaluate_whatsapp_conversation_semantics",
            return_value=semantic_result,
        ), patch(
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

    def test_semantic_clarification_sends_mocked_auto_response(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.workers.jobs_worker import _process_job

        db = self.Session()
        message = self._message(db, 40, "Ponme 4 de tomate")
        db.commit()

        job = SimpleNamespace(
            job_type="process_inbound_message",
            company_id=1,
            created_by_user_id=None,
            payload_json='{"inbound_message_id": %d, "channel": "whatsapp"}' % message.id,
        )

        semantic_result = SimpleNamespace(
            intent="order",
            state="needs_clarification",
            missing_or_uncertain=["unidad de tomate"],
            reply_needed=True,
            suggested_reply="¿Las 4 de tomate son cajas o unidades?",
            confidence=0.95,
            prompt_execution_id=200,
        )

        with patch(
            "app.workers.jobs_worker.whatsapp_config",
            return_value=SimpleNamespace(bot_enabled=True),
        ), patch(
            "app.workers.jobs_worker.evaluate_whatsapp_conversation_semantics",
            return_value=semantic_result,
        ), patch(
            "app.workers.jobs_worker.send_automatic_response",
            new=AsyncMock(
                return_value={
                    "sent": True,
                    "skipped": False,
                    "reason": "sent",
                    "message_id": 999,
                }
            ),
        ) as sender, patch(
            "app.workers.jobs_worker.AgentProcessingService"
        ) as processing_service:
            pipeline = MagicMock()
            processing_service.return_value.pipeline = pipeline

            result = _process_job(db, job)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["semantic_state"], "needs_clarification")
        self.assertTrue(result["auto_response"]["sent"])
        pipeline.process_inbound_message.assert_not_called()
        sender.assert_awaited_once()
        db.close()

    def test_bot_disabled_skips_semantic_evaluation_and_auto_response(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from app.workers.jobs_worker import _process_job

        db = self.Session()
        message = self._message(db, 41, "Ponme 4 de tomate")
        db.commit()

        job = SimpleNamespace(
            job_type="process_inbound_message",
            company_id=1,
            created_by_user_id=None,
            payload_json='{"inbound_message_id": %d, "channel": "whatsapp"}' % message.id,
        )

        with patch(
            "app.workers.jobs_worker.whatsapp_config",
            return_value=SimpleNamespace(bot_enabled=False),
        ), patch(
            "app.workers.jobs_worker.evaluate_whatsapp_conversation_semantics"
        ) as semantic, patch(
            "app.workers.jobs_worker.send_automatic_response"
        ) as sender, patch(
            "app.workers.jobs_worker.AgentProcessingService"
        ) as processing_service:
            pipeline = MagicMock()
            processing_service.return_value.pipeline = pipeline

            result = _process_job(db, job)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "collecting")
        semantic.assert_not_called()
        sender.assert_not_called()
        pipeline.process_inbound_message.assert_not_called()
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
