from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.db.models import Email, Product  # noqa: E402
from app.main import app  # noqa: E402
from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402


def _tenant_session(database_url: str):
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


class ManualImportRoutesTests(unittest.TestCase):
    def test_expected_ui_routes_are_registered(self):
        routes = {(route.path, tuple(sorted(route.methods or []))) for route in app.routes if hasattr(route, "methods")}
        expected = {
            ("/imports/manual", ("GET",)),
            ("/imports/manual/preview", ("POST",)),
            ("/entries", ("GET",)),
            ("/entries/{entry_id}", ("GET",)),
            ("/entries/{entry_id}/process", ("POST",)),
            ("/entries/{entry_id}/resolve", ("GET",)),
            ("/channels/{source_kind}/{source_id}/process", ("POST",)),
            ("/channels/{source_kind}/{source_id}/resolve", ("GET",)),
        }
        for item in expected:
            self.assertIn(item, routes)

    def test_manual_import_page_and_previews(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                page = client.get("/imports/manual")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Importación manual", page.text)

                email_preview = client.post(
                    "/imports/manual/preview",
                    data={
                        "channel": "email",
                        "sender": "cliente@example.com",
                        "subject": "Pedido demo",
                        "raw_text": "Necesitamos 10 cajas de producto A",
                    },
                )
                self.assertEqual(email_preview.status_code, 200)
                self.assertIn("Correo recibido", email_preview.text)

                whatsapp_preview = client.post(
                    "/imports/manual/preview",
                    data={
                        "channel": "whatsapp",
                        "client_participant": "Cliente Demo",
                        "company_participant": "Empresa Demo",
                        "raw_text": "[16/07/26, 09:30] Cliente Demo: Necesitamos 3 cajas de producto B",
                    },
                )
                self.assertEqual(whatsapp_preview.status_code, 200)
                self.assertIn("Vista previa", whatsapp_preview.text)

                invalid = client.post("/imports/manual/preview", data={"channel": "bogus", "raw_text": "Hola"})
                self.assertEqual(invalid.status_code, 422)
        finally:
            fixture.cleanup()


    def test_analysis_context_uses_validated_content_from_fenced_json_extraction(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_database_url)

        try:
            with SessionLocal() as db:
                db.add_all(
                    [
                        Product(
                            company_id=fixture.company_id,
                            reference="G2VASOPB12OZ",
                            name="Vaso Cartón Personalizable 12 oz BLANCO",
                            description="Vaso Cartón Personalizable 12 oz BLANCO",
                            status="active",
                        ),
                        Product(
                            company_id=fixture.company_id,
                            reference="GBAKRAFT12.5X12",
                            name="Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
                            description="Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
                            status="active",
                        ),
                    ]
                )
                db.commit()

                from app.imports.quick import analysis_context

                user = SimpleNamespace(company_id=fixture.company_id)

                validated_content = {
                    "cliente": "desconocido",
                    "pedido": {
                        "lineas": [
                            {
                                "texto_original": "2 cajas de Vaso Cartón Personalizable 12 oz BLANCO",
                                "referencia_detectada": "Vaso Cartón Personalizable 12 oz BLANCO",
                                "producto_detectado": "Vaso Cartón Personalizable 12 oz BLANCO",
                                "cantidad": 2,
                                "unidad": "cajas",
                                "confianza_extraccion": 0.95,
                            },
                            {
                                "texto_original": "3 cajas de Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
                                "referencia_detectada": "Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
                                "producto_detectado": "Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
                                "cantidad": 3,
                                "unidad": "cajas",
                                "confianza_extraccion": 0.95,
                            },
                        ]
                    },
                }

                fenced_content = """```json
{
  "cliente": "desconocido",
  "pedido": {
    "lineas": [
      {
        "texto_original": "2 cajas de Vaso Cartón Personalizable 12 oz BLANCO",
        "referencia_detectada": "Vaso Cartón Personalizable 12 oz BLANCO",
        "producto_detectado": "Vaso Cartón Personalizable 12 oz BLANCO",
        "cantidad": 2,
        "unidad": "cajas",
        "confianza_extraccion": 0.95
      },
      {
        "texto_original": "3 cajas de Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
        "referencia_detectada": "Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
        "producto_detectado": "Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
        "cantidad": 3,
        "unidad": "cajas",
        "confianza_extraccion": 0.95
      }
    ]
  }
}
```"""

                raw_text = """Buenos días,

Quiero hacer el siguiente pedido:

2 cajas de Vaso Cartón Personalizable 12 oz BLANCO
3 cajas de Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12

Gracias."""

                with patch(
                    "app.imports.quick.classify_sample",
                    return_value={
                        "ok": True,
                        "validation_ok": True,
                        "validated_content": {
                            "tipo_correo": "pedido",
                            "confianza": 0.95,
                            "motivo": "Pedido",
                        },
                    },
                ), patch(
                    "app.imports.quick.extract_sample",
                    return_value={
                        "ok": True,
                        "validation_ok": True,
                        "content": fenced_content,
                        "validated_content": validated_content,
                    },
                ):
                    result = analysis_context(
                        db,
                        user,
                        raw_text,
                        "uat-g04-01@example.com",
                        "UAT-G04-01 Pedido prueba sin PDF",
                        "",
                        "",
                        "",
                        source_label="Importación manual de correo",
                    )

                self.assertEqual(len(result["lines"]), 2)
                self.assertEqual(result["lines"][0]["quantity"], 2.0)
                self.assertEqual(result["lines"][1]["quantity"], 3.0)
                self.assertEqual(
                    result["lines"][0]["original_text"],
                    "2 cajas de Vaso Cartón Personalizable 12 oz BLANCO",
                )
                self.assertEqual(
                    result["lines"][1]["original_text"],
                    "3 cajas de Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12",
                )
                self.assertNotIn("Buenos días,", [line["original_text"] for line in result["lines"]])
                self.assertNotIn("Gracias.", [line["original_text"] for line in result["lines"]])
        finally:
            fixture.cleanup()


    def test_parse_line_block_ignores_natural_language(self):
        from app.imports.quick import _parse_line_block

        raw_text = """Buenos días,

Quiero hacer el siguiente pedido:

2 cajas de Vaso Cartón Personalizable 12 oz BLANCO
3 cajas de Bolsa abierta 2 lados 50 gsm KRAFT 12.5x12

Gracias."""

        self.assertEqual(_parse_line_block(raw_text), [])

    def test_parse_line_block_keeps_structured_tabular_input(self):
        from app.imports.quick import _parse_line_block

        result = _parse_line_block(
            "G2VASOPB12OZ | Vaso Cartón Personalizable 12 oz BLANCO | 2 | cajas"
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["reference"], "G2VASOPB12OZ")
        self.assertEqual(
            result[0]["product_name"],
            "Vaso Cartón Personalizable 12 oz BLANCO",
        )
        self.assertEqual(result[0]["quantity"], "2")
        self.assertEqual(result[0]["unit"], "cajas")


    def test_manual_import_result_shows_ai_classification_and_omits_empty_mismatch(self):
        fixture = build_performance_fixture("small")

        try:
            with performance_test_client(fixture) as client:
                with patch(
                    "app.imports.routes.analysis_context",
                    return_value={
                        "classification": {
                            "ok": True,
                            "validated_content": {
                                "tipo_correo": "pedido",
                                "confianza": 0.95,
                                "motivo": "Pedido",
                            },
                        },
                        "classification_type": "pedido",
                        "classification_confidence": 0.95,
                        "customer": {
                            "name": "desconocido",
                            "method": "sin_cliente",
                            "score": 0.0,
                            "matched": False,
                        },
                        "lines": [],
                        "score": 74.8,
                        "category": "doubtful",
                        "category_label": "Dudoso",
                        "status": "pedido_pendiente_revision",
                        "source_text": "Pedido de prueba",
                        "source_label": "Importación manual de correo",
                        "expected": {
                            "customer": "",
                            "score": None,
                            "status": "",
                        },
                        "comparison": {
                            "customer_match": False,
                            "score_delta": None,
                            "status_match": False,
                        },
                        "suggested_action": "Revisar",
                    },
                ):
                    response = client.post(
                        "/imports/quick",
                        data={
                            "sender": "uat@example.com",
                            "subject": "Pedido prueba",
                            "sample_text": "Pedido de prueba",
                            "expected_customer": "",
                            "expected_score": "",
                            "expected_status": "",
                        },
                    )

                self.assertEqual(response.status_code, 200)
                self.assertIn("Pedido", response.text)
                self.assertIn("95%", response.text)
                self.assertIn("Dudoso", response.text)
                self.assertIn("Revisar", response.text)
                self.assertNotIn("Cliente no coincide", response.text)
                self.assertNotIn("Estado no coincide", response.text)
        finally:
            fixture.cleanup()

    def test_channel_buttons_use_real_routes(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_database_url)
        try:
            with SessionLocal() as db:
                email_id = db.scalar(select(Email.id).where(Email.company_id == fixture.company_id).order_by(Email.id))
            self.assertIsNotNone(email_id)

            with performance_test_client(fixture) as client:
                resolve_response = client.get(f"/entries/email-{email_id}/resolve", follow_redirects=False)
                self.assertEqual(resolve_response.status_code, 200)
                self.assertIn("Confianza del pedido", resolve_response.text)

                process_response = client.post(f"/entries/email-{email_id}/process", follow_redirects=False)
                self.assertEqual(process_response.status_code, 303)
                self.assertTrue(process_response.headers["location"].startswith("/"))
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
