from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.prompt_runtime import run_prompt_execution
from app.agent.platform import UnifiedOrderPipelineService
from app.db.database import Base
from app.db.models import Company, LLMSettings, PromptExecution, PromptExecutionDetail
from app.whatsapp.conversation_semantics import evaluate_whatsapp_agent_turn


class WhatsAppSharedEmailPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        db.add(Company(id=1, name="Test"))
        db.commit()
        db.close()

    @staticmethod
    def _extraction():
        return {
            "cliente": {"nombre_detectado": "Nombre inferido no verificado"},
            "pedido": {
                "lineas": [
                    {
                        "producto_detectado": "Caja eco",
                        "referencia_detectada": "ECO-1",
                        "cantidad": 4,
                        "unidad": "cajas",
                    }
                ],
                "observaciones": "Entrega por la mañana",
            },
        }

    def test_turn_uses_shared_email_classifier_and_extractor(self):
        db = self.Session()
        inbound = object()
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "pedido", "confianza": 0.91, "motivo": "Pedido explícito"},
            ) as classifier,
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value=self._extraction(),
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: Ponme cuatro cajas eco",
                latest_message_text="Ponme cuatro cajas eco",
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer={"id": "customer:8", "name": "Cliente verificado"},
                products=[],
                business_faq="[]",
                inbound_message=inbound,
                input_reference="whatsapp-session:4:revision:2",
            )

        classifier.assert_called_once_with(
            db,
            1,
            "Ponme cuatro cajas eco",
            input_reference="whatsapp-session:4:revision:2",
        )
        self.assertEqual(extractor.call_args.args[:3], (
            db,
            1,
            "CLIENTE: Ponme cuatro cajas eco",
        ))
        self.assertIs(extractor.call_args.args[3], inbound)
        self.assertEqual(extractor.call_args.kwargs["input_reference"], "whatsapp-session:4:revision:2")
        self.assertEqual(result.intent, "order")
        self.assertEqual(result.draft["customer_name"], "Cliente verificado")
        self.assertEqual(result.draft["lines"][0]["product_query"], "Caja eco")
        self.assertEqual(result.draft["lines"][0]["quantity"], 4)
        self.assertEqual(result.draft["notes"], "Entrega por la mañana")
        self.assertEqual(result.confidence, 0.91)
        self.assertEqual(result.prompt_execution_id, None)
        db.close()

    def test_public_pipeline_methods_delegate_to_email_methods(self):
        db = self.Session()
        pipeline = UnifiedOrderPipelineService()
        settings = object()
        inbound = object()
        with (
            patch("app.agent.platform.get_or_create_settings", return_value=settings),
            patch.object(pipeline, "_classify", return_value={"tipo_correo": "pedido"}) as classify,
            patch.object(pipeline, "_extract", return_value={"pedido": {"lineas": []}}) as extract,
        ):
            classified = pipeline.classify_inbound_text(
                db, 1, "pedido", input_reference="whatsapp-session:1"
            )
            extracted = pipeline.extract_inbound_order(
                db,
                1,
                "CLIENTE: pedido",
                inbound,
                input_reference="whatsapp-session:1",
            )
        self.assertEqual(classified["tipo_correo"], "pedido")
        self.assertIn("pedido", extracted)
        self.assertEqual(classify.call_args.args[1], settings)
        self.assertEqual(classify.call_args.args[3], "pedido")
        self.assertEqual(classify.call_args.kwargs["input_reference"], "whatsapp-session:1")
        self.assertEqual(extract.call_args.args[1], settings)
        self.assertEqual(extract.call_args.args[3], "CLIENTE: pedido")
        self.assertIs(extract.call_args.args[4], inbound)
        self.assertEqual(extract.call_args.kwargs["input_reference"], "whatsapp-session:1")
        db.close()

    def test_consultation_uses_only_a_matching_approved_faq(self):
        db = self.Session()
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "consulta", "confianza": 0.96, "motivo": "Pregunta"},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order"
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: ¿Cuál es el horario?",
                latest_message_text="¿Cuál es el horario?",
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer=None,
                products=[],
                business_faq=json.dumps(
                    [{"id": "faq:1", "question": "¿Cuál es el horario de apertura?", "answer": "De 9 a 17 h."}]
                ),
            )
        self.assertEqual(result.intent, "question")
        self.assertEqual(result.action, "respond")
        self.assertEqual(result.evidence_ids, ["faq:1"])
        self.assertTrue(result.reply_needed)
        extractor.assert_not_called()
        db.close()

    def test_short_customer_answer_continues_an_existing_order(self):
        db = self.Session()
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "no_pedido", "confianza": 0.82, "motivo": "Texto breve"},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value=self._extraction(),
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: ¿Qué unidad quieres?\nCLIENTE: cajas",
                latest_message_text="cajas",
                draft={"customer_name": "", "lines": [{"product_query": "Caja eco"}], "notes": ""},
                missing_or_uncertain=["Indicar la unidad"],
                customer=None,
                products=[],
                business_faq="[]",
            )
        self.assertEqual(result.intent, "order")
        extractor.assert_called_once()
        db.close()

    def test_unknown_question_or_incident_is_handed_to_a_person(self):
        db = self.Session()
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "incidencia", "confianza": 0.94, "motivo": "Reclamación"},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order"
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: Tengo una incidencia",
                latest_message_text="Tengo una incidencia",
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer=None,
                products=[],
                business_faq="[]",
            )
        self.assertEqual(result.action, "handoff")
        extractor.assert_not_called()
        db.close()

    def test_question_without_matching_faq_or_exact_product_is_handed_off(self):
        db = self.Session()
        with patch(
            "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
            return_value={"tipo_correo": "consulta", "confianza": 0.94, "motivo": "Pregunta"},
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: ¿Tenéis stock de un producto no identificado?",
                latest_message_text="¿Tenéis stock de un producto no identificado?",
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer=None,
                products=[],
                business_faq="[]",
            )
        self.assertEqual(result.intent, "question")
        self.assertEqual(result.action, "handoff")
        self.assertEqual(result.evidence_ids, [])
        db.close()

    def test_low_classification_confidence_hands_off_before_extraction(self):
        db = self.Session()
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "pedido", "confianza": 0.3, "motivo": "Dudoso"},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order"
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: Quiero hacer un pedido",
                latest_message_text="Quiero hacer un pedido",
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer=None,
                products=[],
                business_faq="[]",
            )
        self.assertEqual(result.action, "handoff")
        extractor.assert_not_called()
        db.close()

    def test_sensitive_classification_never_persists_prompt_or_response_payloads(self):
        db = self.Session()
        settings = LLMSettings(
            company_id=1,
            provider="openai",
            api_key_encrypted="synthetic-test-key",
            classification_model="gpt-4.1-mini",
            detailed_llm_logs=True,
            store_llm_payloads=True,
            anonymize_llm_logs=False,
        )
        db.add(settings)
        db.commit()

        result = run_prompt_execution(
            db,
            1,
            "classification",
            settings,
            "Cliente: synthetic@example.test +34600000000",
            provider_call=lambda *_args: {
                "ok": True,
                "content": '{"tipo_correo":"consulta","confianza":0.9,"motivo":"synthetic@example.test"}',
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
            input_reference="whatsapp-session:9:revision:1",
        )
        execution = db.get(PromptExecution, result["prompt_execution_id"])
        self.assertTrue(result["validation_ok"])
        self.assertFalse(result["detail_stored"])
        self.assertFalse(result["payload_stored"])
        self.assertIsNone(execution.response_excerpt)
        self.assertEqual(db.query(PromptExecutionDetail).count(), 0)
        db.close()

    def test_sensitive_classification_redacts_provider_error_text(self):
        db = self.Session()
        settings = LLMSettings(
            company_id=1,
            provider="openai",
            api_key_encrypted="synthetic-test-key",
            classification_model="gpt-4.1-mini",
            detailed_llm_logs=True,
            store_llm_payloads=True,
            anonymize_llm_logs=False,
        )
        db.add(settings)
        db.commit()

        result = run_prompt_execution(
            db,
            1,
            "classification",
            settings,
            "Cliente: synthetic@example.test +34600000000",
            provider_call=lambda *_args: {
                "ok": False,
                "message": "provider echoed synthetic@example.test +34600000000",
                "error_type": "provider_failure",
            },
            input_reference="whatsapp-session:10:revision:1",
        )
        execution = db.get(PromptExecution, result["prompt_execution_id"])
        self.assertEqual(execution.output_status, "provider_error")
        self.assertNotIn("synthetic@example.test", execution.validation_errors_json or "")
        self.assertNotIn("+34600000000", execution.validation_errors_json or "")
        self.assertNotIn("synthetic@example.test", json.dumps(result.get("validation_errors")))
        self.assertEqual(db.query(PromptExecutionDetail).count(), 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
