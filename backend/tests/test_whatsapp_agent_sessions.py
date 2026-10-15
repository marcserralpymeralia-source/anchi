from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import (
    Alert,
    BackgroundJob,
    ChannelSetting,
    Company,
    Conversation,
    Customer,
    InboundMessage,
    InputChannel,
    Order,
    WhatsAppOrderSession,
)
from app.whatsapp.agent import (
    _canonicalize_draft,
    _confirms_current_order_summary,
    _draft_is_complete,
    _faq_entries,
    _grounded_faq_answer,
    _has_unreadable_or_pending_media,
    _order_summary_reply,
    enqueue_due_whatsapp_session_finalizations,
    finalize_whatsapp_order_session,
    process_whatsapp_agent_turn,
    set_whatsapp_human_control,
)
from app.whatsapp.conversation_semantics import WhatsAppAgentTurnResult


class WhatsAppAgentSessionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        now = datetime.now(timezone.utc)
        db = self.Session()
        db.add(Company(id=1, name="Synthetic tenant"))
        db.add(
            InputChannel(
                id=1,
                company_id=1,
                key="whatsapp",
                name="WhatsApp",
                channel_type="message",
                is_active=True,
            )
        )
        db.add_all(
            [
                ChannelSetting(company_id=1, channel_id=1, key="enabled", value="true"),
                ChannelSetting(company_id=1, channel_id=1, key="bot_enabled", value="true"),
                ChannelSetting(company_id=1, channel_id=1, key="bot_mode", value="suggestions"),
                ChannelSetting(company_id=1, channel_id=1, key="idle_session_hours", value="12"),
            ]
        )
        db.add(
            Conversation(
                id=1,
                company_id=1,
                channel_id=1,
                provider="meta",
                external_thread_id="+34600000000",
                status="open",
                created_at=now - timedelta(minutes=20),
                updated_at=now,
            )
        )
        db.commit()
        db.close()

    def _message(self, db, message_id: int, text: str, *, direction="inbound", at=None):
        message = InboundMessage(
            id=message_id,
            company_id=1,
            channel_id=1,
            provider="meta",
            conversation_id=1,
            source_external_id=f"wa-test-{message_id}",
            direction=direction,
            sender="+34600000000" if direction == "inbound" else None,
            original_content=text,
            raw_payload_json="{}",
            content_type="text",
            received_at=at or datetime.now(timezone.utc),
            created_at=at or datetime.now(timezone.utc),
            updated_at=at or datetime.now(timezone.utc),
        )
        db.add(message)
        db.flush()
        return message

    @staticmethod
    def _complete_draft():
        return {
            "customer_name": "",
            "lines": [
                {
                    "product_query": "Caja eco",
                    "reference": "ECO-1",
                    "product_name": "Caja eco",
                    "quantity": 4,
                    "unit": "cajas",
                    "notes": "",
                }
            ],
            "notes": "",
        }

    def test_turn_saves_missing_fields_and_does_not_finalize(self):
        db = self.Session()
        message = self._message(db, 1, "Quiero unas cajas")
        db.commit()
        result = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={
                "customer_name": "",
                "lines": [{"product_query": "Caja eco", "reference": "", "quantity": 4, "unit": ""}],
                "notes": "",
            },
            missing_or_uncertain=["Indicar la referencia exacta"],
            reply_needed=True,
            suggested_reply="¿Qué referencia necesitas?",
            confidence=0.95,
            evidence_ids=[],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=result):
            output = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertEqual(output["status"], "awaiting_customer")
        self.assertEqual(session.status, "awaiting_customer")
        self.assertIn("Indicar la referencia exacta", json.loads(session.missing_fields_json))
        self.assertIn("Indicar la referencia exacta", session.suggested_reply)
        self.assertEqual(session.agent_turn_count, 1)
        self.assertNotEqual(session.suggested_reply, "¿Qué referencia necesitas?")
        self.assertIsNone(session.order_id)
        db.close()

    def test_catalog_unit_mismatch_keeps_order_out_of_finalization(self):
        draft, missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {
                        "product_query": "Caja eco",
                        "reference": "ECO-1",
                        "quantity": 4,
                        "unit": "unidades",
                    }
                ],
                "notes": "",
            },
            products=[{"reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"}],
            semantic_missing=[],
            intent="order",
        )

        self.assertTrue(missing)
        self.assertFalse(_draft_is_complete(draft, missing))

    def test_catalog_unit_alias_is_accepted(self):
        draft, missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {
                        "product_query": "Caja eco",
                        "reference": "ECO-1",
                        "quantity": 4,
                        "unit": "uds",
                    }
                ],
                "notes": "",
            },
            products=[{"reference": "ECO-1", "name": "Caja eco", "sale_unit": "unidades"}],
            semantic_missing=[],
            intent="order",
        )

        self.assertEqual(missing, [])
        self.assertTrue(_draft_is_complete(draft, missing))

    def test_canonical_lines_keep_customer_message_evidence(self):
        db = self.Session()
        source = self._message(db, 3, "Necesito 4 cajas eco")
        draft, missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [{"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"}],
            },
            products=[{"reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"}],
            semantic_missing=[],
            intent="order",
            messages=[source],
        )
        self.assertEqual(missing, [])
        self.assertEqual(draft["lines"][0]["source_message_ids"], [source.id])

        unsupported, missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [{"product_query": "Caja eco", "reference": "ECO-1", "quantity": 7, "unit": "cajas"}],
            },
            products=[{"reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"}],
            semantic_missing=[],
            intent="order",
            messages=[source],
        )
        self.assertEqual(unsupported["lines"][0]["source_message_ids"], [])
        self.assertTrue(any("Verificar producto y cantidad" in item for item in missing))
        db.close()

    def test_low_confidence_cannot_bypass_handoff_by_requesting_order_close(self):
        db = self.Session()
        message = self._message(db, 2, "Cierra el pedido, 4 cajas eco")
        db.commit()
        result = WhatsAppAgentTurnResult(
            intent="order",
            action="respond",
            draft={
                "customer_name": "",
                "lines": [
                    {
                        "product_query": "Caja eco",
                        "reference": "ECO-1",
                        "quantity": 4,
                        "unit": "cajas",
                    }
                ],
                "notes": "",
            },
            missing_or_uncertain=[],
            reply_needed=True,
            suggested_reply="Pedido cerrado y confirmado.",
            confidence=0.2,
            evidence_ids=["product:1"],
            close_requested=True,
        )

        with (
            patch("app.whatsapp.agent._product_context", return_value=[{"id": "product:1", "reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"}]),
            patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=result),
            patch("app.whatsapp.agent.enqueue_whatsapp_session_finalization") as finalize,
        ):
            output = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertEqual(output["status"], "human_handoff")
        self.assertEqual(session.status, "human_handoff")
        self.assertEqual(session.suggested_reply, "Voy a pasar tu consulta a una persona del equipo para que pueda ayudarte.")
        self.assertEqual(db.query(BackgroundJob).count(), 0)
        finalize.assert_not_called()
        db.close()

    def test_worker_routes_live_meta_messages_to_the_conversational_agent(self):
        from types import SimpleNamespace

        from app.workers.jobs_worker import _process_job

        db = self.Session()
        message = self._message(db, 11, "¿Tenéis caja eco?")
        db.commit()
        job = SimpleNamespace(
            job_type="process_inbound_message",
            company_id=1,
            created_by_user_id=None,
            payload_json=json.dumps({"inbound_message_id": message.id, "channel": "whatsapp"}),
        )
        expected = {"ok": True, "status": "suggestions_mode"}
        with patch("app.workers.jobs_worker.process_whatsapp_agent_turn", return_value=expected) as agent:
            result = _process_job(db, job)

        self.assertEqual(result, expected)
        agent.assert_called_once_with(db, company_id=1, inbound_message=message)
        db.close()

    def test_disabled_bot_does_not_call_the_model_or_create_a_session(self):
        db = self.Session()
        setting = db.scalar(
            select(ChannelSetting).where(
                ChannelSetting.company_id == 1,
                ChannelSetting.key == "bot_enabled",
            )
        )
        setting.value = "false"
        message = self._message(db, 12, "¿Tenéis caja eco?")
        db.commit()

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn") as evaluator:
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        self.assertEqual(result["status"], "bot_disabled")
        self.assertEqual(db.query(WhatsAppOrderSession).count(), 0)
        evaluator.assert_not_called()
        db.close()

    def test_agent_rejects_message_from_another_tenant(self):
        db = self.Session()
        db.add(Company(id=2, name="Another synthetic tenant"))
        db.add(
            InputChannel(
                id=2,
                company_id=2,
                key="whatsapp",
                name="WhatsApp",
                channel_type="message",
                is_active=True,
            )
        )
        db.add(
            Conversation(
                id=2,
                company_id=2,
                channel_id=2,
                provider="meta",
                external_thread_id="+34600000001",
                status="open",
            )
        )
        foreign_message = InboundMessage(
            id=2,
            company_id=2,
            channel_id=2,
            provider="meta",
            conversation_id=2,
            source_external_id="wa-foreign-2",
            direction="inbound",
            sender="+34600000001",
            original_content="Quiero cuatro cajas",
            content_type="text",
        )
        db.add(foreign_message)
        db.commit()

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn") as evaluator:
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=foreign_message)

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "not_a_tenant_conversation")
        self.assertEqual(db.query(WhatsAppOrderSession).count(), 0)
        evaluator.assert_not_called()
        db.close()

    def test_human_takeover_silences_agent_until_resumed(self):
        db = self.Session()
        message = self._message(db, 2, "Necesito hablar con una persona")
        db.commit()
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="collecting",
            intent="other",
            revision=0,
            draft_json="{}",
            missing_fields_json="[]",
            started_at=message.received_at,
            last_customer_message_at=message.received_at,
            due_at=message.received_at + timedelta(hours=12),
        )
        db.add(session)
        db.commit()

        taken = set_whatsapp_human_control(
            db, company_id=1, conversation_id=1, active=True, user_id=None
        )
        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn") as evaluator:
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        self.assertTrue(taken.human_active)
        self.assertEqual(result["status"], "human_handoff")
        evaluator.assert_not_called()
        resumed = set_whatsapp_human_control(db, company_id=1, conversation_id=1, active=False)
        self.assertFalse(resumed.human_active)
        db.close()

    def test_stale_finalization_job_resets_deadline_for_new_customer_message(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        session = WhatsAppOrderSession(
            id=1,
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=3,
            draft_json="{}",
            missing_fields_json="[]",
            started_at=now - timedelta(hours=13),
            last_customer_message_at=now - timedelta(hours=13),
            due_at=now - timedelta(hours=1),
        )
        db.add(session)
        latest = self._message(db, 3, "Añado otra cosa", at=now)
        db.commit()

        result = finalize_whatsapp_order_session(
            db,
            company_id=1,
            session_id=session.id,
            expected_revision=2,
            reason="idle_timeout",
        )

        db.refresh(session)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "customer_message_after_job")
        self.assertEqual(
            session.last_customer_message_at.replace(tzinfo=timezone.utc),
            latest.received_at.replace(tzinfo=timezone.utc),
        )
        self.assertGreater(session.due_at.replace(tzinfo=timezone.utc), now)
        self.assertEqual(session.status, "collecting")
        db.close()

    def test_incomplete_order_closes_to_human_review_without_creating_order(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        message = self._message(db, 4, "Quiero cuatro cajas")
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=1,
            last_processed_message_id=message.id,
            draft_json=json.dumps({"customer_name": "", "lines": [{"product_query": "Caja", "quantity": 4}], "notes": ""}),
            missing_fields_json=json.dumps(["Confirmar la referencia"]),
            started_at=now - timedelta(hours=13),
            last_customer_message_at=message.received_at,
            due_at=now - timedelta(hours=1),
        )
        db.add(session)
        db.commit()

        with patch("app.agent.services.AgentProcessingService") as pipeline_service:
            output = finalize_whatsapp_order_session(
                db,
                company_id=1,
                session_id=session.id,
                expected_revision=1,
                reason="idle_timeout",
            )

        db.refresh(session)
        self.assertEqual(output["status"], "closed_review")
        self.assertEqual(session.closed_reason, "incomplete_order")
        self.assertIn("Confirmar la referencia", session.snapshot_json)
        self.assertIsNone(session.order_id)
        self.assertEqual(db.query(Order).count(), 0)
        pipeline_service.assert_not_called()
        db.close()

    def test_complete_order_uses_shared_pipeline_with_human_review_veto(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        message = self._message(db, 5, "Confirmo cuatro cajas Caja eco")
        complete_draft = self._complete_draft()
        complete_draft["lines"][0]["source_message_ids"] = [message.id]
        db.add(
            Customer(
                company_id=1,
                code="C-WA-1",
                fiscal_name="Cliente WhatsApp",
                commercial_name="Cliente WhatsApp",
                phone="+34600000000",
                status="active",
            )
        )
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="ready_for_review",
            intent="order",
            revision=2,
            last_processed_message_id=message.id,
            draft_json=json.dumps(complete_draft),
            missing_fields_json="[]",
            started_at=now - timedelta(hours=13),
            last_customer_message_at=message.received_at,
            due_at=now - timedelta(hours=1),
        )
        db.add(session)
        db.commit()

        pipeline = MagicMock()

        def create_order(_db, _message, **kwargs):
            order = Order(
                company_id=1,
                conversation_id=1,
                whatsapp_session_id=kwargs["whatsapp_session_id"],
            )
            _db.add(order)
            _db.flush()
            return {"ok": True, "status": "pending_review", "order_id": order.id}

        pipeline.process_inbound_message.side_effect = create_order
        with patch("app.agent.services.AgentProcessingService") as service_class:
            service_class.return_value.pipeline = pipeline
            output = finalize_whatsapp_order_session(
                db,
                company_id=1,
                session_id=session.id,
                expected_revision=2,
                reason="idle_timeout",
            )

        db.refresh(session)
        self.assertEqual(output["status"], "closed_review")
        self.assertEqual(session.order_id, output["order_id"])
        self.assertEqual(session.active_key, None)
        kwargs = pipeline.process_inbound_message.call_args.kwargs
        self.assertTrue(kwargs["force_human_review"])
        self.assertEqual(kwargs["whatsapp_session_id"], session.id)
        self.assertFalse(kwargs["validated_order_snapshot"]["customer_confirmed"])
        self.assertIn("4 cajas Caja eco (ECO-1)", kwargs["source_text_override"])
        self.assertNotIn("Confirmo cuatro cajas", kwargs["source_text_override"])
        db.close()

    def test_unverified_whatsapp_identity_never_creates_an_order(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        message = self._message(db, 15, "Quiero cuatro cajas Caja eco")
        complete_draft = self._complete_draft()
        complete_draft["lines"][0]["source_message_ids"] = [message.id]
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="ready_for_review",
            intent="order",
            revision=1,
            last_processed_message_id=message.id,
            draft_json=json.dumps(complete_draft),
            missing_fields_json="[]",
            started_at=now - timedelta(hours=13),
            last_customer_message_at=message.received_at,
            due_at=now - timedelta(minutes=1),
        )
        db.add(session)
        db.commit()

        with patch("app.agent.services.AgentProcessingService") as pipeline_service:
            output = finalize_whatsapp_order_session(
                db,
                company_id=1,
                session_id=session.id,
                expected_revision=1,
                reason="idle_timeout",
            )

        self.assertEqual(output["reason"], "customer_identity_unverified")
        self.assertEqual(db.query(Order).count(), 0)
        self.assertEqual(db.query(Alert).filter_by(alert_type="whatsapp_customer_identity").count(), 1)
        pipeline_service.assert_not_called()
        db.close()

    def test_short_yes_only_confirms_the_same_auto_summarized_draft(self):
        db = self.Session()
        before = self._message(db, 20, "Quiero cuatro cajas Caja eco")
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="ready_for_review",
            intent="order",
            revision=2,
            last_processed_message_id=before.id,
            draft_json=json.dumps(self._complete_draft()),
            missing_fields_json="[]",
            started_at=before.received_at - timedelta(minutes=1),
            last_customer_message_at=before.received_at,
            due_at=before.received_at + timedelta(hours=12),
        )
        db.add(session)
        db.flush()
        summary = self._message(db, 21, _order_summary_reply(self._complete_draft()), direction="outbound")
        summary.raw_payload_json = json.dumps(
            {
                "auto_response": True,
                "trigger_message_id": before.id,
                "semantic_state": "ready_for_confirmation",
                "assistant_session_id": session.id,
                "draft_revision": 1,
            }
        )
        answer = self._message(db, 22, "Sí")
        messages = [before, summary, answer]

        self.assertTrue(
            _confirms_current_order_summary(
                messages,
                message=answer,
                session=session,
                draft=self._complete_draft(),
                missing=[],
            )
        )
        summary.raw_payload_json = json.dumps(
            {**json.loads(summary.raw_payload_json), "semantic_state": "informational"}
        )
        self.assertFalse(
            _confirms_current_order_summary(
                messages,
                message=answer,
                session=session,
                draft=self._complete_draft(),
                missing=[],
            )
        )
        db.close()

    def test_question_during_order_preserves_draft_and_appends_exact_faq_answer(self):
        db = self.Session()
        previous = self._message(db, 23, "Quiero cuatro cajas Caja eco")
        question = self._message(db, 24, "¿Cuál es el horario?")
        draft = self._complete_draft()
        draft["lines"][0]["source_message_ids"] = [previous.id]
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="ready_for_review",
            intent="order",
            revision=1,
            last_processed_message_id=previous.id,
            draft_json=json.dumps(draft),
            missing_fields_json="[]",
            started_at=previous.received_at - timedelta(minutes=1),
            last_customer_message_at=previous.received_at,
            due_at=question.received_at + timedelta(hours=12),
        )
        db.add_all(
            [
                session,
                ChannelSetting(
                    company_id=1,
                    channel_id=1,
                    key="business_faq",
                    value="Pregunta: ¿Cuál es el horario?\nRespuesta: Abrimos de 9 a 17.",
                ),
            ]
        )
        db.commit()
        answer = WhatsAppAgentTurnResult(
            intent="question",
            action="respond",
            draft={"customer_name": "", "lines": [], "notes": ""},
            missing_or_uncertain=[],
            reply_needed=True,
            suggested_reply="Atendemos todo el día.",
            confidence=0.95,
            evidence_ids=["faq:1"],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=answer):
            output = process_whatsapp_agent_turn(db, company_id=1, inbound_message=question)

        db.refresh(session)
        self.assertEqual(output["status"], "ready_for_review")
        self.assertEqual(session.intent, "order")
        self.assertEqual(json.loads(session.draft_json)["lines"], draft["lines"])
        self.assertTrue(session.suggested_reply.startswith("Abrimos de 9 a 17."))
        self.assertIn(_order_summary_reply(draft), session.suggested_reply)
        self.assertNotIn("todo el día", session.suggested_reply)
        db.close()

    def test_faq_answer_is_exact_and_untranscribed_audio_is_unreadable(self):
        entries = _faq_entries(
            "Pregunta: ¿Cuál es el horario?\nRespuesta: Abrimos de 9 a 17.\n\n"
            "Pregunta: ¿Dónde estáis?\nRespuesta: En Madrid.",
            "¿Cuál es vuestro horario?",
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(_grounded_faq_answer(entries, ["faq:1"]), "Abrimos de 9 a 17.")
        self.assertIsNone(_grounded_faq_answer(entries, ["faq:2"]))

        db = self.Session()
        message = self._message(db, 25, "nota de voz")
        from app.db.models import MessageAttachment

        message.attachments.append(
            MessageAttachment(
                company_id=1,
                filename="voice.ogg",
                content_type="audio/ogg",
                extraction_status="transcription_pending",
            )
        )
        self.assertEqual(_has_unreadable_or_pending_media([message]), ("unreadable", "voice.ogg"))
        db.close()

    def test_explicit_second_order_starts_a_separate_session(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        previous = self._message(db, 40, "Quiero cuatro cajas Caja eco", at=now - timedelta(minutes=2))
        current = self._message(db, 41, "Quiero otro pedido: una caja eco", at=now)
        prior_draft = self._complete_draft()
        prior_draft["lines"][0]["source_message_ids"] = [previous.id]
        prior = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="ready_for_review",
            intent="order",
            revision=1,
            last_processed_message_id=previous.id,
            draft_json=json.dumps(prior_draft),
            missing_fields_json="[]",
            started_at=previous.received_at - timedelta(minutes=1),
            last_customer_message_at=previous.received_at,
            due_at=now + timedelta(hours=12),
        )
        db.add(prior)
        db.commit()
        neutral_turn = WhatsAppAgentTurnResult(
            intent="other",
            action="silent",
            draft={"customer_name": "", "lines": [], "notes": ""},
            missing_or_uncertain=[],
            reply_needed=False,
            suggested_reply="",
            confidence=0.9,
            evidence_ids=[],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=neutral_turn):
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=current)

        sessions = db.scalars(
            select(WhatsAppOrderSession).where(WhatsAppOrderSession.company_id == 1).order_by(WhatsAppOrderSession.id)
        ).all()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].message_cutoff_at, current.received_at)
        self.assertNotEqual(sessions[0].id, result["session_id"])
        self.assertEqual(sessions[1].last_processed_message_id, current.id)
        job = db.scalar(select(BackgroundJob).where(BackgroundJob.job_type == "whatsapp_finalize_session"))
        self.assertIsNotNone(job)
        self.assertIn(f'"session_id": {sessions[0].id}', job.payload_json)
        db.close()

    def test_pending_media_timeout_is_deferred_for_bounded_retries(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        message = self._message(db, 50, "Pedido adjunto", at=now - timedelta(hours=13))
        from app.db.models import MessageAttachment

        message.attachments.append(
            MessageAttachment(
                company_id=1,
                filename="pedido.pdf",
                content_type="application/pdf",
                extraction_status="pending",
            )
        )
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=1,
            last_processed_message_id=message.id,
            draft_json="{}",
            missing_fields_json="[]",
            started_at=message.received_at,
            last_customer_message_at=message.received_at,
            due_at=now - timedelta(minutes=1),
        )
        db.add(session)
        db.commit()

        result = finalize_whatsapp_order_session(
            db,
            company_id=1,
            session_id=session.id,
            expected_revision=1,
            reason="idle_timeout",
        )
        db.refresh(session)
        self.assertEqual(result["reason"], "media_processing_pending")
        self.assertEqual(session.pending_media_retry_count, 1)
        self.assertEqual(session.status, "awaiting_customer")
        self.assertGreater(session.due_at.replace(tzinfo=timezone.utc), now)
        self.assertEqual(db.query(Order).count(), 0)
        db.close()

    def test_due_sweep_enqueues_one_job_for_due_session(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="collecting",
            intent="other",
            revision=1,
            draft_json="{}",
            missing_fields_json="[]",
            started_at=now - timedelta(hours=13),
            last_customer_message_at=now - timedelta(hours=13),
            due_at=now - timedelta(minutes=1),
        )
        db.add(session)
        db.commit()

        first = enqueue_due_whatsapp_session_finalizations(db, company_id=1, now=now)
        second = enqueue_due_whatsapp_session_finalizations(db, company_id=1, now=now)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(db.query(BackgroundJob).count(), 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
