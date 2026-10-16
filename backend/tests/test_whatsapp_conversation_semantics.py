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

    def test_question_mislabeled_as_non_order_is_not_silenced(self):
        db = self.Session()
        text = "¿Hacéis envíos a Girona?"
        with patch(
            "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
            return_value={"tipo_correo": "no_pedido", "confianza": 0.99},
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript=f"CLIENTE: {text}",
                latest_message_text=text,
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer=None,
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.intent, "question")
        self.assertEqual(result.action, "handoff")
        self.assertTrue(result.reply_needed)
        db.close()

    def test_neutral_non_order_message_gets_a_safe_clarification(self):
        db = self.Session()
        text = "Quería hacer una consulta"
        with patch(
            "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
            return_value={"tipo_correo": "no_pedido", "confianza": 0.99},
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript=f"CLIENTE: {text}",
                latest_message_text=text,
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer=None,
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.intent, "other")
        self.assertEqual(result.action, "ask")
        self.assertTrue(result.reply_needed)
        db.close()

    def test_explicit_request_for_a_person_forces_handoff_even_if_classifier_says_no_order(self):
        db = self.Session()
        customer_texts = (
            "Necesito hablar con una persona",
            "Quiero hablar con un agente humano",
            "Podr\u00edas pasarme con alguien",
        )
        for customer_text in customer_texts:
            with self.subTest(customer_text=customer_text):
                with (
                    patch(
                        "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                        return_value={"tipo_correo": "no_pedido", "confianza": 0.99},
                    ),
                    patch(
                        "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order"
                    ) as extractor,
                ):
                    result = evaluate_whatsapp_agent_turn(
                        db,
                        company_id=1,
                        transcript=f"CLIENTE: {customer_text}",
                        latest_message_text=customer_text,
                        draft={"customer_name": "", "lines": [], "notes": ""},
                        customer=None,
                        products=[],
                        business_faq="[]",
                    )

                self.assertEqual(result.intent, "other")
                self.assertEqual(result.action, "handoff")
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
        self.assertTrue(result.contextual_order_followup)
        extractor.assert_called_once()
        db.close()

    def test_order_addition_is_preserved_when_same_message_contains_a_question(self):
        db = self.Session()
        previous = {
            "customer_name": "Cliente",
            "lines": [
                {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"}
            ],
            "notes": "",
        }
        extraction = {
            "pedido": {
                "lineas": [
                    {
                        "producto_detectado": "Caja kraft",
                        "referencia_detectada": "KRA-2",
                        "cantidad": 2,
                        "unidad": "cajas",
                    }
                ]
            }
        }
        transcript = (
            "CLIENTE: Quiero 4 cajas eco.\n"
            "CLIENTE: Añade 2 cajas kraft, ¿entregáis mañana?"
        )
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "consulta", "confianza": 0.41},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value=extraction,
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript=transcript,
                latest_message_text="Añade 2 cajas kraft, ¿entregáis mañana?",
                draft=previous,
                missing_or_uncertain=["Indicar la fecha de entrega"],
                customer={"name": "Cliente"},
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.intent, "order")
        self.assertTrue(result.contextual_order_followup)
        self.assertEqual(
            [(line["reference"], line["quantity"]) for line in result.draft["lines"]],
            [("ECO-1", 4), ("KRA-2", 2)],
        )
        self.assertIn("Indicar la fecha de entrega", result.missing_or_uncertain)
        self.assertEqual(extractor.call_count, 1)
        db.close()

    def test_explicit_addition_continues_a_complete_order_without_missing_fields(self):
        db = self.Session()
        previous = {
            "customer_name": "Cliente",
            "lines": [
                {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"}
            ],
            "notes": "",
        }
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "no_pedido", "confianza": 0.31},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value={
                    "pedido": {
                        "lineas": [
                            {
                                "producto_detectado": "Caja kraft",
                                "referencia_detectada": "KRA-2",
                                "cantidad": 2,
                                "unidad": "cajas",
                            }
                        ]
                    }
                },
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: Quiero 4 cajas eco.\nCLIENTE: A\u00f1ade 2 cajas kraft.",
                latest_message_text="A\u00f1ade 2 cajas kraft.",
                draft=previous,
                missing_or_uncertain=[],
                customer={"name": "Cliente"},
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.intent, "order")
        self.assertTrue(result.contextual_order_followup)
        self.assertEqual(
            [(line["reference"], line["quantity"]) for line in result.draft["lines"]],
            [("ECO-1", 4), ("KRA-2", 2)],
        )
        extractor.assert_called_once()
        db.close()

    def test_short_confirmation_to_the_agents_clarification_continues_order(self):
        db = self.Session()
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "no_pedido", "confianza": 0.87, "motivo": "Respuesta breve"},
            ) as classifier,
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value=self._extraction(),
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript=(
                    "CLIENTE: Quiero Caja eco\n"
                    "MENSAJE PREVIO RELACIONADO (EMPRESA): ¿Quieres pedirlo en cajas?\n"
                    "CLIENTE: Sí"
                ),
                latest_message_text="Sí",
                reply_context_text="EMPRESA: ¿Quieres pedirlo en cajas?",
                draft={"customer_name": "", "lines": [{"product_query": "Caja eco"}], "notes": ""},
                missing_or_uncertain=["Confirmar la unidad de pedido"],
                customer=None,
                products=[],
                business_faq="[]",
            )

        self.assertIn("EMPRESA: ¿Quieres pedirlo en cajas?", classifier.call_args.args[2])
        self.assertIn("Respuesta actual del cliente: Sí", classifier.call_args.args[2])
        self.assertEqual(result.intent, "order")
        self.assertTrue(result.contextual_order_followup)
        extractor.assert_called_once()
        db.close()

    def test_low_confidence_order_fragment_continues_and_preserves_earlier_lines(self):
        db = self.Session()
        extraction = {
            "pedido": {
                "lineas": [
                    {
                        "producto_detectado": "Bolsa kraft",
                        "referencia_detectada": "BOL-2",
                        "cantidad": 8,
                        "unidad": "cajas",
                    }
                ]
            }
        }
        previous_draft = {
            "customer_name": "Cliente validado",
            "lines": [
                {
                    "product_query": "Caja eco",
                    "reference": "ECO-1",
                    "quantity": 4,
                    "unit": "cajas",
                }
            ],
            "notes": "Entrega por la mañana",
        }
        transcript = "CLIENTE: Quiero 4 cajas eco\nCLIENTE: Son 8 cajas de bolsa kraft"

        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "dudoso", "confianza": 0.31, "motivo": "Fragmento breve"},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value=extraction,
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript=transcript,
                latest_message_text="Son 8 cajas de bolsa kraft",
                draft=previous_draft,
                missing_or_uncertain=["Indicar la referencia exacta"],
                customer={"name": "Cliente validado"},
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.intent, "order")
        self.assertNotEqual(result.action, "handoff")
        self.assertTrue(result.contextual_order_followup)
        self.assertEqual(
            [line["product_query"] for line in result.draft["lines"]],
            ["Caja eco", "Bolsa kraft"],
        )
        self.assertEqual(result.draft["customer_name"], "Cliente validado")
        self.assertIn("Entrega por la mañana", result.draft["notes"])
        self.assertIn("Indicar la referencia exacta", result.missing_or_uncertain)
        self.assertEqual(extractor.call_args.args[2], transcript)
        db.close()

    def test_extracted_correction_updates_a_saved_line_without_erasing_other_fields(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, preserved = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [
                    {
                        "product_query": "Caja eco",
                        "reference": "ECO-1",
                        "quantity": 4,
                        "unit": "cajas",
                    }
                ],
                "notes": "Entrega mañana",
            },
            {
                "customer_name": "",
                "lines": [{"product_query": "Caja eco", "quantity": 6}],
                "notes": "",
            },
        )

        self.assertFalse(preserved)
        self.assertEqual(len(merged["lines"]), 1)
        self.assertEqual(merged["lines"][0]["quantity"], 6)
        self.assertEqual(merged["lines"][0]["reference"], "ECO-1")
        self.assertEqual(merged["lines"][0]["unit"], "cajas")
        self.assertEqual(merged["notes"], "Entrega mañana")

    def test_order_handoff_preserves_the_existing_draft(self):
        db = self.Session()
        previous_draft = {
            "customer_name": "Cliente",
            "lines": [
                {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"}
            ],
            "notes": "Entrega jueves",
        }
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "pedido", "confianza": 0.97},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order"
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: Quiero hablar con una persona.",
                latest_message_text="Quiero hablar con una persona.",
                draft=previous_draft,
                missing_or_uncertain=["Confirmar la cantidad"],
                customer={"name": "Cliente"},
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.action, "handoff")
        self.assertEqual(result.draft, previous_draft)
        self.assertIn("Confirmar la cantidad", result.missing_or_uncertain)
        extractor.assert_not_called()
        db.close()

    def test_correction_drops_resolved_old_line_uncertainty_even_if_another_line_is_preserved(self):
        db = self.Session()
        previous_draft = {
            "customer_name": "Cliente",
            "lines": [
                {"product_query": "Caja eco", "reference": "ECO-1", "quantity": None, "unit": "cajas"},
                {"product_query": "Bolsa kraft", "reference": "KRAFT-1", "quantity": 4, "unit": "cajas"},
            ],
            "notes": "",
        }
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "pedido", "confianza": 0.97},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value={
                    "pedido": {
                        "lineas": [
                            {
                                "producto_detectado": "Caja eco",
                                "referencia_detectada": "ECO-1",
                                "cantidad": 6,
                                "unidad": "cajas",
                            }
                        ]
                    }
                },
            ),
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: Quiero Caja eco y Bolsa kraft.\nCLIENTE: Caja eco: 6 cajas.",
                latest_message_text="Caja eco: 6 cajas.",
                draft=previous_draft,
                missing_or_uncertain=["Indicar la cantidad de la línea 1"],
                customer={"name": "Cliente"},
                products=[],
                business_faq="[]",
            )

        self.assertEqual(len(result.draft["lines"]), 2)
        self.assertEqual(result.draft["lines"][0]["quantity"], 6)
        self.assertEqual(result.draft["lines"][1]["reference"], "KRAFT-1")
        self.assertNotIn("Indicar la cantidad de la línea 1", result.missing_or_uncertain)
        db.close()

    def test_explicit_product_correction_replaces_only_the_named_order_line(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, preserved = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"},
                    {"product_query": "Vaso compostable", "reference": "VASO-2", "quantity": 10, "unit": "uds"},
                ],
                "notes": "Entrega por la mañana",
            },
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Bolsa kraft", "reference": "KRAFT-3", "quantity": 6, "unit": "cajas"},
                ],
                "notes": "",
            },
            latest_message_text="Cambia Caja eco por Bolsa kraft: serán 6 cajas.",
        )

        self.assertTrue(preserved)
        self.assertEqual(
            [(line["product_query"], line["quantity"]) for line in merged["lines"]],
            [("Bolsa kraft", 6), ("Vaso compostable", 10)],
        )
        self.assertEqual(merged["notes"], "Entrega por la mañana")

    def test_colloquial_product_correction_replaces_single_saved_line(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, _ = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"}
                ],
                "notes": "Entrega jueves",
            },
            {
                "customer_name": "Cliente",
                "lines": [
                    {"product_query": "Caja mediana", "reference": "MED-1", "quantity": 6, "unit": "cajas"}
                ],
                "notes": "",
            },
            latest_message_text="Perdona, me refería a Caja mediana: seis cajas.",
        )
        self.assertEqual(len(merged["lines"]), 1)
        self.assertEqual(merged["lines"][0]["reference"], "MED-1")
        self.assertEqual(merged["lines"][0]["quantity"], 6)
        self.assertEqual(merged["notes"], "Entrega jueves")

    def test_explicit_delivery_note_correction_replaces_the_previous_value(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, _preserved = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [{"product_query": "Caja eco", "quantity": 4}],
                "notes": "Entrega por la mañana",
            },
            {
                "customer_name": "",
                "lines": [],
                "notes": "Entrega por la tarde",
            },
            latest_message_text="Corrige la entrega: en lugar de por la mañana, por la tarde.",
        )

        self.assertEqual(merged["notes"], "Entrega por la tarde")

    def test_delivery_note_correction_keeps_unrelated_delivery_instructions(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, _preserved = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [{"product_query": "Caja eco", "quantity": 4}],
                "notes": "Entrega por la mañana\nDejar en recepción",
            },
            {
                "customer_name": "",
                "lines": [],
                "notes": "Entrega por la tarde",
            },
            latest_message_text="Corrige la entrega: en lugar de por la mañana, por la tarde.",
        )

        self.assertNotIn("mañana", merged["notes"])
        self.assertIn("Entrega por la tarde", merged["notes"])
        self.assertIn("Dejar en recepción", merged["notes"])

    def test_combined_product_and_delivery_correction_updates_both_fields(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, _preserved = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [
                    {"product_query": "Caja eco", "quantity": 4},
                    {"product_query": "Vaso", "quantity": 10},
                ],
                "notes": "Entrega por la mañana",
            },
            {
                "customer_name": "",
                "lines": [{"product_query": "Caja kraft", "quantity": 4}],
                "notes": "Entrega por la tarde",
            },
            latest_message_text=(
                "Cambia Caja eco por Caja kraft y corrige la entrega: por la tarde."
            ),
        )

        self.assertEqual(
            [line["product_query"] for line in merged["lines"]],
            ["Caja kraft", "Vaso"],
        )
        self.assertEqual(merged["notes"], "Entrega por la tarde")

    def test_additional_delivery_note_keeps_existing_order_notes(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, _preserved = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [{"product_query": "Caja eco", "quantity": 4}],
                "notes": "Entrega jueves",
            },
            {
                "customer_name": "",
                "lines": [],
                "notes": "Entregar en recepción",
            },
            latest_message_text="Añade que lo dejen en recepción.",
        )

        self.assertIn("Entrega jueves", merged["notes"])
        self.assertIn("Entregar en recepción", merged["notes"])

    def test_shared_turn_extractor_applies_an_explicit_product_correction(self):
        db = self.Session()
        latest = "Cambia Caja eco por Bolsa kraft: seran 6 cajas."
        extraction = {
            "pedido": {
                "lineas": [
                    {
                        "producto_detectado": "Bolsa kraft",
                        "referencia_detectada": "KRAFT-3",
                        "cantidad": 6,
                        "unidad": "cajas",
                    }
                ]
            }
        }
        previous_draft = {
            "customer_name": "Cliente validado",
            "lines": [
                {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"},
                {"product_query": "Vaso compostable", "reference": "VASO-2", "quantity": 10, "unit": "uds"},
            ],
            "notes": "Entrega por la mañana",
        }
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "pedido", "confianza": 0.97},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value=extraction,
            ),
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript=f"CLIENTE: Quiero 4 cajas de Caja eco\nCLIENTE: {latest}",
                latest_message_text=latest,
                draft=previous_draft,
                missing_or_uncertain=[],
                customer={"name": "Cliente validado"},
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.intent, "order")
        self.assertEqual(
            [(line["product_query"], line["quantity"]) for line in result.draft["lines"]],
            [("Bolsa kraft", 6), ("Vaso compostable", 10)],
        )
        self.assertEqual(result.draft["notes"], "Entrega por la mañana")
        db.close()

    def test_adding_a_different_product_does_not_replace_an_existing_line(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, preserved = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"},
                ],
                "notes": "",
            },
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Bolsa kraft", "reference": "KRAFT-3", "quantity": 6, "unit": "cajas"},
                ],
                "notes": "",
            },
            latest_message_text="Añade también 6 cajas de Bolsa kraft.",
        )

        self.assertTrue(preserved)
        self.assertEqual(
            [line["product_query"] for line in merged["lines"]],
            ["Caja eco", "Bolsa kraft"],
        )

    def test_correction_to_an_already_present_product_does_not_duplicate_it(self):
        from app.whatsapp.conversation_semantics import _merge_order_drafts

        merged, _preserved = _merge_order_drafts(
            {
                "customer_name": "Cliente",
                "lines": [
                    {"product_query": "Caja eco", "reference": "ECO-1", "quantity": 4, "unit": "cajas"},
                    {"product_query": "Bolsa kraft", "reference": "KRAFT-3", "quantity": 2, "unit": "cajas"},
                ],
                "notes": "",
            },
            {
                "customer_name": "",
                "lines": [
                    {"product_query": "Bolsa kraft", "reference": "KRAFT-3", "quantity": 6, "unit": "cajas"},
                ],
                "notes": "",
            },
            latest_message_text="Sustituye Caja eco por Bolsa kraft: seran 6 cajas.",
        )

        self.assertEqual(len(merged["lines"]), 1)
        self.assertEqual(merged["lines"][0]["product_query"], "Bolsa kraft")
        self.assertEqual(merged["lines"][0]["quantity"], 6)

    def test_simple_greeting_gets_a_reply_without_becoming_an_order_fragment(self):
        db = self.Session()
        previous_draft = {
            "customer_name": "Cliente",
            "lines": [{"product_query": "Caja eco", "quantity": None}],
            "notes": "",
        }
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "no_pedido", "confianza": 0.96},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order"
            ) as extractor,
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: Hola",
                latest_message_text="Hola",
                draft=previous_draft,
                missing_or_uncertain=["Indicar la cantidad"],
                customer={"name": "Cliente"},
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.intent, "greeting")
        self.assertEqual(result.action, "respond")
        self.assertTrue(result.reply_needed)
        self.assertFalse(result.contextual_order_followup)
        extractor.assert_not_called()
        db.close()

    def test_common_spanish_greetings_are_recognized(self):
        from app.whatsapp.conversation_semantics import _is_simple_greeting

        for greeting in ("Hola", "¡Hola!", "Buenas tardes", "Buenos días", "Hey"):
            with self.subTest(greeting=greeting):
                self.assertTrue(_is_simple_greeting(greeting))
        self.assertFalse(_is_simple_greeting("Hola, quiero hacer un pedido"))

    def test_delivery_date_answer_is_a_contextual_order_followup(self):
        from app.whatsapp.conversation_semantics import _is_contextual_order_followup

        self.assertTrue(
            _is_contextual_order_followup(
                "El viernes por la tarde",
                previous_lines=[{"product_query": "Caja eco", "quantity": 4}],
                previous_missing=["Indicar la fecha de entrega"],
            )
        )

    def test_order_with_more_than_thirty_lines_is_handed_off_without_silent_truncation(self):
        db = self.Session()
        extraction = {
            "pedido": {
                "lineas": [
                    {
                        "producto_detectado": f"Producto {index}",
                        "referencia_detectada": f"REF-{index}",
                        "cantidad": 1,
                        "unidad": "caja",
                    }
                    for index in range(31)
                ]
            }
        }
        with (
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
                return_value={"tipo_correo": "pedido", "confianza": 0.99},
            ),
            patch(
                "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.extract_inbound_order",
                return_value=extraction,
            ),
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: Adjunto el pedido completo",
                latest_message_text="Adjunto el pedido completo",
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer=None,
                products=[],
                business_faq="[]",
            )

        self.assertEqual(result.action, "handoff")
        self.assertEqual(len(result.draft["lines"]), 30)
        self.assertTrue(
            any("supera el límite de 30 líneas" in item for item in result.missing_or_uncertain)
        )
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

    def test_faq_with_only_shared_location_does_not_answer_a_different_question(self):
        db = self.Session()
        business_faq = json.dumps(
            [
                {
                    "id": "faq:delivery-cost",
                    "question": "¿Cuánto cuesta la entrega a Madrid?",
                    "answer": "La entrega a Madrid cuesta 8 euros.",
                }
            ],
            ensure_ascii=False,
        )
        with patch(
            "app.whatsapp.conversation_semantics.UnifiedOrderPipelineService.classify_inbound_text",
            return_value={"tipo_correo": "consulta", "confianza": 0.96},
        ):
            result = evaluate_whatsapp_agent_turn(
                db,
                company_id=1,
                transcript="CLIENTE: ¿A qué hora llega el pedido a Madrid?",
                latest_message_text="¿A qué hora llega el pedido a Madrid?",
                draft={"customer_name": "", "lines": [], "notes": ""},
                customer=None,
                products=[],
                business_faq=business_faq,
            )

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
