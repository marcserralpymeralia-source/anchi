from __future__ import annotations

import os
import unittest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENABLE_DEMO_BOOTSTRAP", "false")

from scripts.performance_data import build_performance_fixture, performance_test_client  # noqa: E402
from app.main import app  # noqa: E402


class OperationalNavigationTests(unittest.TestCase):
    def test_canonical_operator_routes_are_registered(self):
        routes = {(route.path, tuple(sorted(route.methods or []))) for route in app.routes if hasattr(route, "methods")}
        expected = {
            ("/entries", ("GET",)),
            ("/entries/{entry_id}", ("GET",)),
            ("/entries/{entry_id}/process", ("POST",)),
            ("/entries/{entry_id}/resolve", ("GET",)),
            ("/orders/{order_id}/save", ("POST",)),
            ("/orders/{order_id}/reprocess", ("POST",)),
            ("/orders/{order_id}/validate", ("POST",)),
            ("/orders/{order_id}/export", ("POST",)),
            ("/orders/{order_id}/discard", ("POST",)),
            ("/knowledge", ("GET",)),
        }
        for item in expected:
            self.assertIn(item, routes)

    def test_main_navigation_exposes_only_operational_sections(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                response = client.get("/inicio")

            self.assertEqual(response.status_code, 200)
            for label in ("Pedidos pendientes", "Entradas", "Productos", "Clientes", "Conocimiento", "Configuracion"):
                self.assertIn(label, response.text)
            for hidden_label in ("Histórico", "Bandeja de entrada", "Jobs", "Usuarios", "Importación rápida"):
                self.assertNotIn(f'class="nav-label">{hidden_label}</span>', response.text)
        finally:
            fixture.cleanup()

    def test_entries_and_knowledge_canonical_routes_are_available(self):
        fixture = build_performance_fixture("small")
        try:
            with performance_test_client(fixture) as client:
                entries = client.get("/entries")
                knowledge = client.get("/knowledge")

            self.assertEqual(entries.status_code, 200)
            self.assertIn("Entradas", entries.text)
            self.assertIn("Nueva entrada", entries.text)
            self.assertNotIn("Vista técnica", entries.text)
            self.assertEqual(knowledge.status_code, 200)
            self.assertIn("Conocimiento", knowledge.text)
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
