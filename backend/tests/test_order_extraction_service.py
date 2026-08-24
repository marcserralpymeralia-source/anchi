from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

os.environ["APP_ENV"] = "development"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.extraction import OrderExtractionError, extract_order  # noqa: E402
from app.agent.extraction.diagnostics import extraction_diagnostics_from_payload  # noqa: E402
from app.agent.platform import UnifiedOrderPipelineService  # noqa: E402
from app.agent.extraction.prompts import ORDER_EXTRACTION_SYSTEM_PROMPT  # noqa: E402


class FakeMessage:
    def __init__(self, content: str):
        self.content = content


class FakeChoice:
    def __init__(self, content: str):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content: str):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, content: str):
        self.content = content
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return FakeResponse(self.content)


class FakeChat:
    def __init__(self, content: str):
        self.completions = FakeCompletions(content)


class FakeClient:
    def __init__(self, payload: dict):
        self.chat = FakeChat(json.dumps(payload))


class OrderExtractionServiceTests(unittest.TestCase):
    def test_extract_order_calls_structured_output_and_wraps_trace(self):
        payload = {
            "isOrder": True,
            "customer": {"rawName": "Cliente Demo", "rawNameSource": "expressed"},
            "lines": [
                {
                    "rawText": "10 cajas de arroz",
                    "rawDescription": "arroz",
                    "rawDescriptionSource": "expressed",
                    "quantity": 10,
                    "quantitySource": "expressed",
                    "unit": "cajas",
                    "unitSource": "expressed",
                    "notes": [],
                    "uncertainties": [],
                    "requiresReview": False,
                }
            ],
            "notes": [],
            "uncertainties": [],
            "requiresReview": False,
        }
        client = FakeClient(payload)
        result = extract_order({"text": "Cliente Demo pide 10 cajas de arroz", "sourceType": "whatsapp", "sourceId": "wa-1"}, client=client, model="gpt-test")

        kwargs = client.chat.completions.kwargs
        self.assertEqual(kwargs["model"], "gpt-test")
        self.assertEqual(kwargs["response_format"]["type"], "json_schema")
        self.assertEqual(kwargs["response_format"]["json_schema"]["name"], "order_extraction")
        self.assertIn("isOrder", kwargs["response_format"]["json_schema"]["schema"]["properties"])
        self.assertIn("No busques, inventes ni asignes customerId", ORDER_EXTRACTION_SYSTEM_PROMPT)
        self.assertEqual(result.raw_input.source_type, "whatsapp")
        self.assertEqual(result.raw_input.source_id, "wa-1")
        self.assertTrue(result.extracted_data.is_order)
        self.assertEqual(result.model, "gpt-test")
        self.assertEqual(result.schema_version, "1.0")

    def test_extract_order_rejects_non_json_response(self):
        class BadClient:
            chat = FakeChat("no-json")

        with self.assertRaises(OrderExtractionError):
            extract_order({"text": "Pedido"}, client=BadClient(), model="gpt-test")

    def test_extract_order_rejects_erp_identifiers_from_model(self):
        payload = {
            "isOrder": True,
            "customer": {"rawName": "Cliente Demo", "rawNameSource": "expressed"},
            "customerId": 1,
            "lines": [
                {
                    "rawText": "1 caja",
                    "rawDescription": "caja",
                    "rawDescriptionSource": "expressed",
                    "quantity": 1,
                    "quantitySource": "expressed",
                    "unit": "caja",
                    "unitSource": "expressed",
                    "notes": [],
                    "uncertainties": [],
                    "requiresReview": True,
                }
            ],
            "notes": [],
            "uncertainties": [],
            "requiresReview": True,
        }
        with self.assertRaises(ValueError):
            extract_order({"text": "Pedido"}, client=FakeClient(payload), model="gpt-test")

    def test_pipeline_prefers_structured_extraction_payload_before_legacy_matching(self):
        payload = {
            "isOrder": True,
            "customer": {"rawName": "Cliente Demo", "rawNameSource": "expressed"},
            "lines": [
                {
                    "rawText": "20 del plato que pedimos la semana pasada",
                    "rawDescription": "plato que pedimos la semana pasada",
                    "rawDescriptionSource": "expressed",
                    "quantity": 20,
                    "quantitySource": "expressed",
                    "unit": None,
                    "unitSource": "unknown",
                    "notes": [],
                    "uncertainties": [
                        {
                            "field": "lines[0].rawDescription",
                            "reason": "La referencia depende del historial del cliente.",
                        }
                    ],
                    "requiresReview": True,
                }
            ],
            "notes": [],
            "uncertainties": [{"field": "lines[0].rawDescription", "reason": "Debe resolverse en matching."}],
            "requiresReview": True,
        }
        structured = extract_order({"text": "pedido", "sourceType": "email"}, client=FakeClient(payload), model="gpt-test")
        settings = SimpleNamespace(
            api_key_encrypted="encrypted-key",
            extraction_model="gpt-test",
            base_url="https://example.test/v1",
            timeout_seconds=30,
        )

        with patch("app.agent.platform.decrypt_secret", return_value="plain-key"), patch("app.agent.platform.extract_order", return_value=structured) as extractor:
            legacy = UnifiedOrderPipelineService()._extract(None, settings, 1, "pedido normalizado")

        extractor.assert_called_once()
        self.assertEqual(legacy["cliente"]["nombre_detectado"], "Cliente Demo")
        self.assertEqual(legacy["pedido"]["lineas"][0]["texto_original"], "20 del plato que pedimos la semana pasada")
        self.assertEqual(legacy["pedido"]["lineas"][0]["producto_detectado"], "plato que pedimos la semana pasada")
        self.assertIsNone(legacy["pedido"]["lineas"][0]["unidad"])
        self.assertIsNone(legacy["pedido"]["lineas"][0]["referencia_detectada"])
        self.assertEqual(legacy["_extraction_meta"]["schemaVersion"], "1.0")
        self.assertEqual(legacy["_extraction_meta"]["source"], "structured_order_extraction")

    def test_pipeline_falls_back_to_legacy_and_records_reason(self):
        settings = SimpleNamespace(
            api_key_encrypted="encrypted-key",
            extraction_model="gpt-test",
            base_url="https://example.test/v1",
            timeout_seconds=30,
        )
        legacy_response = {
            "ok": True,
            "content": json.dumps(
                {
                    "cliente": {"nombre_detectado": "Cliente Legacy"},
                    "pedido": {
                        "lineas": [
                            {
                                "texto_original": "5 cajas de agua",
                                "producto_detectado": "agua",
                                "cantidad": 5,
                                "unidad": "cajas",
                                "confianza_extraccion": 0.7,
                            }
                        ]
                    },
                }
            ),
        }

        with patch("app.agent.platform.decrypt_secret", return_value="plain-key"), patch("app.agent.platform.extract_order", side_effect=RuntimeError("boom")), patch("app.agent.platform._active_prompt", return_value="prompt"), patch("app.agent.platform.extract_sample", return_value=legacy_response):
            legacy = UnifiedOrderPipelineService()._extract(None, settings, 1, "pedido normalizado")

        self.assertEqual(legacy["_extraction_meta"]["source"], "legacy_extraction")
        self.assertEqual(legacy["_extraction_meta"]["structuredFallbackReason"], "RuntimeError")
        self.assertEqual(legacy["pedido"]["lineas"][0]["producto_detectado"], "agua")

    def test_extraction_diagnostics_reads_structured_and_legacy_payloads(self):
        structured_payload = {
            "_extraction_meta": {
                "source": "structured_order_extraction",
                "schemaVersion": "1.0",
                "model": "gpt-test",
                "payload": {
                    "requiresReview": True,
                    "uncertainties": [{"field": "customer.rawName", "reason": "No aparece cliente."}],
                    "lines": [
                        {
                            "uncertainties": [
                                {
                                    "field": "lines[0].rawDescription",
                                    "reason": "Descripcion contextual.",
                                }
                            ]
                        }
                    ],
                },
            }
        }
        structured = extraction_diagnostics_from_payload(json.dumps(structured_payload))
        self.assertEqual(structured["source"], "structured_order_extraction")
        self.assertEqual(structured["label"], "Extractor estructurado")
        self.assertEqual(structured["schema_version"], "1.0")
        self.assertEqual(structured["uncertainty_count"], 2)

        legacy = extraction_diagnostics_from_payload(json.dumps({"_extraction_meta": {"source": "legacy_extraction", "structuredFallbackReason": "RuntimeError"}}))
        self.assertEqual(legacy["label"], "Extractor anterior")
        self.assertEqual(legacy["fallback_reason"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
