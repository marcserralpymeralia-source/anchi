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
    MessageAttachment,
    Order,
    Product,
    WhatsAppOrderSession,
)
from app.whatsapp.agent import (
    WhatsAppAgentReplyDeliveryError,
    _canonicalize_draft,
    _clarification_reply,
    _confirms_current_order_summary,
    _draft_is_complete,
    _faq_entries,
    _grounded_faq_answer,
    _has_unreadable_or_pending_media,
    _latest_session_customer_message,
    _order_summary_reply,
    _product_context,
    _session_transcript,
    await_send_automatic_response,
    enqueue_due_whatsapp_session_finalizations,
    finalize_whatsapp_order_session,
    process_whatsapp_agent_turn,
    set_whatsapp_bot_enabled,
    set_whatsapp_human_control,
)
from app.whatsapp.conversation_semantics import evaluate_whatsapp_agent_turn
from app.whatsapp.conversation_semantics import WhatsAppAgentTurnResult
from app.whatsapp.service import (
    WhatsAppEmbeddedSignupError,
    whatsapp_config,
    whatsapp_response_window_status,
)


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
                whatsapp_bot_enabled=True,
                created_at=now - timedelta(minutes=20),
                updated_at=now,
            )
        )
        db.commit()
        db.close()
        self.auto_reply_patch = patch(
            "app.whatsapp.agent.await_send_automatic_response",
            return_value={"sent": True, "reason": "sent"},
        )
        self.auto_reply_sender = self.auto_reply_patch.start()
        self.addCleanup(self.auto_reply_patch.stop)

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
        db.add(ChannelSetting(company_id=1, channel_id=1, key="bot_enabled", value="false"))
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
        self.assertTrue(output["auto_response"]["sent"])
        self.auto_reply_sender.assert_called_once()
        self.assertIsNone(session.order_id)
        db.close()

    def test_low_confidence_answer_to_pending_order_does_not_force_handoff(self):
        db = self.Session()
        previous = self._message(db, 100, "Quiero cajas eco")
        message = self._message(db, 101, "Son 4 cajas")
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=1,
            last_processed_message_id=previous.id,
            draft_json=json.dumps({"customer_name": "", "lines": [{"product_query": "Caja eco"}], "notes": ""}),
            missing_fields_json=json.dumps(["Indicar la cantidad"]),
            started_at=previous.received_at,
            last_customer_message_at=previous.received_at,
            due_at=message.received_at + timedelta(hours=12),
        )
        db.add(session)
        db.commit()
        result = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={"customer_name": "", "lines": [{"product_query": "Caja eco", "quantity": 4, "unit": "cajas"}], "notes": ""},
            missing_or_uncertain=["Indicar la referencia exacta"],
            reply_needed=True,
            suggested_reply="¿Qué referencia necesitas?",
            confidence=0.31,
            evidence_ids=[],
            close_requested=False,
            contextual_order_followup=True,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=result):
            output = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        db.refresh(session)
        self.assertEqual(output["status"], "awaiting_customer")
        self.assertEqual(session.status, "awaiting_customer")
        self.assertFalse(session.human_active)
        db.close()

    def test_short_quantity_reply_uses_prior_bot_question_for_line_evidence(self):
        db = self.Session()
        db.add(
            Product(
                company_id=1,
                reference="ECO-1",
                name="Caja eco",
                sale_unit="cajas",
                status="active",
            )
        )
        now = datetime.now(timezone.utc)
        original = self._message(db, 105, "Quiero Caja eco", at=now - timedelta(minutes=2))
        self._message(
            db,
            106,
            "Para preparar bien el pedido necesito confirmar: ¿Qué cantidad de Caja eco (cajas) necesitas?",
            direction="outbound",
            at=now - timedelta(minutes=1),
        )
        reply = self._message(db, 107, "Serán 4", at=now)
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=2,
            last_processed_message_id=original.id,
            draft_json=json.dumps(
                {
                    "customer_name": "",
                    "lines": [
                        {
                            "product_query": "Caja eco",
                            "reference": "ECO-1",
                            "product_name": "Caja eco",
                            "quantity": None,
                            "unit": "cajas",
                            "notes": "",
                            "source_message_ids": [original.id],
                        }
                    ],
                    "notes": "",
                }
            ),
            missing_fields_json=json.dumps(["Indicar la cantidad de la línea 1"]),
            started_at=original.received_at,
            last_customer_message_at=original.received_at,
            due_at=reply.received_at + timedelta(hours=12),
        )
        db.add(session)
        db.commit()

        extraction = {
            "pedido": {
                "lineas": [
                    {
                        "producto_detectado": "Caja eco",
                        "referencia_detectada": "ECO-1",
                        "cantidad": 4,
                        "unidad": "cajas",
                    }
                ]
            }
        }
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "no_pedido", "confianza": 0.31},
            ) as classifier,
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value=extraction,
            ) as extractor,
        ):
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=reply)

        db.refresh(session)
        draft = json.loads(session.draft_json)
        self.assertEqual(result["status"], "ready_for_review")
        classifier_input = classifier.call_args.args[2]
        self.assertIn("EMPRESA: Para preparar bien el pedido", classifier_input)
        self.assertIn("Mensaje previo relacionado con la respuesta", classifier_input)
        self.assertIn("Respuesta actual del cliente: Serán 4", classifier_input)
        self.assertEqual(
            extractor.call_args.args[2],
            "CLIENTE: Quiero Caja eco\n"
            "MENSAJE PREVIO RELACIONADO (EMPRESA): Para preparar bien el pedido necesito confirmar: "
            "¿Qué cantidad de Caja eco (cajas) necesitas?\n"
            "CLIENTE: Serán 4",
        )
        self.assertEqual(draft["lines"][0]["quantity"], 4)
        self.assertEqual(draft["lines"][0]["source_message_ids"], [original.id, reply.id])
        self.assertEqual(json.loads(session.missing_fields_json), [])
        db.close()

    def test_new_session_keeps_rapid_messages_and_extracted_pdf_text(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        old_message = self._message(db, 110, "Conversación antigua", at=now - timedelta(days=2))
        first = self._message(db, 111, "Quiero hacer un pedido de cajas eco", at=now - timedelta(minutes=2))
        latest = self._message(db, 112, "Te adjunto el PDF", at=now)
        extracted_pdf_text = "Pedido del cliente\n" + ("Línea de catálogo. " * 130) + "\nECO-1: 4 cajas de Caja eco"
        latest.attachments.append(
            MessageAttachment(
                company_id=1,
                filename="pedido.pdf",
                content_type="application/pdf",
                extracted_text=extracted_pdf_text,
                is_pdf=True,
                extraction_status="extracted",
            )
        )
        db.commit()
        result = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={"customer_name": "", "lines": [{"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"}], "notes": ""},
            missing_or_uncertain=["Confirmar el pedido"],
            reply_needed=True,
            suggested_reply="¿Confirmas el pedido?",
            confidence=0.91,
            evidence_ids=[],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=result) as evaluator:
            process_whatsapp_agent_turn(db, company_id=1, inbound_message=latest)

        session = db.scalar(select(WhatsAppOrderSession))
        evaluation_kwargs = evaluator.call_args.kwargs
        self.assertEqual(session.started_at, first.received_at)
        self.assertIn("Quiero hacer un pedido de cajas eco", evaluation_kwargs["transcript"])
        self.assertIn("ECO-1: 4 cajas de Caja eco", evaluation_kwargs["transcript"])
        self.assertIn("ECO-1: 4 cajas de Caja eco", evaluation_kwargs["latest_message_text"])
        self.assertNotIn("Conversación antigua", evaluation_kwargs["transcript"])
        self.assertGreater(len(evaluation_kwargs["transcript"]), 1600)
        db.close()

    def test_late_earlier_webhook_is_merged_into_active_order(self):
        db = self.Session()
        db.add_all(
            [
                Product(company_id=1, reference="P-100", name="Caja eco", sale_unit="uds"),
                Product(company_id=1, reference="V-200", name="Vaso compostable", sale_unit="uds"),
            ]
        )
        later_at = datetime.now(timezone.utc) - timedelta(hours=25)
        latest = self._message(db, 159, "Anade 2 unidades de V-200", at=later_at)
        db.commit()

        p100_line = {
            "product_query": "Caja eco",
            "reference": "P-100",
            "product_name": "Caja eco",
            "quantity": 4,
            "unit": "uds",
            "notes": "",
        }
        v200_line = {
            "product_query": "Vaso compostable",
            "reference": "V-200",
            "product_name": "Vaso compostable",
            "quantity": 2,
            "unit": "uds",
            "notes": "",
        }

        def turn(lines):
            return WhatsAppAgentTurnResult(
                intent="order",
                action="respond",
                draft={"customer_name": "", "lines": lines, "notes": ""},
                missing_or_uncertain=[],
                reply_needed=True,
                suggested_reply="Pedido actualizado.",
                confidence=0.95,
                evidence_ids=[],
                close_requested=False,
            )

        with patch(
            "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
            side_effect=[turn([v200_line]), turn([p100_line, v200_line])],
        ) as evaluator:
            first_result = process_whatsapp_agent_turn(
                db, company_id=1, inbound_message=latest
            )
            earlier_at = later_at - timedelta(minutes=1)
            earlier = self._message(db, 160, "Quiero 4 unidades de P-100", at=earlier_at)
            db.commit()
            delayed_result = process_whatsapp_agent_turn(
                db, company_id=1, inbound_message=earlier
            )
            duplicate_late_result = process_whatsapp_agent_turn(
                db, company_id=1, inbound_message=earlier
            )

        session = db.scalar(select(WhatsAppOrderSession))
        draft = json.loads(session.draft_json)
        self.assertEqual(first_result["status"], "ready_for_review")
        self.assertEqual(delayed_result["status"], "ready_for_review")
        self.assertTrue(duplicate_late_result["skipped"])
        self.assertEqual(evaluator.call_count, 2)
        self.assertIn("CLIENTE: Quiero 4 unidades de P-100", evaluator.call_args.kwargs["transcript"])
        self.assertLess(
            evaluator.call_args.kwargs["transcript"].index("P-100"),
            evaluator.call_args.kwargs["transcript"].index("V-200"),
        )
        self.assertEqual(
            session.started_at.replace(tzinfo=timezone.utc),
            earlier_at,
        )
        self.assertEqual(session.last_processed_message_id, latest.id)
        self.assertEqual(session.agent_turn_count, 2)
        self.assertEqual(session.last_customer_message_at.replace(tzinfo=timezone.utc), later_at)
        self.assertEqual(
            [(line["reference"], line["quantity"]) for line in draft["lines"]],
            [("P-100", 4), ("V-200", 2)],
        )
        self.assertEqual(earlier.processing_step, "whatsapp_agent_reconciled")
        self.auto_reply_sender.assert_not_called()
        self.assertEqual(db.query(Order).count(), 0)
        db.close()

    def test_late_webhook_before_previous_order_boundary_is_not_merged(self):
        db = self.Session()
        db.add(Product(company_id=1, reference="V-200", name="Vaso compostable", sale_unit="uds"))
        boundary_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        previous_message = self._message(
            db, 169, "Pedido anterior de P-100", at=boundary_at
        )
        db.add(
            WhatsAppOrderSession(
                company_id=1,
                conversation_id=1,
                active_key=None,
                status="closed_review",
                intent="order",
                revision=1,
                last_processed_message_id=previous_message.id,
                started_at=boundary_at - timedelta(minutes=2),
                last_customer_message_at=boundary_at,
                due_at=boundary_at + timedelta(hours=12),
                closed_at=boundary_at + timedelta(minutes=1),
            )
        )
        latest_at = boundary_at + timedelta(minutes=2)
        latest = self._message(db, 170, "Anade 2 unidades de V-200", at=latest_at)
        db.commit()
        current_turn = WhatsAppAgentTurnResult(
            intent="order",
            action="respond",
            draft={
                "customer_name": "",
                "lines": [{
                    "product_query": "Vaso compostable",
                    "reference": "V-200",
                    "product_name": "Vaso compostable",
                    "quantity": 2,
                    "unit": "uds",
                    "notes": "",
                }],
                "notes": "",
            },
            missing_or_uncertain=[],
            reply_needed=True,
            suggested_reply="Pedido actualizado.",
            confidence=0.95,
            evidence_ids=[],
            close_requested=False,
        )

        with patch(
            "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
            return_value=current_turn,
        ) as evaluator:
            process_whatsapp_agent_turn(db, company_id=1, inbound_message=latest)
            delayed = self._message(
                db,
                171,
                "Quiero 4 unidades de P-100",
                at=boundary_at - timedelta(minutes=1),
            )
            db.commit()
            delayed_result = process_whatsapp_agent_turn(
                db, company_id=1, inbound_message=delayed
            )

        active = db.scalar(
            select(WhatsAppOrderSession).where(WhatsAppOrderSession.active_key.is_not(None))
        )
        draft = json.loads(active.draft_json)
        self.assertTrue(delayed_result["skipped"])
        self.assertEqual(delayed_result["reason"], "superseded_by_new_customer_message")
        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(delayed.status, "received")
        self.assertEqual(delayed.processing_step, "received")
        self.assertNotIn("P-100", json.dumps(draft))
        self.assertEqual(active.started_at.replace(tzinfo=timezone.utc), latest_at)
        db.close()

    def test_late_ready_pdf_is_included_while_latest_media_is_pending(self):
        db = self.Session()
        db.add_all(
            [
                Product(company_id=1, reference="P-100", name="Caja eco", sale_unit="uds"),
                Product(company_id=1, reference="V-200", name="Vaso compostable", sale_unit="uds"),
            ]
        )
        latest_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        latest = self._message(db, 172, "Te envio el pedido complementario", at=latest_at)
        latest_pdf = MessageAttachment(
            company_id=1,
            filename="complemento.pdf",
            content_type="application/pdf",
            is_pdf=True,
            extraction_status="pending",
        )
        latest.attachments.append(latest_pdf)
        db.commit()

        result = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={
                "customer_name": "",
                "lines": [
                    {
                        "product_query": "Caja eco",
                        "reference": "P-100",
                        "product_name": "Caja eco",
                        "quantity": 4,
                        "unit": "uds",
                        "notes": "",
                    },
                    {
                        "product_query": "Vaso compostable",
                        "reference": "V-200",
                        "product_name": "Vaso compostable",
                        "quantity": 2,
                        "unit": "uds",
                        "notes": "",
                    },
                ],
                "notes": "",
            },
            missing_or_uncertain=["Confirmar el pedido"],
            reply_needed=True,
            suggested_reply="Pedido actualizado.",
            confidence=0.95,
            evidence_ids=[],
            close_requested=False,
        )

        with patch(
            "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
            return_value=result,
        ) as evaluator:
            waiting = process_whatsapp_agent_turn(db, company_id=1, inbound_message=latest)
            earlier_at = latest_at - timedelta(minutes=1)
            earlier = self._message(db, 173, "Pedido original", at=earlier_at)
            earlier.attachments.append(
                MessageAttachment(
                    company_id=1,
                    filename="pedido-original.pdf",
                    content_type="application/pdf",
                    extracted_text="P-100: 4 uds Caja eco",
                    is_pdf=True,
                    extraction_status="extracted",
                )
            )
            db.commit()
            deferred = process_whatsapp_agent_turn(db, company_id=1, inbound_message=earlier)
            db.refresh(latest_pdf)
            latest_pdf.extraction_status = "extracted"
            latest_pdf.extracted_text = "V-200: 2 uds Vaso compostable"
            db.commit()
            completed = process_whatsapp_agent_turn(db, company_id=1, inbound_message=latest)

        session = db.scalar(select(WhatsAppOrderSession))
        transcript = evaluator.call_args.kwargs["transcript"]
        self.assertEqual(waiting["status"], "media_pending")
        self.assertEqual(deferred["status"], "media_pending")
        self.assertEqual(completed["status"], "awaiting_customer")
        self.assertEqual(evaluator.call_count, 1)
        self.assertIn("P-100: 4 uds Caja eco", transcript)
        self.assertIn("V-200: 2 uds Vaso compostable", transcript)
        self.assertLess(transcript.index("P-100"), transcript.index("V-200"))
        self.assertEqual(session.started_at.replace(tzinfo=timezone.utc), earlier_at)
        self.assertEqual(earlier.processing_step, "whatsapp_agent_reconciled")
        db.close()

    def test_processed_earlier_pdf_retries_latest_turn_deferred_for_media(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        pdf_message = self._message(db, 120, "Adjunto el pedido", at=now - timedelta(minutes=1))
        pdf_message.attachments.append(
            MessageAttachment(
                company_id=1,
                filename="pedido.pdf",
                content_type="application/pdf",
                extracted_text="Referencia ECO-1: 4 cajas de Caja eco",
                is_pdf=True,
                extraction_status="extracted",
            )
        )
        latest = self._message(db, 121, "Son 4 cajas", at=now)
        latest.processing_step = "whatsapp_media_pending"
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=1,
            last_processed_message_id=latest.id,
            draft_json=json.dumps({"customer_name": "", "lines": [{"product_query": "Caja eco"}], "notes": ""}),
            missing_fields_json=json.dumps(["Indicar la referencia exacta"]),
            started_at=pdf_message.received_at,
            last_customer_message_at=latest.received_at,
            due_at=latest.received_at + timedelta(hours=12),
        )
        db.add(session)
        db.commit()
        result = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={"customer_name": "", "lines": [{"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"}], "notes": ""},
            missing_or_uncertain=["Confirmar el pedido"],
            reply_needed=True,
            suggested_reply="¿Confirmas el pedido?",
            confidence=0.9,
            evidence_ids=[],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=result) as evaluator:
            output = process_whatsapp_agent_turn(db, company_id=1, inbound_message=pdf_message)

        self.assertEqual(output["session_id"], session.id)
        self.assertEqual(evaluator.call_args.kwargs["latest_message_text"], "Son 4 cajas")
        self.assertIn("Referencia ECO-1: 4 cajas de Caja eco", evaluator.call_args.kwargs["transcript"])
        self.assertEqual(latest.processing_step, "whatsapp_agent_evaluated")
        db.close()

    def test_realistic_multiturn_order_with_pdf_correction_and_idle_review(self):
        db = self.Session()
        customer = Customer(
            company_id=1,
            code="C-WA-SIM",
            fiscal_name="Cliente de simulación",
            commercial_name="Cliente de simulación",
            phone="+34600000000",
            status="active",
        )
        products = [
            Product(company_id=1, reference="ECO-1", name="Caja Eco", sale_unit="cajas"),
            Product(company_id=1, reference="MED-1", name="Caja mediana", sale_unit="cajas"),
            Product(company_id=1, reference="VASO-1", name="Vaso compostable", sale_unit="uds"),
        ]
        db.add_all([customer, *products])
        db.commit()

        start = datetime.now(timezone.utc) - timedelta(hours=13)
        def extracted_line(name, reference, quantity, unit):
            return {
                "producto_detectado": name,
                "referencia_detectada": reference,
                "cantidad": quantity,
                "unidad": unit,
            }

        extraction_results = [
            {
                "pedido": {"lineas": [extracted_line("Caja Eco", "ECO-1", 4, "cajas")]},
                "motivos_revision": ["Confirmar fecha de entrega"],
            },
            {
                "pedido": {
                    "lineas": [extracted_line("Caja Eco", "ECO-1", 4, "cajas")],
                    "observaciones": "Entrega jueves",
                }
            },
            {
                "pedido": {
                    "lineas": [
                        extracted_line("Caja Eco", "ECO-1", 4, "cajas"),
                        extracted_line("Vaso compostable", "VASO-1", 10, "uds"),
                    ],
                    "observaciones": "Entrega jueves",
                }
            },
            {
                "pedido": {
                    "lineas": [
                        extracted_line("Caja mediana", "MED-1", 6, "cajas"),
                        extracted_line("Vaso compostable", "VASO-1", 10, "uds"),
                    ],
                    "observaciones": "Entrega jueves",
                }
            },
        ]
        with (
            patch(
                "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
                wraps=evaluate_whatsapp_agent_turn,
            ) as evaluator,
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "pedido", "confianza": 0.97},
            ) as classifier,
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                side_effect=extraction_results,
            ) as extractor,
        ):
            first = self._message(db, 150, "Quiero 4 cajas de Caja Eco", at=start)
            db.commit()
            first_result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=first)

            second = self._message(
                db, 151, "La entrega sería el jueves.", at=start + timedelta(minutes=5)
            )
            db.commit()
            second_result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=second)

            pdf_message = self._message(
                db,
                152,
                "También añado vasos según el documento.",
                at=start + timedelta(minutes=10),
            )
            pdf_message.attachments.append(
                MessageAttachment(
                    company_id=1,
                    filename="pedido-complementario.pdf",
                    content_type="application/pdf",
                    extracted_text="Vaso compostable (VASO-1), 10 uds",
                    is_pdf=True,
                    extraction_status="extracted",
                )
            )
            db.commit()
            pdf_result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=pdf_message)
            pdf_session = db.scalar(select(WhatsAppOrderSession))
            pdf_missing = pdf_session.missing_fields_json
            pdf_draft = pdf_session.draft_json

            correction = self._message(
                db,
                153,
                "Perdona, cambia Caja Eco por Caja mediana: 6 cajas.",
                at=start + timedelta(minutes=20),
            )
            db.commit()
            correction_result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=correction)

        session = db.scalar(select(WhatsAppOrderSession))
        draft = json.loads(session.draft_json)
        self.assertEqual(first_result["status"], "awaiting_customer")
        self.assertEqual(second_result["status"], "ready_for_review")
        self.assertEqual(
            pdf_result["status"],
            "ready_for_review",
            msg=(
                f"result={pdf_result!r}; missing={pdf_missing}; draft={pdf_draft}; "
                f"products={evaluator.call_args_list[2].kwargs['products']!r}"
            ),
        )
        self.assertEqual(correction_result["status"], "ready_for_review")
        self.assertEqual(
            [(line["reference"], line["quantity"], line["unit"]) for line in draft["lines"]],
            [("MED-1", 6, "cajas"), ("VASO-1", 10, "uds")],
        )
        self.assertEqual(draft["notes"], "Entrega jueves")
        self.assertEqual(session.agent_turn_count, 4)
        self.assertEqual(session.last_processed_message_id, correction.id)
        self.assertEqual(session.last_customer_message_at, correction.received_at)
        self.assertEqual(
            session.due_at.replace(tzinfo=timezone.utc),
            (correction.received_at + timedelta(hours=12)).replace(tzinfo=timezone.utc),
        )
        self.assertIn("Vaso compostable (VASO-1), 10 uds", extractor.call_args_list[2].args[2])
        self.assertEqual(classifier.call_count, 4)
        self.assertEqual(extractor.call_count, 4)
        self.assertEqual(self.auto_reply_sender.call_count, 4)
        self.assertEqual(db.query(Order).count(), 0)

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
            finalized = finalize_whatsapp_order_session(
                db,
                company_id=1,
                session_id=session.id,
                expected_revision=session.revision,
                reason="idle_timeout",
            )

        db.refresh(session)
        self.assertEqual(finalized["status"], "closed_review")
        self.assertEqual(session.status, "closed_review")
        self.assertEqual(db.query(Order).count(), 1)
        self.assertTrue(pipeline.process_inbound_message.call_args.kwargs["force_human_review"])
        duplicate_finalize = finalize_whatsapp_order_session(
            db,
            company_id=1,
            session_id=session.id,
            expected_revision=session.revision,
            reason="idle_timeout",
        )
        self.assertEqual(duplicate_finalize["reason"], "already_closed")
        self.assertEqual(db.query(Order).count(), 1)
        db.close()

    def test_duplicate_product_reference_is_left_for_confirmation(self):
        products = [
            {"id": "product:1", "reference": "DUP-1", "name": "Caja eco pequeña", "sale_unit": "cajas"},
            {"id": "product:2", "reference": "DUP-1", "name": "Caja eco grande", "sale_unit": "cajas"},
        ]
        draft, missing = _canonicalize_draft(
            {
                "customer_name": "Cliente",
                "lines": [
                    {"product_query": "", "reference": "DUP-1", "quantity": 2, "unit": "cajas"}
                ],
                "notes": "",
            },
            products=products,
            semantic_missing=[],
            intent="order",
        )

        self.assertEqual(draft["lines"][0]["reference"], "")
        self.assertEqual(draft["lines"][0]["product_name"], "")
        self.assertTrue(any("coincide con varios productos" in item for item in missing))
        self.assertFalse(_draft_is_complete(draft, missing))

    def test_catalog_context_keeps_more_than_eight_products_from_order_draft(self):
        db = self.Session()
        product_rows = [
            Product(
                company_id=1,
                reference=f"SIM-{index:02d}",
                name=f"Producto de simulación {index:02d}",
                sale_unit="uds",
            )
            for index in range(12)
        ]
        db.add_all(product_rows)
        db.commit()
        draft = {
            "lines": [
                {
                    "reference": product.reference,
                    "product_query": product.name,
                    "quantity": 1,
                    "unit": "uds",
                }
                for product in product_rows
            ]
        }

        context = _product_context(
            db,
            company_id=1,
            transcript="CLIENTE: Necesito preparar el pedido de esta lista.",
            draft=draft,
        )

        self.assertEqual(len(context), 12)
        self.assertEqual(
            {product["reference"] for product in context},
            {product.reference for product in product_rows},
        )
        db.close()

    def test_oversized_pdf_is_sent_for_full_review_instead_of_truncated_extraction(self):
        db = self.Session()
        message = self._message(db, 130, "Adjunto el pedido completo")
        message.attachments.append(
            MessageAttachment(
                company_id=1,
                filename="pedido-extenso.pdf",
                content_type="application/pdf",
                extracted_text=("Referencia ECO-1, 4 cajas de Caja eco. " * 500),
                is_pdf=True,
                extraction_status="extracted",
            )
        )
        db.commit()

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn") as evaluator:
            output = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertEqual(output["status"], "human_handoff")
        self.assertIn("supera el límite", session.handoff_reason)
        evaluator.assert_not_called()
        db.close()

    def test_agent_hands_off_extracted_order_with_more_than_thirty_lines(self):
        db = self.Session()
        message = self._message(db, 140, "Adjunto el pedido completo")
        db.commit()
        result = WhatsAppAgentTurnResult(
            intent="order",
            action="respond",
            draft={
                "customer_name": "",
                "lines": [
                    {
                        "product_query": f"Producto {index}",
                        "reference": f"REF-{index}",
                        "quantity": 1,
                        "unit": "caja",
                    }
                    for index in range(31)
                ],
                "notes": "",
            },
            missing_or_uncertain=[],
            reply_needed=True,
            suggested_reply="",
            confidence=0.95,
            evidence_ids=[],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=result):
            output = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertEqual(output["status"], "human_handoff")
        self.assertIn("30 líneas", session.handoff_reason)
        self.assertIn("30 líneas", session.suggested_reply)
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

    def test_line_evidence_does_not_pair_quantity_with_a_different_product(self):
        db = self.Session()
        source = self._message(db, 84, "4 cajas eco y 2 cajas kraft")
        draft, missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 2, "unit": "cajas"},
                    {"product_query": "Caja kraft", "reference": "KRA-2", "quantity": 4, "unit": "cajas"},
                ],
                "notes": "",
            },
            products=[
                {"reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"},
                {"reference": "KRA-2", "name": "Caja kraft", "sale_unit": "cajas"},
            ],
            semantic_missing=[],
            intent="order",
            messages=[source],
        )
        self.assertEqual(
            [line["source_message_ids"] for line in draft["lines"]],
            [[], []],
        )
        self.assertTrue(any("Verificar producto y cantidad" in item for item in missing))
        db.close()

    def test_single_extracted_line_cannot_borrow_quantity_for_another_catalog_product(self):
        db = self.Session()
        first = self._message(db, 841, "Quiero Caja eco")
        second = self._message(db, 842, "Para Caja kraft son 4 cajas")
        products = [
            {"id": "product:eco", "reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"},
            {"id": "product:kraft", "reference": "KRA-2", "name": "Caja kraft", "sale_unit": "cajas"},
        ]

        draft, missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"}
                ],
                "notes": "",
            },
            products=products,
            semantic_missing=[],
            intent="order",
            messages=[first, second],
        )

        self.assertEqual(draft["lines"][0]["source_message_ids"], [])
        self.assertTrue(any("Verificar producto y cantidad" in item for item in missing))
        self.assertFalse(_draft_is_complete(draft, missing))
        db.close()

    def test_comma_separated_products_keep_their_own_quantity_evidence(self):
        db = self.Session()
        source = self._message(db, 85, "4 cajas eco, 2 cajas kraft")
        products = [
            {"reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"},
            {"reference": "KRA-2", "name": "Caja kraft", "sale_unit": "cajas"},
        ]

        matching, matching_missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"},
                    {"product_query": "Caja kraft", "reference": "KRA-2", "quantity": 2, "unit": "cajas"},
                ],
                "notes": "",
            },
            products=products,
            semantic_missing=[],
            intent="order",
            messages=[source],
        )
        self.assertEqual([line["source_message_ids"] for line in matching["lines"]], [[source.id], [source.id]])
        self.assertEqual(matching_missing, [])

        mismatched, mismatched_missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 2, "unit": "cajas"},
                    {"product_query": "Caja kraft", "reference": "KRA-2", "quantity": 4, "unit": "cajas"},
                ],
                "notes": "",
            },
            products=products,
            semantic_missing=[],
            intent="order",
            messages=[source],
        )
        self.assertEqual([line["source_message_ids"] for line in mismatched["lines"]], [[], []])
        self.assertEqual(sum("Verificar producto y cantidad" in item for item in mismatched_missing), 2)
        db.close()

    def test_word_quantities_need_local_product_context_and_cover_common_amounts(self):
        db = self.Session()
        products = [{"reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"}]
        cases = [
            (86, "Quiero una Caja eco.", 1, True),
            (87, "Es un pedido urgente de Caja eco; en realidad son cuatro cajas.", 1, False),
            (88, "Quiero veinticuatro cajas eco.", 24, True),
            (89, "Quiero treinta y dos cajas eco.", 32, True),
            (90, "Quiero Caja eco para entrega a las 4:30.", 4, False),
            (91, "El precio de Caja eco es 4 euros.", 4, False),
        ]

        for message_id, text, quantity, should_have_evidence in cases:
            source = self._message(db, message_id, text)
            draft, missing = _canonicalize_draft(
                {
                    "customer_name": "",
                    "lines": [
                        {
                            "product_query": "Caja eco",
                            "reference": "ECO-1",
                            "quantity": quantity,
                            "unit": "cajas",
                        }
                    ],
                    "notes": "",
                },
                products=products,
                semantic_missing=[],
                intent="order",
                messages=[source],
            )
            evidence = draft["lines"][0]["source_message_ids"]
            self.assertEqual(bool(evidence), should_have_evidence, msg=text)
            self.assertEqual(bool(missing), not should_have_evidence, msg=text)
        db.close()

    def test_short_quantity_reply_is_tied_only_to_a_product_specific_bot_question(self):
        db = self.Session()
        products = [
            {"reference": "ECO-1", "name": "Caja eco", "sale_unit": "cajas"},
            {"reference": "KRA-2", "name": "Caja kraft", "sale_unit": "cajas"},
        ]
        now = datetime.now(timezone.utc)
        order_request = self._message(
            db,
            92,
            "Quiero 2 cajas kraft y también cajas eco",
            at=now - timedelta(minutes=2),
        )
        clarification = self._message(
            db,
            93,
            "Para preparar bien el pedido necesito confirmar: ¿Qué cantidad de Caja eco (cajas) necesitas?",
            direction="outbound",
            at=now - timedelta(minutes=1),
        )
        quantity_reply = self._message(db, 94, "Serán 4", at=now)
        draft, missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"},
                    {"product_query": "Caja kraft", "reference": "KRA-2", "quantity": 2, "unit": "cajas"},
                ],
                "notes": "",
            },
            products=products,
            semantic_missing=[],
            intent="order",
            messages=[order_request, clarification, quantity_reply],
        )

        self.assertEqual(draft["lines"][0]["source_message_ids"], [order_request.id, quantity_reply.id])
        self.assertEqual(draft["lines"][1]["source_message_ids"], [order_request.id])
        self.assertEqual(missing, [])
        question = _clarification_reply(
            ["Indicar la cantidad de la línea 1."],
            draft={"lines": [{"product_name": "Caja eco", "unit": "cajas"}]},
        )
        self.assertIn("Caja eco (cajas)", question)
        self.assertNotIn("línea 1", question)

        legacy_clarification = self._message(
            db,
            97,
            "Para preparar bien el pedido necesito confirmar: Indicar la cantidad de la línea 1.",
            direction="outbound",
            at=now + timedelta(minutes=3),
        )
        legacy_reply = self._message(db, 98, "4", at=now + timedelta(minutes=4))
        legacy_draft, legacy_missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"},
                    {"product_query": "Caja kraft", "reference": "KRA-2", "quantity": 2, "unit": "cajas"},
                ],
                "notes": "",
            },
            products=products,
            semantic_missing=[],
            intent="order",
            messages=[order_request, legacy_clarification, legacy_reply],
        )
        self.assertEqual(legacy_draft["lines"][0]["source_message_ids"], [order_request.id, legacy_reply.id])
        self.assertEqual(legacy_missing, [])

        ambiguous_question = self._message(
            db,
            95,
            "¿Qué cantidad necesitas para Caja eco y Caja kraft?",
            direction="outbound",
            at=now + timedelta(minutes=1),
        )
        ambiguous_reply = self._message(db, 96, "4", at=now + timedelta(minutes=2))
        ambiguous_draft, ambiguous_missing = _canonicalize_draft(
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"},
                    {"product_query": "Caja kraft", "reference": "KRA-2", "quantity": 2, "unit": "cajas"},
                ],
                "notes": "",
            },
            products=products,
            semantic_missing=[],
            intent="order",
            messages=[order_request, ambiguous_question, ambiguous_reply],
        )
        self.assertEqual(ambiguous_draft["lines"][0]["source_message_ids"], [])
        self.assertTrue(any("Verificar producto y cantidad" in item for item in ambiguous_missing))
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
        conversation = db.get(Conversation, 1)
        self.assertEqual(output["status"], "human_handoff")
        self.assertEqual(session.status, "human_handoff")
        self.assertFalse(conversation.whatsapp_bot_enabled)
        self.assertEqual(session.suggested_reply, "Voy a pasar tu consulta a una persona del equipo para que pueda ayudarte.")
        handoff_alert = db.scalar(
            select(Alert).where(Alert.alert_type == "whatsapp_agent_handoff")
        )
        self.assertIsNotNone(handoff_alert)
        self.auto_reply_sender.assert_called_once()
        self.assertEqual(db.query(BackgroundJob).count(), 0)
        finalize.assert_not_called()
        resumed = set_whatsapp_bot_enabled(
            db,
            company_id=1,
            conversation_id=1,
            enabled=True,
        )
        self.assertTrue(resumed.whatsapp_bot_enabled)
        self.assertEqual(session.status, "ready_for_review")
        db.close()

    def test_agent_error_hands_off_and_sends_safe_ack_inside_customer_window(self):
        db = self.Session()
        message = self._message(db, 15, "Quiero 3 cajas de prueba")
        db.commit()

        with (
            patch(
                "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
                side_effect=RuntimeError("private provider detail"),
            ),
            patch(
                "app.whatsapp.agent.whatsapp_response_window_status",
                return_value={"is_open": True},
            ),
            patch(
                "app.whatsapp.agent.await_send_automatic_response",
                return_value={"sent": True, "reason": "sent"},
            ) as send_reply,
        ):
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        conversation = db.get(Conversation, 1)
        self.assertEqual(result["status"], "human_handoff")
        self.assertTrue(result["auto_response"]["sent"])
        self.assertEqual(session.status, "human_handoff")
        self.assertFalse(conversation.whatsapp_bot_enabled)
        self.assertEqual(session.last_error, "RuntimeError")
        self.assertEqual(message.status, "doubtful")
        send_reply.assert_called_once()
        body = send_reply.call_args.kwargs["body"]
        self.assertIn("No he podido procesar", body)
        self.assertNotIn("private provider detail", body)
        self.assertEqual(db.query(Order).count(), 0)
        db.close()

    def test_agent_error_does_not_send_fallback_outside_customer_window(self):
        db = self.Session()
        message = self._message(db, 16, "Quiero 3 cajas de prueba")
        db.commit()

        with (
            patch(
                "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
                side_effect=RuntimeError("private provider detail"),
            ),
            patch(
                "app.whatsapp.agent.whatsapp_response_window_status",
                return_value={"is_open": False, "reason": "response_window_closed"},
            ),
            patch("app.whatsapp.agent.await_send_automatic_response") as send_reply,
        ):
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        self.assertEqual(result["status"], "human_handoff")
        self.assertFalse(result["auto_response"]["sent"])
        self.assertEqual(result["auto_response"]["reason"], "response_window_closed")
        send_reply.assert_not_called()
        self.assertEqual(db.query(Order).count(), 0)
        db.close()

    def test_rate_limited_agent_error_handoff_ack_retries_before_handoff_skip(self):
        db = self.Session()
        message = self._message(db, 250, "Quiero 3 cajas de prueba")
        db.add_all(
            [
                ChannelSetting(company_id=1, channel_id=1, key="provider", value="meta"),
                ChannelSetting(company_id=1, channel_id=1, key="phone_number_id", value="pn-test"),
                ChannelSetting(company_id=1, channel_id=1, key="access_token", value="test-token"),
                ChannelSetting(company_id=1, channel_id=1, key="connection_status", value="connected"),
            ]
        )
        db.commit()
        self.auto_reply_sender.side_effect = await_send_automatic_response
        provider_calls = 0

        async def provider(*_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                raise WhatsAppEmbeddedSignupError("Rate limited", error_type="rate_limited")
            return {"provider_message_id": "wamid-agent-error-retry-success"}

        with (
            patch(
                "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
                side_effect=RuntimeError("synthetic provider outage"),
            ) as evaluator,
            patch("app.whatsapp.service.send_whatsapp_text", new=provider),
        ):
            with self.assertRaises(WhatsAppAgentReplyDeliveryError) as first_error:
                process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

            self.assertTrue(first_error.exception.retryable)
            self.assertEqual(message.processing_step, "whatsapp_agent_reply_retryable")
            self.assertTrue(db.get(Conversation, 1).whatsapp_bot_enabled)
            retry = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertTrue(retry["retried"])
        self.assertTrue(retry["auto_response"]["sent"])
        self.assertEqual(evaluator.call_count, 1)
        self.assertEqual(provider_calls, 2)
        self.assertEqual(db.query(InboundMessage).filter(InboundMessage.direction == "outbound").count(), 1)
        self.assertFalse(db.get(Conversation, 1).whatsapp_bot_enabled)
        self.assertEqual(session.status, "human_handoff")
        self.assertEqual(message.processing_step, "whatsapp_agent_reply_sent")
        db.close()

    def test_automatic_reply_limit_creates_handoff_instead_of_silent_success(self):
        db = self.Session()
        message = self._message(db, 251, "Hola")
        db.commit()

        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "no_pedido", "confianza": 0.2},
            ),
            patch(
                "app.whatsapp.agent.await_send_automatic_response",
                return_value={
                    "sent": False,
                    "skipped": True,
                    "reason": "auto_message_limit",
                },
            ),
        ):
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        conversation = db.get(Conversation, 1)
        alert = db.scalar(
            select(Alert).where(
                Alert.alert_type == "whatsapp_agent_handoff",
                Alert.inbound_message_id == message.id,
            )
        )
        self.assertEqual(result["status"], "human_handoff")
        self.assertEqual(session.status, "human_handoff")
        self.assertFalse(conversation.whatsapp_bot_enabled)
        self.assertIsNotNone(alert)
        self.assertEqual(message.processing_step, "whatsapp_agent_response_limit")
        self.assertEqual(db.query(Order).count(), 0)
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
        expected = {"ok": True, "status": "processed"}
        with patch("app.workers.jobs_worker.process_whatsapp_agent_turn", return_value=expected) as agent:
            result = _process_job(db, job)

        self.assertEqual(result, expected)
        agent.assert_called_once_with(db, company_id=1, inbound_message=message)
        db.close()

    def test_worker_does_not_call_the_model_when_chat_bot_is_disabled(self):
        from types import SimpleNamespace

        from app.workers.jobs_worker import _process_job

        db = self.Session()
        db.get(Conversation, 1).whatsapp_bot_enabled = False
        message = self._message(db, 14, "Quiero 3 cajas de prueba")
        db.commit()
        job = SimpleNamespace(
            job_type="process_inbound_message",
            company_id=1,
            created_by_user_id=None,
            payload_json=json.dumps({"inbound_message_id": message.id, "channel": "whatsapp"}),
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn") as evaluator:
            result = _process_job(db, job)

        self.assertEqual(result["status"], "bot_disabled")
        self.assertEqual(db.query(WhatsAppOrderSession).count(), 0)
        evaluator.assert_not_called()
        self.auto_reply_sender.assert_not_called()
        db.close()

    def test_disabled_chat_bot_does_not_call_the_model_or_create_a_session(self):
        db = self.Session()
        db.get(Conversation, 1).whatsapp_bot_enabled = False
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

    def test_unknown_catalog_product_asks_for_reference_instead_of_false_handoff(self):
        db = self.Session()
        message = self._message(db, 240, "Quiero 2 cajas de un producto que no tenemos")
        db.commit()
        evaluation = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={
                "customer_name": "",
                "lines": [
                    {
                        "product_query": "Producto que no tenemos",
                        "reference": "",
                        "quantity": 2,
                        "unit": "cajas",
                    }
                ],
                "notes": "",
            },
            missing_or_uncertain=[],
            reply_needed=True,
            suggested_reply="",
            confidence=0.98,
            evidence_ids=[],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=evaluation):
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        conversation = db.get(Conversation, 1)
        handoff_alert = db.scalar(
            select(Alert).where(Alert.alert_type == "whatsapp_agent_handoff")
        )
        self.assertEqual(result["status"], "awaiting_customer")
        self.assertEqual(session.status, "awaiting_customer")
        self.assertTrue(conversation.whatsapp_bot_enabled)
        self.assertIsNone(handoff_alert)
        self.assertIn("¿Puedes indicarme su referencia o el modelo", session.suggested_reply)
        self.assertNotIn("con el equipo", session.suggested_reply.casefold())
        db.close()

    def test_customer_request_for_a_person_hands_off_despite_no_order_classification(self):
        db = self.Session()
        message = self._message(db, 241, "Necesito hablar con una persona")
        db.commit()

        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "no_pedido", "confianza": 0.99},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order"
            ) as extractor,
        ):
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        conversation = db.get(Conversation, 1)
        handoff_alert = db.scalar(
            select(Alert).where(Alert.alert_type == "whatsapp_agent_handoff")
        )
        self.assertEqual(result["status"], "human_handoff")
        self.assertEqual(session.status, "human_handoff")
        self.assertFalse(conversation.whatsapp_bot_enabled)
        self.assertIsNotNone(handoff_alert)
        self.assertIn("Voy a pasar tu consulta a una persona", session.suggested_reply)
        self.assertEqual(
            self.auto_reply_sender.call_args.kwargs["body"],
            session.suggested_reply,
        )
        extractor.assert_not_called()
        db.close()

    def test_simple_greeting_gets_a_safe_reply_without_creating_an_order(self):
        db = self.Session()
        message = self._message(db, 242, "Hola")
        db.commit()

        with patch(
            "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
            return_value={"tipo_correo": "no_pedido", "confianza": 0.2},
        ):
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(session.intent, "greeting")
        self.assertEqual(db.query(Order).count(), 0)
        self.assertEqual(self.auto_reply_sender.call_count, 1)
        self.assertIn("¿En qué puedo ayudarte?", self.auto_reply_sender.call_args.kwargs["body"])
        self.assertTrue(db.get(Conversation, 1).whatsapp_bot_enabled)
        db.close()

    def test_explicit_cancellation_closes_only_the_active_order_draft(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        previous = self._message(db, 245, "Quiero 4 cajas eco", at=now - timedelta(minutes=2))
        message = self._message(db, 246, "Por favor, cancela el pedido", at=now)
        existing_order = Order(
            company_id=1,
            conversation_id=1,
            status="pending_review",
            created_at=now - timedelta(days=2),
        )
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=3,
            draft_json=json.dumps(self._complete_draft()),
            missing_fields_json="[]",
            last_processed_message_id=previous.id,
            started_at=previous.received_at,
            last_customer_message_at=previous.received_at,
            due_at=now + timedelta(hours=12),
        )
        db.add_all([existing_order, session])
        db.commit()

        result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        db.refresh(session)
        db.refresh(existing_order)
        self.assertEqual(result["status"], "closed_no_order")
        self.assertEqual(session.closed_reason, "customer_cancelled")
        self.assertIsNone(session.active_key)
        self.assertEqual(session.last_processed_message_id, message.id)
        self.assertEqual(existing_order.status, "pending_review")
        self.assertEqual(db.query(Order).count(), 1)
        self.assertTrue(db.get(Conversation, 1).whatsapp_bot_enabled)
        self.assertEqual(message.processing_step, "whatsapp_agent_order_cancelled")
        self.assertIn("He cancelado el borrador", self.auto_reply_sender.call_args.kwargs["body"])
        db.close()

    def test_cancellation_of_an_already_created_order_only_hands_off(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        order = Order(
            company_id=1,
            conversation_id=1,
            status="pending_review",
            created_at=now - timedelta(minutes=1),
        )
        message = self._message(db, 247, "Cancela el pedido, por favor", at=now)
        db.add(order)
        db.commit()

        result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        db.refresh(order)
        conversation = db.get(Conversation, 1)
        alert = db.scalar(
            select(Alert).where(
                Alert.alert_type == "whatsapp_agent_handoff",
                Alert.inbound_message_id == message.id,
            )
        )
        self.assertEqual(result["status"], "human_handoff")
        self.assertEqual(session.status, "human_handoff")
        self.assertEqual(session.order_id, None)
        self.assertEqual(order.status, "pending_review")
        self.assertFalse(conversation.whatsapp_bot_enabled)
        self.assertIsNotNone(alert)
        self.assertIn("ya está registrado", self.auto_reply_sender.call_args.kwargs["body"])
        self.assertEqual(message.processing_step, "whatsapp_agent_cancel_handoff")
        db.close()

    def test_rate_limited_cancellation_ack_retries_same_reply_then_closes_draft(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        previous = self._message(db, 248, "Quiero 4 cajas eco", at=now - timedelta(minutes=2))
        message = self._message(db, 249, "Anula la comanda", at=now)
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=2,
            draft_json=json.dumps(self._complete_draft()),
            missing_fields_json="[]",
            last_processed_message_id=previous.id,
            started_at=previous.received_at,
            last_customer_message_at=previous.received_at,
            due_at=now + timedelta(hours=12),
        )
        db.add_all(
            [
                session,
                ChannelSetting(company_id=1, channel_id=1, key="provider", value="meta"),
                ChannelSetting(company_id=1, channel_id=1, key="phone_number_id", value="pn-test"),
                ChannelSetting(company_id=1, channel_id=1, key="access_token", value="test-token"),
                ChannelSetting(company_id=1, channel_id=1, key="connection_status", value="connected"),
            ]
        )
        db.commit()
        self.auto_reply_sender.side_effect = await_send_automatic_response
        provider_calls = 0

        async def provider(*_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                raise WhatsAppEmbeddedSignupError("Rate limited", error_type="rate_limited")
            return {"provider_message_id": "wamid-cancel-retry-success"}

        with patch("app.whatsapp.service.send_whatsapp_text", new=provider):
            with self.assertRaises(WhatsAppAgentReplyDeliveryError) as first_error:
                process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

            self.assertTrue(first_error.exception.retryable)
            self.assertEqual(message.processing_step, "whatsapp_agent_reply_retryable")
            self.assertEqual(session.closed_reason, "customer_cancel_reply_pending")
            self.assertIsNotNone(session.active_key)
            retry = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        db.refresh(session)
        self.assertTrue(retry["retried"])
        self.assertTrue(retry["auto_response"]["sent"])
        self.assertEqual(provider_calls, 2)
        self.assertEqual(session.status, "closed_no_order")
        self.assertEqual(session.closed_reason, "customer_cancelled")
        self.assertIsNone(session.active_key)
        self.assertEqual(db.query(Order).count(), 0)
        self.assertEqual(message.processing_step, "whatsapp_agent_order_cancelled")
        db.close()

    def test_unclassified_non_order_turn_sends_a_safe_reply_and_keeps_bot_active(self):
        db = self.Session()
        message = self._message(db, 243, "Quería hacer una consulta")
        db.commit()
        result = WhatsAppAgentTurnResult(
            intent="other",
            action="ask",
            draft={"customer_name": "", "lines": [], "notes": ""},
            missing_or_uncertain=[],
            reply_needed=True,
            suggested_reply="",
            confidence=0.99,
            evidence_ids=[],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=result):
            output = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertTrue(output["auto_response"]["sent"])
        self.assertIn("¿Quieres hacer un pedido o tienes una consulta?", session.suggested_reply)
        self.assertTrue(db.get(Conversation, 1).whatsapp_bot_enabled)
        self.assertEqual(db.query(Order).count(), 0)
        self.auto_reply_sender.assert_called_once()
        db.close()

    def test_rate_limited_reply_retries_same_turn_without_reclassifying_or_duplicate(self):
        db = self.Session()
        message = self._message(db, 244, "Hola")
        db.add_all(
            [
                ChannelSetting(company_id=1, channel_id=1, key="provider", value="meta"),
                ChannelSetting(company_id=1, channel_id=1, key="phone_number_id", value="pn-test"),
                ChannelSetting(company_id=1, channel_id=1, key="access_token", value="test-token"),
                ChannelSetting(company_id=1, channel_id=1, key="connection_status", value="connected"),
            ]
        )
        db.commit()
        self.auto_reply_sender.side_effect = await_send_automatic_response
        provider_calls = 0

        async def provider(*_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 1:
                raise WhatsAppEmbeddedSignupError("Rate limited", error_type="rate_limited")
            return {"provider_message_id": "wamid-agent-retry-success"}

        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "no_pedido", "confianza": 0.2},
            ) as classifier,
            patch("app.whatsapp.service.send_whatsapp_text", new=provider),
        ):
            with self.assertRaises(WhatsAppAgentReplyDeliveryError) as first_error:
                process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

            self.assertEqual(
                first_error.exception.error_type,
                "rate_limited",
                str(first_error.exception.__cause__),
            )
            self.assertTrue(first_error.exception.retryable)
            self.assertEqual(message.processing_step, "whatsapp_agent_reply_retryable")

            retry = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        outbound = db.query(InboundMessage).filter(InboundMessage.direction == "outbound").all()
        self.assertTrue(retry["retried"])
        self.assertTrue(retry["auto_response"]["sent"])
        self.assertEqual(provider_calls, 2)
        self.assertEqual(classifier.call_count, 1)
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].status, "accepted")
        self.assertEqual(message.processing_step, "whatsapp_agent_reply_sent")
        self.assertTrue(db.get(Conversation, 1).whatsapp_bot_enabled)
        self.assertEqual(db.query(Order).count(), 0)
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
        alert = db.scalar(
            select(Alert).where(Alert.alert_type == "whatsapp_order_incomplete")
        )
        self.assertIsNotNone(alert)
        self.assertIn("revisión humana", alert.message)
        self.assertEqual(json.loads(alert.payload_json)["conversation_id"], 1)
        from app.alerts.routes import alert_default_action

        self.assertEqual(alert_default_action(alert)[0], "Abrir conversación")
        self.assertEqual(
            alert_default_action(alert)[1],
            "/whatsapp/inbox?conversation_id=1",
        )
        pipeline_service.assert_not_called()
        db.close()

    def test_complete_order_uses_shared_pipeline_with_human_review_veto(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        message = self._message(db, 5, "Confirmo cuatro cajas Caja eco")
        complete_draft = self._complete_draft()
        complete_draft["lines"][0]["source_message_ids"] = [message.id]
        customer = Customer(
            company_id=1,
            code="C-WA-1",
            fiscal_name="Cliente WhatsApp",
            commercial_name="Cliente WhatsApp",
            phone="+34600000000",
            status="active",
        )
        db.add(customer)
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
        self.assertEqual(db.get(Conversation, 1).customer_id, customer.id)
        self.assertEqual(message.customer_id, customer.id)
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
        identity_alert = db.query(Alert).filter_by(alert_type="whatsapp_customer_identity").one()
        self.assertEqual(json.loads(identity_alert.payload_json)["conversation_id"], 1)
        pipeline_service.assert_not_called()
        db.close()

    def test_duplicate_customer_phone_is_never_assigned_to_an_arbitrary_order(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        db.add_all(
            [
                Customer(
                    company_id=1,
                    code="C-WA-DUP-1",
                    fiscal_name="Cliente ambiguo uno",
                    commercial_name="Cliente ambiguo uno",
                    phone="+34 600 000 000",
                    status="active",
                ),
                Customer(
                    company_id=1,
                    code="C-WA-DUP-2",
                    fiscal_name="Cliente ambiguo dos",
                    commercial_name="Cliente ambiguo dos",
                    phone="0034 600000000",
                    status="active",
                ),
            ]
        )
        message = self._message(db, 16, "Quiero cuatro cajas Caja eco")
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
        self.assertIsNone(db.get(Conversation, 1).customer_id)
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

    def test_short_yes_replying_to_quoted_summary_confirms_only_the_current_order(self):
        db = self.Session()
        before = self._message(db, 25, "Quiero cuatro cajas Caja eco")
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
        summary = self._message(db, 26, _order_summary_reply(self._complete_draft()), direction="outbound")
        summary.raw_payload_json = json.dumps(
            {
                "auto_response": True,
                "trigger_message_id": before.id,
                "semantic_state": "ready_for_confirmation",
                "assistant_session_id": session.id,
                "draft_revision": 1,
            }
        )
        later_company_message = self._message(db, 27, "¿Puedo ayudarte con algo más?", direction="outbound")
        answer = self._message(db, 28, "Sí")
        answer.raw_payload_json = json.dumps(
            {"payload": {"context": {"id": summary.source_external_id}}}
        )
        messages = [before, summary, later_company_message, answer]

        self.assertTrue(
            _confirms_current_order_summary(
                messages,
                message=answer,
                session=session,
                draft=self._complete_draft(),
                missing=[],
            )
        )

        intervening_customer_message = self._message(db, 29, "En realidad, añade dos cajas más")
        session.last_processed_message_id = intervening_customer_message.id
        session.revision = 3
        stale_answer = self._message(db, 30, "Sí")
        stale_answer.raw_payload_json = json.dumps(
            {"payload": {"context": {"id": summary.source_external_id}}}
        )
        messages.extend([intervening_customer_message, stale_answer])
        self.assertFalse(
            _confirms_current_order_summary(
                messages,
                message=stale_answer,
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

    def test_unreadable_media_is_handed_to_a_person_and_customer_is_not_left_silent(self):
        db = self.Session()
        message = self._message(db, 155, "")
        message.attachments.append(
            MessageAttachment(
                company_id=1,
                filename="pedido-escaneado.pdf",
                content_type="application/pdf",
                extraction_status="no_text_found",
            )
        )
        db.commit()

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn") as evaluator:
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertEqual(result["status"], "human_handoff")
        self.assertEqual(result["reason"], "unreadable_media")
        self.assertEqual(session.status, "human_handoff")
        self.assertFalse(db.get(Conversation, 1).whatsapp_bot_enabled)
        self.assertEqual(message.processing_step, "whatsapp_media_handoff")
        evaluator.assert_not_called()
        self.auto_reply_sender.assert_called_once()
        self.assertIn("No he podido interpretar el archivo o audio", self.auto_reply_sender.call_args.kwargs["body"])
        self.assertNotIn("puedes escribir", self.auto_reply_sender.call_args.kwargs["body"])
        self.assertEqual(result["auto_response"]["sent"], True)
        db.close()

    def test_readable_text_with_unreadable_attachment_is_preserved_for_human_review(self):
        db = self.Session()
        message = self._message(db, 156, "Quiero 4 cajas eco; adjunto el PDF con los detalles")
        message.attachments.append(
            MessageAttachment(
                company_id=1,
                filename="pedido.pdf",
                content_type="application/pdf",
                extraction_status="no_text_found",
            )
        )
        db.commit()
        turn = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={"customer_name": "", "lines": [], "notes": ""},
            missing_or_uncertain=["Confirmar productos del adjunto"],
            reply_needed=True,
            suggested_reply="¿Puedes confirmar el pedido?",
            confidence=0.9,
            evidence_ids=[],
            close_requested=False,
        )

        with patch("app.whatsapp.agent.evaluate_whatsapp_agent_turn", return_value=turn) as evaluator:
            result = process_whatsapp_agent_turn(db, company_id=1, inbound_message=message)

        session = db.scalar(select(WhatsAppOrderSession))
        self.assertEqual(result["status"], "human_handoff")
        self.assertEqual(session.status, "human_handoff")
        self.assertEqual(
            evaluator.call_args.kwargs["latest_message_text"],
            "Quiero 4 cajas eco; adjunto el PDF con los detalles",
        )
        self.assertIn("He conservado el texto de tu mensaje", self.auto_reply_sender.call_args.kwargs["body"])
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

    def test_order_after_closed_session_starts_with_clean_draft_and_transcript(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        previous = self._message(
            db,
            42,
            "Quiero cuatro cajas Caja eco",
            at=now - timedelta(hours=13),
        )
        current = self._message(
            db,
            43,
            "Ahora necesito dos cajas de Vaso compostable",
            at=now,
        )
        prior_draft = self._complete_draft()
        prior_draft["lines"][0]["source_message_ids"] = [previous.id]
        prior = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key=None,
            status="closed_review",
            intent="order",
            revision=2,
            last_processed_message_id=previous.id,
            draft_json=json.dumps(prior_draft),
            missing_fields_json="[]",
            started_at=previous.received_at - timedelta(minutes=1),
            last_customer_message_at=previous.received_at,
            due_at=previous.received_at + timedelta(hours=12),
        )
        db.add(prior)
        db.commit()
        next_turn = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={"customer_name": "", "lines": [], "notes": ""},
            missing_or_uncertain=["Confirmar producto"],
            reply_needed=True,
            suggested_reply="¿Qué producto necesitas?",
            confidence=0.9,
            evidence_ids=[current.id],
            close_requested=False,
        )

        with patch(
            "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
            return_value=next_turn,
        ) as evaluator:
            result = process_whatsapp_agent_turn(
                db,
                company_id=1,
                inbound_message=current,
            )

        sessions = db.scalars(
            select(WhatsAppOrderSession)
            .where(WhatsAppOrderSession.company_id == 1)
            .order_by(WhatsAppOrderSession.id)
        ).all()
        new_session = sessions[-1]
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(new_session.id, prior.id)
        self.assertEqual(new_session.started_at, current.received_at)
        self.assertEqual(json.loads(new_session.draft_json)["lines"], [])
        transcript = evaluator.call_args.kwargs["transcript"]
        self.assertIn("Ahora necesito dos cajas", transcript)
        self.assertNotIn("Caja eco", transcript)
        self.assertEqual(result["session_id"], new_session.id)
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

    def test_idle_session_window_stays_at_twelve_hours_with_legacy_setting(self):
        db = self.Session()
        legacy_setting = db.scalar(
            select(ChannelSetting).where(
                ChannelSetting.company_id == 1,
                ChannelSetting.channel_id == 1,
                ChannelSetting.key == "idle_session_hours",
            )
        )
        legacy_setting.value = "72"
        db.commit()

        self.assertEqual(whatsapp_config(db, 1).idle_session_hours, 12)
        db.close()

    def test_disabling_chat_bot_pauses_due_finalization_and_can_resume(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="finalizing",
            intent="order",
            revision=4,
            draft_json="{}",
            missing_fields_json='["Confirmar producto"]',
            started_at=now - timedelta(hours=13),
            last_customer_message_at=now - timedelta(hours=13),
            due_at=now - timedelta(minutes=1),
            finalization_job_key="wa-session-finalize:1:r4",
        )
        db.add(session)
        db.commit()

        set_whatsapp_bot_enabled(db, company_id=1, conversation_id=1, enabled=False)
        self.assertEqual(
            enqueue_due_whatsapp_session_finalizations(db, company_id=1, now=now),
            0,
        )
        result = finalize_whatsapp_order_session(
            db,
            company_id=1,
            session_id=session.id,
            expected_revision=session.revision,
            reason="idle_timeout",
        )
        db.refresh(session)
        self.assertEqual(result["reason"], "bot_disabled")
        self.assertEqual(session.status, "awaiting_customer")
        self.assertIsNone(session.finalization_job_key)
        self.assertEqual(db.query(Order).count(), 0)

        session.due_at = now - timedelta(minutes=1)
        db.commit()
        set_whatsapp_bot_enabled(db, company_id=1, conversation_id=1, enabled=True)
        self.assertEqual(
            enqueue_due_whatsapp_session_finalizations(db, company_id=1, now=now),
            1,
        )
        self.assertEqual(db.query(BackgroundJob).count(), 1)
        db.close()

    def test_session_cutoff_uses_message_id_when_timestamps_match(self):
        db = self.Session()
        at = datetime.now(timezone.utc).replace(microsecond=0)
        previous = self._message(db, 81, "Pide 2 cajas", at=at)
        later = self._message(db, 82, "Añade 4 vasos", at=at)
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=2,
            last_processed_message_id=previous.id,
            draft_json="{}",
            missing_fields_json="[]",
            started_at=at - timedelta(minutes=1),
            message_cutoff_at=at,
            last_customer_message_at=at,
            due_at=at + timedelta(hours=12),
        )
        db.add(session)
        db.commit()

        latest = _latest_session_customer_message(db, company_id=1, session=session)
        transcript, messages = _session_transcript(
            db,
            company_id=1,
            conversation_id=1,
            session=session,
            latest_message=previous,
        )
        self.assertEqual(latest.id, previous.id)
        self.assertEqual([message.id for message in messages], [previous.id])
        self.assertIn("Pide 2 cajas", transcript)
        self.assertNotIn("Añade 4 vasos", transcript)
        self.assertNotEqual(previous.id, later.id)
        db.close()

    def test_customer_message_after_idle_deadline_starts_a_new_session_before_sweeper(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        previous_message = self._message(
            db,
            60,
            "Quiero cuatro cajas de Caja eco",
            at=now - timedelta(hours=13),
        )
        current_message = self._message(
            db,
            61,
            "Ahora necesito dos vasos compostables",
            at=now,
        )
        prior_draft = self._complete_draft()
        prior_draft["lines"][0]["source_message_ids"] = [previous_message.id]
        prior_session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="awaiting_customer",
            intent="order",
            revision=2,
            last_processed_message_id=previous_message.id,
            draft_json=json.dumps(prior_draft),
            missing_fields_json=json.dumps(["Indicar unidad"]),
            started_at=previous_message.received_at,
            last_customer_message_at=previous_message.received_at,
            due_at=now - timedelta(hours=1),
        )
        db.add(prior_session)
        db.commit()
        next_turn = WhatsAppAgentTurnResult(
            intent="order",
            action="ask",
            draft={"customer_name": "", "lines": [], "notes": ""},
            missing_or_uncertain=["Indicar el producto"],
            reply_needed=True,
            suggested_reply="¿Qué producto necesitas?",
            confidence=0.95,
            evidence_ids=[],
            close_requested=False,
        )

        with patch(
            "app.whatsapp.agent.evaluate_whatsapp_agent_turn",
            return_value=next_turn,
        ) as evaluator:
            result = process_whatsapp_agent_turn(
                db,
                company_id=1,
                inbound_message=current_message,
            )

        db.refresh(prior_session)
        sessions = db.scalars(
            select(WhatsAppOrderSession)
            .where(WhatsAppOrderSession.company_id == 1)
            .order_by(WhatsAppOrderSession.id)
        ).all()
        new_session = sessions[-1]
        finalization = db.scalar(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "whatsapp_finalize_session"
            )
        )
        self.assertEqual(len(sessions), 2)
        self.assertEqual(prior_session.message_cutoff_at, current_message.received_at)
        self.assertEqual(prior_session.status, "awaiting_customer")
        self.assertEqual(new_session.started_at, current_message.received_at)
        self.assertNotEqual(new_session.id, prior_session.id)
        transcript = evaluator.call_args.kwargs["transcript"]
        self.assertIn("Ahora necesito dos vasos", transcript)
        self.assertNotIn("cuatro cajas de Caja eco", transcript)
        self.assertEqual(json.loads(finalization.payload_json)["reason"], "idle_timeout")
        self.assertEqual(result["session_id"], new_session.id)
        db.close()

    def test_session_transcript_excludes_customer_message_arriving_after_selected_turn(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        first = self._message(db, 70, "Quiero cuatro cajas eco", at=now - timedelta(minutes=2))
        selected = self._message(db, 71, "Son cuatro", at=now - timedelta(minutes=1))
        arrived_during_processing = self._message(
            db,
            72,
            "Y también dos vasos",
            at=now,
        )
        session = WhatsAppOrderSession(
            company_id=1,
            conversation_id=1,
            active_key="whatsapp-conversation:1",
            status="collecting",
            intent="order",
            revision=1,
            draft_json="{}",
            missing_fields_json="[]",
            started_at=first.received_at,
            last_customer_message_at=first.received_at,
            due_at=now + timedelta(hours=12),
        )
        db.add(session)
        db.flush()

        transcript, messages = _session_transcript(
            db,
            company_id=1,
            conversation_id=1,
            session=session,
            latest_message=selected,
        )

        self.assertIn("Son cuatro", transcript)
        self.assertNotIn("dos vasos", transcript)
        self.assertNotIn(arrived_during_processing.id, {item.id for item in messages})
        db.close()

    def test_whatsapp_customer_response_window_never_exceeds_24_hours(self):
        db = self.Session()
        message = self._message(
            db,
            80,
            "Pedido de ayer",
            at=datetime.now(timezone.utc) - timedelta(hours=24, minutes=1),
        )
        db.commit()

        result = whatsapp_response_window_status(
            db,
            company_id=1,
            conversation_id=1,
            response_window_minutes=7 * 24 * 60,
        )

        self.assertFalse(result["is_open"])
        self.assertEqual(result["reason"], "response_window_expired")
        db.close()

    def test_transient_finalization_failure_remains_retryable_and_does_not_lose_order(self):
        db = self.Session()
        now = datetime.now(timezone.utc)
        message = self._message(db, 90, "Confirmo cuatro cajas de Caja eco", at=now - timedelta(hours=13))
        complete_draft = self._complete_draft()
        complete_draft["lines"][0]["source_message_ids"] = [message.id]
        db.add(
            Customer(
                company_id=1,
                code="C-WA-RETRY",
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
            revision=3,
            last_processed_message_id=message.id,
            draft_json=json.dumps(complete_draft),
            missing_fields_json="[]",
            started_at=message.received_at,
            last_customer_message_at=message.received_at,
            due_at=now - timedelta(hours=1),
        )
        db.add(session)
        db.commit()
        pipeline = MagicMock()
        pipeline.process_inbound_message.side_effect = RuntimeError("temporary evaluator outage")

        with patch("app.agent.services.AgentProcessingService") as service_class:
            service_class.return_value.pipeline = pipeline
            with self.assertRaisesRegex(RuntimeError, "temporary evaluator outage"):
                finalize_whatsapp_order_session(
                    db,
                    company_id=1,
                    session_id=session.id,
                    expected_revision=session.revision,
                    reason="idle_timeout",
                )

            db.refresh(session)
            self.assertEqual(session.status, "finalizing")
            self.assertIsNotNone(session.active_key)
            self.assertEqual(db.query(Order).count(), 0)

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
            retry_result = finalize_whatsapp_order_session(
                db,
                company_id=1,
                session_id=session.id,
                expected_revision=session.revision,
                reason="idle_timeout",
            )

        db.refresh(session)
        self.assertEqual(retry_result["status"], "closed_review")
        self.assertEqual(session.status, "closed_review")
        self.assertEqual(db.query(Order).count(), 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
