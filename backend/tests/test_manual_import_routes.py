from __future__ import annotations

import os
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from app.db.models import Email  # noqa: E402
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

    def test_channel_buttons_use_real_routes(self):
        fixture = build_performance_fixture("small")
        SessionLocal = _tenant_session(fixture.tenant_database_url)
        try:
            with SessionLocal() as db:
                email_id = db.scalar(select(Email.id).where(Email.company_id == fixture.company_id).order_by(Email.id))
            self.assertIsNotNone(email_id)

            with performance_test_client(fixture) as client:
                resolve_response = client.get(f"/entries/email-{email_id}/resolve", follow_redirects=False)
                self.assertEqual(resolve_response.status_code, 303)
                self.assertTrue(resolve_response.headers["location"].startswith("/"))

                process_response = client.post(f"/entries/email-{email_id}/process", follow_redirects=False)
                self.assertEqual(process_response.status_code, 303)
                self.assertTrue(process_response.headers["location"].startswith("/"))
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
